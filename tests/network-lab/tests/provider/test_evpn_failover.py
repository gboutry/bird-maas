"""Withdraw one EVPN address-family peer while probing provider and NAT traffic."""

import importlib
import ipaddress
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


def reply_rows(text, address):
    return [
        (float(t), int(s))
        for t, s in re.findall(
            rf"(?m)^\[(\d+\.\d+)\].* bytes from {re.escape(address)}: icmp_seq=(\d+)", text
        )
    ]


def af_lines(guest, tor, peer):
    # Filter inside the guest: other running-config sections contain credentials.
    script = """import json,subprocess,sys
node,peer=sys.argv[1:]
text=subprocess.check_output(["docker","exec","clab-maas-bird-fabric-"+node,
"vtysh","-c","show running-config"],text=True)
inside=False; rows=[]
for line in text.splitlines():
 line=line.strip()
 if line=="address-family l2vpn evpn": inside=True
 elif line=="exit-address-family": inside=False
 elif inside and line.startswith("neighbor "+peer+" "):
  if "password" in line: raise RuntimeError("Unexpected authentication in EVPN AF")
  rows.append(line)
print(json.dumps(rows))"""
    return json.loads(guest("python3", "-c", script, tor, peer))


@pytest.mark.parametrize("tor", TORS)
@pytest.mark.parametrize("peer_index", (0, 1))
def test_one_evpn_peer_loss_preserves_provider_and_gateway_packets(
    provider, gateways, tor, peer_index
):
    model, guest = provider
    lab, _, endpoints, _ = gateways
    host = importlib.import_module("host_acceptance")
    manifest = json.loads((ROOT / "runtime/host-fabric/public-plan.json").read_text())
    core = {
        p: v for p, v in manifest["peers"][tor].items() if ipaddress.ip_address(p).version == 4
    }
    assert len(core) == 2
    peer = sorted(core, key=ipaddress.ip_address)[peer_index]
    config = (ROOT / f"containerlab/frr/{tor}.conf").read_text()
    asn = re.search(r"^router bgp ([0-9]+)$", config, re.MULTILINE)
    assert asn is not None
    original = af_lines(guest, tor, peer)
    activate = "neighbor " + peer + " activate"
    assert activate in original and len(set(original)) == len(original)

    def router(*args):
        return guest("docker", "exec", "clab-maas-bird-fabric-" + tor, *args)

    def configure(*commands):
        return router(
            "vtysh",
            "-c",
            "configure terminal",
            "-c",
            "router bgp " + asn[1],
            "-c",
            "address-family l2vpn evpn",
            *sum((["-c", c] for c in commands), []),
            "-c",
            "exit-address-family",
            "-c",
            "end",
        )

    def health(disabled=False):
        evpn = json.loads(router("vtysh", "-c", "show bgp l2vpn evpn summary json"))
        expected = set(core) - ({peer} if disabled else set())
        assert evpn["as"] == int(asn[1]) and set(evpn["peers"]) == expected
        assert all(p["state"] == "Established" for p in evpn["peers"].values())
        ipv4 = json.loads(router("vtysh", "-c", "show bgp summary json"))["ipv4Unicast"]
        assert set(ipv4["peers"]) == set(manifest["peers"][tor])
        assert all(p["state"] == "Established" for p in ipv4["peers"].values())
        return {"evpn": evpn, "ipv4": ipv4}

    def packets():
        results = {}
        for external in model["endpoints"]:
            other = next(e for n, e in model["endpoints"].items() if n != external)
            destinations = [(str(ipaddress.ip_interface(other["address"]).ip), "1472")]
            destinations += [(e["floating"], "1372") for e in endpoints.values()]
            for address, payload in destinations:
                text = guest(
                    "ip",
                    "netns",
                    "exec",
                    external,
                    "ping",
                    "-n",
                    "-M",
                    "do",
                    "-s",
                    payload,
                    "-c",
                    "3",
                    "-i",
                    "0.2",
                    "-W",
                    "1",
                    address,
                )
                assert "3 packets transmitted, 3 received, 0% packet loss" in text, text
                results[external + "->" + address] = text
        return results

    before = host.wait_for(health, timeout=45)
    before_packets = host.wait_for(packets, timeout=45)
    directory = ROOT / f"runtime/evpn-failures/{tor}-{peer_index}-{time.time_ns()}"
    directory.mkdir(mode=0o700, parents=True)
    receipt = {
        "tor": tor,
        "peer": peer,
        "started": time.time(),
        "original_af_lines": original,
        "before": before,
        "before_packets": before_packets,
        "flows": [],
    }
    processes = []
    changed = False
    with ExitStack() as files:
        try:
            for external, cfg in model["endpoints"].items():
                other = next(e for n, e in model["endpoints"].items() if n != external)
                targets = [(str(ipaddress.ip_interface(other["address"]).ip), "1472")]
                target_name = "probe" if cfg["tor"].startswith("r2") else "probe-r2a"
                targets.append((endpoints[target_name]["floating"], "1372"))
                for address, payload in targets:
                    index = len(processes)
                    output = directory / f"{index}.txt"
                    errors = directory / f"{index}.err"
                    output.touch(mode=0o600)
                    errors.touch(mode=0o600)
                    args = [
                        "lxc",
                        "exec",
                        "--project",
                        lab.PROJECT,
                        "fabric",
                        "--",
                        "ip",
                        "netns",
                        "exec",
                        external,
                        "stdbuf",
                        "-oL",
                        "ping",
                        "-n",
                        "-D",
                        "-O",
                        "-M",
                        "do",
                        "-s",
                        payload,
                        "-e",
                        str(56000 + index),
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
                    receipt["flows"].append(
                        {
                            "external": external,
                            "destination": address,
                            "payload": int(payload),
                            "index": index,
                        }
                    )
            deadline = time.monotonic() + 10
            while not all(
                reply_rows((directory / f"{f['index']}.txt").read_text(), f["destination"])
                for f in receipt["flows"]
            ):
                assert all(p.poll() is None for p in processes) and time.monotonic() < deadline
                time.sleep(0.1)
            receipt["disable_before"] = time.time()
            changed = True
            configure("no " + activate)
            receipt["disable_after"] = time.time()
            receipt["surviving"] = host.wait_for(lambda: health(True), timeout=45)
            receipt["convergence_observed"] = time.time()
            receipt["surviving_packets"] = host.wait_for(packets, timeout=45)
            for process in processes:
                process.communicate(timeout=55)
                assert process.returncode == 0
            for flow in receipt["flows"]:
                text = (directory / f"{flow['index']}.txt").read_text()
                rows = reply_rows(text, flow["destination"])
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
                if changed:
                    configure(*(line for line in original if line != activate), activate)
                receipt["restored"] = host.wait_for(health, timeout=45)
                assert set(af_lines(guest, tor, peer)) == set(original)
                receipt["restored_packets"] = host.wait_for(packets, timeout=45)
                receipt["passed"] = receipt.get("fault_assertions_passed", False)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=55)
                write_atomic(
                    directory / "receipt.json", json.dumps(receipt, indent=2) + "\n", 0o600
                )
