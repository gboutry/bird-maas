"""Prepare or start dedicated logical gateway namespaces in the owned fabric.

This configures isolated OVS/OVN processes and their links. Logical router,
host route imports, NAT and end-to-end gateway acceptance remain separate.
"""

import argparse
import json
import os
import time

import deploy_ovn as lab
from commission import PROJECT, ROOT, command
from maas_bird.runtime import write_atomic

DIRECTORY = "/opt/network-lab/gateway-src"
TLS = "/etc/ovn/network-lab-gateways"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    instance = json.loads(command("lxc", "query", f"/1.0/instances/fabric?project={PROJECT}"))
    if (
        instance["type"] != "virtual-machine"
        or instance["config"].get("user.maas-bird-owner") != "network-lab"
        or instance["config"]["limits.cpu"] != "2"
        or instance["config"]["limits.memory"] != "8GiB"
    ):
        raise RuntimeError("Gateway setup requires the owned, capped fabric VM")
    model = json.loads((ROOT / "containerlab/gateways.json").read_text())
    if args.apply:
        manifest = json.loads((ROOT / "runtime/host-fabric/public-plan.json").read_text())
        if manifest.get("gateway_intent") != model:
            raise RuntimeError("Apply the matching compiled gateway transport first")
    certificates = ROOT / "runtime/ovn/tls"
    lab.certificates(certificates, {name: name for name in model["gateways"]})
    lab.fabric("install", "-d", "-m", "700", DIRECTORY, TLS)
    for source, destination in (
        (ROOT / "containerlab/gateways.json", DIRECTORY + "/gateways.json"),
        (
            ROOT / "containerlab/bootstrap/gateway_dataplane.py",
            DIRECTORY + "/gateway_dataplane.py",
        ),
    ):
        command(
            "lxc",
            "file",
            "push",
            "--project",
            PROJECT,
            str(source),
            "fabric" + destination,
            "--mode=0600",
            "--uid=0",
            "--gid=0",
        )
    for namespace in model["gateways"]:
        lab.fabric("install", "-d", "-m", "700", TLS + "/" + namespace)
        for source, name in (
            ("ca.crt", "ca.crt"),
            (namespace + ".key", "key.pem"),
            (namespace + ".crt", "cert.pem"),
        ):
            command(
                "lxc",
                "file",
                "push",
                "--project",
                PROJECT,
                str(certificates / source),
                "fabric" + TLS + "/" + namespace + "/" + name,
                "--mode=0600",
                "--uid=0",
                "--gid=0",
            )
    if (
        args.apply
        and lab.fabric(
            "python3",
            "-c",
            "from pathlib import Path; "
            "print(Path('/etc/systemd/system/network-lab-gateways.service').is_file())",
        )
        == "True"
    ):
        lab.fabric("systemctl", "stop", "network-lab-gateways.service")
    result = lab.fabric(
        "python3",
        DIRECTORY + "/gateway_dataplane.py",
        "--model",
        DIRECTORY + "/gateways.json",
        *(("--apply",) if args.apply else ()),
    )
    if args.apply:
        command(
            "lxc",
            "file",
            "push",
            "--project",
            PROJECT,
            str(ROOT / "containerlab/bootstrap/network-lab-gateways.service"),
            "fabric/etc/systemd/system/network-lab-gateways.service",
            "--mode=0644",
            "--uid=0",
            "--gid=0",
        )
        lab.fabric("systemctl", "daemon-reload")
        lab.fabric("systemctl", "enable", "network-lab-gateways.service")
        lab.fabric("systemctl", "start", "network-lab-gateways.service")
        if lab.fabric("systemctl", "is-active", "network-lab-gateways.service") != "active":
            raise RuntimeError("Gateway process service is not active")
    output = ROOT / "runtime/gateways"
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_atomic(
        output / f"nodes-{time.time_ns()}.json",
        json.dumps(
            {
                "apply_requested": args.apply,
                "model": model,
                "native_result": result,
                "completed": time.time(),
                "boundary": "Namespace links and isolated daemons; "
                "router and packet acceptance separate",
            },
            indent=2,
        )
        + "\n",
        0o600,
    )
    print(result)


if __name__ == "__main__":
    main()
