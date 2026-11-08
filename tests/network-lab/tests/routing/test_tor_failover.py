"""Disable all owned ToR data ports while retaining management and live probes."""

import importlib
import json
import re
import subprocess
import time
from contextlib import ExitStack
from itertools import pairwise
from pathlib import Path

import pytest
from maas_bird.runtime import write_atomic

ROOT = Path(__file__).resolve().parents[2]
TORS = ("r1a", "r1b", "r2a", "r2b")
pytestmark = pytest.mark.integration


def replies(text, address):
    return [
        (float(t), int(s))
        for t, s in re.findall(
            rf"(?m)^\[(\d+\.\d+)\].* bytes from {re.escape(address)}: icmp_seq=(\d+)", text
        )
    ]


@pytest.mark.parametrize("tor", TORS)
def test_tor_data_outage_preserves_other_paths_and_restores(
    router, host_manifest, host_packets, tor
):
    assert host_manifest is not None, "Require --host-routing for the four-worker fabric"
    assert host_packets is not None
    host = importlib.import_module("host_acceptance")
    hosts = importlib.import_module("commission").HOSTS
    provider = json.loads((ROOT / "containerlab/provider.json").read_text())
    gateways = json.loads((ROOT / "containerlab/gateways.json").read_text())
    assert host_manifest["provider_intent"] == provider
    assert host_manifest["gateway_intent"] == gateways
    endpoints = json.loads((ROOT / "runtime/gateways/ovn-intent.json").read_text())["endpoints"]
    assert set(endpoints) == set(hosts)
    assert all(endpoints[n]["uuid"] == host_manifest["worker_uuids"][n] for n in hosts)
    labels = json.loads(
        host.root(
            "fabric",
            "docker",
            "inspect",
            "--format",
            "{{json .Config.Labels}}",
            "clab-maas-bird-fabric-" + tor,
        ).stdout
    )
    assert labels["containerlab"] == "maas-bird-fabric"

    def state(down=None):
        result = {}
        for name, config in hosts.items():
            parents = ("enp7s0", "enp8s0")
            if down and config["rack"] == int(down[1]):
                parents = ("enp8s0",) if down.endswith("a") else ("enp7s0",)
            result[name] = host.routing_ready(
                name, parents=parents, unavailable_tors=(down,) if down else ()
            )
        return result

    def nat_packets(down=None):
        results = {}
        available = {n: e for n, e in provider["endpoints"].items() if e["tor"] != down}
        assert len(available) == (1 if down and down.endswith("b") else 2)
        for external in available:
            for name, endpoint in endpoints.items():
                text = host.root(
                    "fabric",
                    "ip",
                    "netns",
                    "exec",
                    external,
                    "ping",
                    "-n",
                    "-M",
                    "do",
                    "-s",
                    "1372",
                    "-c",
                    "3",
                    "-i",
                    "0.2",
                    "-W",
                    "1",
                    endpoint["floating"],
                ).stdout
                assert "3 packets transmitted, 3 received, 0% packet loss" in text, text
                results[external + "->" + name] = text
        return results

    before = state()
    baseline_nat = host.wait_for(nat_packets, timeout=45)
    links = {r["ifname"]: r for r in json.loads(router(tor, "ip", "-j", "link", "show"))}
    base = {n for n in links if re.fullmatch(r"eth[0-9]+", n)}
    expected = {"eth0", "eth1", "eth2", "eth3", "eth4"}
    if any(g["tor"] == tor for g in gateways["gateways"].values()):
        expected.add("eth5")
    assert base == expected
    ports = sorted(base - {"eth0"})
    assert all("UP" in links[p]["flags"] for p in ports)
    directory = ROOT / f"runtime/tor-failures/{tor}-{time.time_ns()}"
    directory.mkdir(mode=0o700, parents=True)
    receipt = {
        "tor": tor,
        "started": time.time(),
        "ports": ports,
        "before": before,
        "baseline_nat": baseline_nat,
        "flows": [],
    }
    processes = []
    changed = []
    with ExitStack() as files:
        try:
            for index, (name, config) in enumerate(hosts.items()):
                target = next(n for n in hosts if hosts[n]["rack"] != config["rack"])
                address = hosts[target]["loopback"].split("/")[0]
                output = directory / (name + ".txt")
                errors = directory / (name + ".err")
                output.touch(mode=0o600)
                errors.touch(mode=0o600)
                args = [
                    "lxc",
                    "exec",
                    "--project",
                    host.PROJECT,
                    name,
                    "--",
                    "stdbuf",
                    "-oL",
                    "ping",
                    "-n",
                    "-D",
                    "-O",
                    "-M",
                    "do",
                    "-s",
                    "1472",
                    "-I",
                    config["loopback"].split("/")[0],
                    "-e",
                    str(55000 + index),
                    "-i",
                    "0.1",
                    "-w",
                    "45",
                    "-W",
                    "1",
                    address,
                ]
                process = subprocess.Popen(
                    args,
                    stdout=files.enter_context(output.open("w")),
                    stderr=files.enter_context(errors.open("w")),
                    text=True,
                )
                processes.append(process)
                receipt["flows"].append({"source": name, "destination": address})
            deadline = time.monotonic() + 10
            while not all(
                replies((directory / (f["source"] + ".txt")).read_text(), f["destination"])
                for f in receipt["flows"]
            ):
                assert all(p.poll() is None for p in processes) and time.monotonic() < deadline
                time.sleep(0.1)
            receipt["down_before"] = time.time()
            for port in ports:
                changed.append(port)
                router(tor, "ip", "link", "set", "dev", port, "down")
            receipt["down_after"] = time.time()
            receipt["surviving"] = host.wait_for(lambda: state(tor), timeout=30)
            receipt["route_convergence_observed"] = time.time()
            receipt["surviving_role_packets"] = {n: host.packets(n) for n in hosts}
            receipt["surviving_tenant_packets"] = {n: host.overlay_ready(n) for n in hosts}
            receipt["surviving_nat"] = host.wait_for(lambda: nat_packets(tor), timeout=45)
            receipt["management"] = {n: host.management(n) for n in hosts}
            assert (
                "UP"
                in json.loads(router(tor, "ip", "-j", "link", "show", "dev", "eth0"))[0]["flags"]
            )
            for process in processes:
                process.communicate(timeout=55)
                assert process.returncode == 0
            for flow in receipt["flows"]:
                text = (directory / (flow["source"] + ".txt")).read_text()
                rows = replies(text, flow["destination"])
                assert len(rows) > 2 and rows[-1][0] - rows[0][0] >= 40
                counts = re.search(r"(\d+) packets transmitted, (\d+) received", text)
                assert counts is not None
                sequences = [s for _, s in rows]
                flow.update(
                    transmitted=int(counts[1]),
                    received=int(counts[2]),
                    max_inter_reply_gap=max(b[0] - a[0] for a, b in pairwise(rows)),
                    reordered_replies=sum(b < a for a, b in pairwise(sequences)),
                    duplicate_replies=len(sequences) - len(set(sequences)),
                    trace=text,
                )
            receipt["fault_assertions_passed"] = True
        except BaseException as error:
            receipt["fault_error"] = {
                "type": type(error).__name__,
                "message": str(error),
                "notes": getattr(error, "__notes__", []),
            }
            raise
        finally:
            try:
                for port in changed:
                    router(tor, "ip", "link", "set", "dev", port, "up")
                receipt["up_requested"] = time.time()
                recovery_started = time.monotonic()
                strict_error = None
                try:
                    receipt["restored"] = host.wait_for(state, timeout=30)
                except AssertionError as error:
                    strict_error = error
                    receipt["strict_restoration_failure"] = str(error)
                    receipt["strict_restoration_elapsed"] = time.monotonic() - recovery_started
                    receipt["eventual_restored"] = host.wait_for(state, timeout=180)
                    receipt["eventual_restoration_elapsed"] = time.monotonic() - recovery_started
                receipt["restored_role_packets"] = {n: host.packets(n) for n in hosts}
                receipt["restored_tenant_packets"] = {
                    n: host.wait_for(lambda n=n: host.overlay_ready(n), timeout=45) for n in hosts
                }
                receipt["restored_nat"] = host.wait_for(nat_packets, timeout=45)
                receipt["passed"] = (
                    receipt.get("fault_assertions_passed", False) and strict_error is None
                )
                if strict_error is not None and "fault_error" not in receipt:
                    raise strict_error
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=55)
                write_atomic(
                    directory / "receipt.json", json.dumps(receipt, indent=2) + "\n", 0o600
                )
