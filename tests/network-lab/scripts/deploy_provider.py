"""Prepare or apply provider L2 namespaces inside the owned fabric VM.

Apply requires the compiled EVPN intent and established core EVPN neighbors.
The resulting endpoints test provider transport, not OVN gateway operation.
"""

import argparse
import json
import re
import time

from commission import PROJECT, ROOT, command
from maas_bird.runtime import write_atomic
from render_host_fabric import NODES

DIRECTORY = "/opt/network-lab/provider-src"


def guest(*args):
    return command("lxc", "exec", "--project", PROJECT, "fabric", "--", *args)


def established_evpn_peers(node, expected, timeout=120):
    deadline = time.monotonic() + timeout
    while True:
        summary = json.loads(
            guest(
                "docker",
                "exec",
                "clab-maas-bird-fabric-" + node,
                "vtysh",
                "-c",
                "show bgp l2vpn evpn summary json",
            )
        )
        peers = summary["peers"]
        if set(peers) == set(expected) and all(
            row["state"] == "Established" and row["remoteAs"] == int(expected[ip])
            for ip, row in peers.items()
        ):
            return summary
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Native EVPN core peers did not converge to the declared allocation: {node}"
            )
        time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    instance = json.loads(command("lxc", "query", f"/1.0/instances/fabric?project={PROJECT}"))
    if (
        instance["type"] != "virtual-machine"
        or instance["config"].get("user.maas-bird-owner") != "network-lab"
        or instance["config"]["limits.cpu"] != "2"
        or instance["config"]["limits.memory"] != "8GiB"
    ):
        raise RuntimeError("Provider setup requires the owned, capped fabric VM")
    model = json.loads((ROOT / "containerlab/provider.json").read_text())
    receipt = {"apply_requested": args.apply, "started": time.time(), "model": model}
    if args.apply:
        manifest = json.loads((ROOT / "runtime/host-fabric/public-plan.json").read_text())
        if manifest.get("provider_intent") != model:
            raise RuntimeError("Apply the matching native-validated EVPN fabric first")
        families = {}
        for node in NODES:
            core = (ROOT / f"containerlab/frr/{node}.conf").read_text()
            expected = dict(
                re.findall(r"^ neighbor ([0-9.]+) remote-as ([0-9]+)$", core, re.MULTILINE)
            )
            families[node] = established_evpn_peers(node, expected)
        receipt["evpn_peers"] = families
    guest("install", "-d", "-m", "700", DIRECTORY)
    for local, target in (
        (ROOT / "containerlab/provider.json", DIRECTORY + "/provider.json"),
        (
            ROOT / "containerlab/bootstrap/provider_dataplane.py",
            DIRECTORY + "/provider_dataplane.py",
        ),
    ):
        command(
            "lxc",
            "file",
            "push",
            "--project",
            PROJECT,
            str(local),
            "fabric" + target,
            "--mode=0600",
            "--uid=0",
            "--gid=0",
        )
    receipt["native_result"] = guest(
        "python3",
        DIRECTORY + "/provider_dataplane.py",
        "--model",
        DIRECTORY + "/provider.json",
        *(("--apply",) if args.apply else ()),
    )
    if args.apply:
        command(
            "lxc",
            "file",
            "push",
            "--project",
            PROJECT,
            str(ROOT / "containerlab/bootstrap/network-lab-provider.service"),
            "fabric/etc/systemd/system/network-lab-provider.service",
            "--mode=0644",
            "--uid=0",
            "--gid=0",
        )
        guest("systemctl", "daemon-reload")
        guest("systemctl", "enable", "network-lab-provider.service")
        guest("systemctl", "restart", "network-lab-provider.service")
        receipt["service"] = guest("systemctl", "is-active", "network-lab-provider.service")
        if receipt["service"] != "active":
            raise RuntimeError("Native provider persistence service is not active")
    receipt["completed"] = time.time()
    output = ROOT / "runtime/provider"
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_atomic(
        output / f"setup-{time.time_ns()}.json", json.dumps(receipt, indent=2) + "\n", 0o600
    )
    print(receipt["native_result"])


if __name__ == "__main__":
    main()
