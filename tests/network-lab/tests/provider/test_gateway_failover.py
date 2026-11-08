"""Stop an actual isolated gateway role and observe OVN HA plus live packets."""

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
pytestmark = pytest.mark.integration


def active_gateway(lab):
    names = json.loads(
        lab.fabric("ovn-sbctl", "--format=json", "--columns=name,_uuid", "list", "Chassis")
    )["data"]
    names = {uuid[1]: name for name, uuid in names}
    ports = json.loads(
        lab.fabric(
            "ovn-sbctl",
            "--format=json",
            "--columns=logical_port,chassis",
            "find",
            "Port_Binding",
            "type=chassisredirect",
        )
    )["data"]
    assert len(ports) == 1 and ports[0][0] == "cr-provider-router-to-external"
    binding = ports[0][1]
    return names.get(binding[1]) if binding[0] == "uuid" else None


def wait_binding(lab, expected, timeout=45):
    deadline = time.monotonic() + timeout
    while True:
        current = active_gateway(lab)
        if current == expected:
            return time.time()
        assert time.monotonic() < deadline, (current, expected)
        time.sleep(0.2)


def operate(lab, name, operation):
    return lab.fabric(
        "python3",
        "/opt/network-lab/gateway-src/gateway_dataplane.py",
        "--model",
        "/opt/network-lab/gateway-src/gateways.json",
        "--name",
        name,
        operation,
    )


def replies(text, destination):
    return [
        float(t)
        for t in re.findall(rf"(?m)^\[(\d+\.\d+)\].* bytes from {re.escape(destination)}:", text)
    ]


def full_mtu_packets(lab, endpoints):
    results = {}
    for external in ("nlab-provider-r1", "nlab-provider-r2"):
        for name, config in endpoints.items():
            text = lab.fabric(
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
                config["floating"],
            )
            assert "3 packets transmitted, 3 received, 0% packet loss" in text, {
                "external": external,
                "worker": name,
                "packets": text,
            }
            results[external + "->" + name] = text
    return results


@pytest.mark.parametrize("failed", ("nlab-gw-r1", "nlab-gw-r2"))
def test_provider_link_only_outage_preserves_floating_ip_traffic(gateways, failed):
    lab, model, endpoints, _ = gateways
    preferred = max(model["gateways"], key=lambda n: model["gateways"][n]["priority"])
    wait_binding(lab, preferred)
    directory = ROOT / f"runtime/gateways/provider-link-{failed}-{time.time_ns()}"
    directory.mkdir(mode=0o700, parents=True)
    receipt = {"failed": failed, "started": time.time(), "observations": []}

    def namespace(*args):
        return lab.fabric("ip", "netns", "exec", failed, *args)

    def state():
        links = {
            r["ifname"]: r
            for r in json.loads(namespace("ip", "-j", "link", "show"))
            if r["ifname"] in {"provider0", "transport0", "control0"}
        }
        assert set(links) == {"provider0", "transport0", "control0"}
        for interface in ("transport0", "control0"):
            assert {"UP", "LOWER_UP"} <= set(links[interface]["flags"])
        process_ids = namespace(
            "cat",
            *(
                f"/run/network-lab/gateways/{failed}/{c}.pid"
                for c in ("ovsdb-server", "ovs-vswitchd", "ovn-controller")
            ),
        ).splitlines()
        assert len(process_ids) == len(set(process_ids)) == 3
        connected = namespace(
            "ovn-appctl",
            "-t",
            f"/run/ovn/ovn-controller.{process_ids[2]}.ctl",
            "connection-status",
        )
        assert connected == "connected"
        return {
            "links": links,
            "controller": connected,
            "pids": process_ids,
            "active_gateway": active_gateway(lab),
            "at": time.time(),
        }

    receipt["before"] = state()
    assert "UP" in receipt["before"]["links"]["provider0"]["flags"]
    receipt["baseline_packets"] = full_mtu_packets(lab, endpoints)
    try:
        namespace("ip", "link", "set", "dev", "provider0", "down")
        receipt["fault_installed"] = time.time()
        deadline = time.monotonic() + 30
        fault = state()
        assert "UP" not in fault["links"]["provider0"]["flags"]
        assert fault["pids"] == receipt["before"]["pids"]
        receipt["fault_state"] = fault
        host = importlib.import_module("host_acceptance")
        receipt["fault_routing"] = {name: host.routing_ready(name) for name in endpoints}
        text = lab.ssh(
            "probe",
            "ping",
            "-n",
            "-M",
            "do",
            "-s",
            "1472",
            "-I",
            lab.HOSTS["probe"]["loopback"].split("/")[0],
            "-c",
            "3",
            "-i",
            "0.2",
            "-W",
            "1",
            model["gateways"][failed]["vtep"].split("/")[0],
        )
        assert "3 packets transmitted, 3 received, 0% packet loss" in text
        receipt["transport_packets"] = text
        while True:
            observation = state()
            receipt["observations"].append(observation)
            assert "UP" not in observation["links"]["provider0"]["flags"]
            assert observation["pids"] == receipt["before"]["pids"]
            try:
                receipt["surviving_packets"] = full_mtu_packets(lab, endpoints)
                receipt["forwarding_observed"] = time.time()
                assert time.monotonic() <= deadline, (
                    "Full provider packet matrix exceeded 30 seconds"
                )
                break
            except (AssertionError, subprocess.CalledProcessError) as error:
                observation["packet_error"] = str(error)
                if isinstance(error, subprocess.CalledProcessError):
                    observation["stdout"] = error.stdout
                    observation["stderr"] = error.stderr
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        receipt["fault_assertions_passed"] = True
    except BaseException as error:
        receipt["fault_error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        try:
            namespace("ip", "link", "set", "dev", "provider0", "up")
            receipt["link_restored"] = time.time()
            wait_binding(lab, preferred)
            deadline = time.monotonic() + 60
            while True:
                try:
                    receipt["restored_packets"] = full_mtu_packets(lab, endpoints)
                    break
                except (AssertionError, subprocess.CalledProcessError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.2)
            receipt["restored"] = state()
            assert {"UP", "LOWER_UP"} <= set(receipt["restored"]["links"]["provider0"]["flags"])
            assert receipt["restored"]["pids"] == receipt["before"]["pids"]
        finally:
            write_atomic(directory / "receipt.json", json.dumps(receipt, indent=2) + "\n", 0o600)


@pytest.mark.parametrize("failed", ("nlab-gw-r1", "nlab-gw-r2"))
def test_whole_gateway_outage_preserves_floating_ip_traffic(gateways, failed):
    lab, model, endpoints, _ = gateways
    preferred = max(model["gateways"], key=lambda n: model["gateways"][n]["priority"])
    survivor = next(n for n in model["gateways"] if n != failed)
    expected = survivor if failed == preferred else preferred
    wait_binding(lab, preferred)
    directory = ROOT / f"runtime/gateways/fault-{failed}-{time.time_ns()}"
    directory.mkdir(mode=0o700, parents=True)
    processes = []
    flows = []
    receipt = {
        "failed": failed,
        "expected_active": expected,
        "started": time.time(),
        "flows": flows,
    }
    with ExitStack() as files:

        def output(name):
            path = directory / name
            path.touch(mode=0o600)
            return files.enter_context(path.open("w"))

        try:
            for index, (name, config) in enumerate(endpoints.items()):
                prefix = [
                    "lxc",
                    "exec",
                    "--project",
                    lab.PROJECT,
                    "fabric",
                    "--",
                    "ip",
                    "netns",
                    "exec",
                    "nlab-provider-r1",
                    "stdbuf",
                    "-oL",
                    "ping",
                    "-n",
                    "-D",
                    "-O",
                    "-M",
                    "do",
                    "-s",
                    "1372",
                    "-e",
                    str(47000 + index),
                    "-i",
                    "0.1",
                    "-w",
                    "30",
                    "-W",
                    "1",
                    config["floating"],
                ]
                process = subprocess.Popen(
                    prefix, stdout=output(name + ".txt"), stderr=output(name + ".err"), text=True
                )
                processes.append(process)
                flows.append({"name": name, "destination": config["floating"]})
            deadline = time.monotonic() + 10
            while not all(
                replies((directory / (f["name"] + ".txt")).read_text(), f["destination"])
                for f in flows
            ):
                assert all(p.poll() is None for p in processes) and time.monotonic() < deadline
                time.sleep(0.1)
            receipt["stop_before"] = time.time()
            operate(lab, failed, "--stop")
            receipt["stop_after"] = time.time()
            print(f"{failed}: gateway role stopped; waiting for {expected}", flush=True)
            receipt["binding_observed"] = wait_binding(lab, expected)
            receipt["surviving_packets"] = full_mtu_packets(lab, endpoints)
            for process in processes:
                process.communicate(timeout=35)
                assert process.returncode == 0
            for flow in flows:
                text = (directory / (flow["name"] + ".txt")).read_text()
                stamps = replies(text, flow["destination"])
                assert len(stamps) > 2 and stamps[-1] - stamps[0] >= 25
                counts = re.search(r"(\d+) packets transmitted, (\d+) received", text)
                assert counts is not None
                flow.update(
                    transmitted=int(counts[1]),
                    received=int(counts[2]),
                    max_inter_reply_gap=max(b - a for a, b in pairwise(stamps)),
                    reply_trace=text,
                )
            receipt["fault_assertions_passed"] = True
        except BaseException as error:
            receipt["fault_error"] = {"type": type(error).__name__, "message": str(error)}
            raise
        finally:
            try:
                operate(lab, failed, "--apply")
                receipt["restored_binding"] = wait_binding(lab, preferred)
                try:
                    receipt["restored_packets"] = full_mtu_packets(lab, endpoints)
                except (subprocess.CalledProcessError, AssertionError):
                    receipt["immediate_restoration_failed"] = True
                    deadline = time.monotonic() + 45
                    while True:
                        try:
                            receipt["eventual_restored_packets"] = full_mtu_packets(lab, endpoints)
                            receipt["eventual_restoration_observed"] = time.time()
                            break
                        except (subprocess.CalledProcessError, AssertionError):
                            if time.monotonic() >= deadline:
                                break
                            time.sleep(0.2)
                    if "fault_error" not in receipt:
                        raise
                receipt["passed"] = receipt.get("fault_assertions_passed", False)
                print(f"{failed}: preferred binding and full-MTU traffic restored", flush=True)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=35)
                write_atomic(
                    directory / "receipt.json", json.dumps(receipt, indent=2) + "\n", 0o600
                )
