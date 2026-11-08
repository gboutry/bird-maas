"""Compile a seed fabric; --apply installs it only on an empty owned fabric VM.

The seed provides native ToR LLDP before Juju can produce accepted host plans.
It advertises only switch loopbacks. Host routing, provider L2 and gateways
are later deployment stages with separate native acceptance.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from bootstrap import owned, run
from commission import HOSTS, PROJECT, ROOT, command
from maas_bird.runtime import write_atomic
from render_host_fabric import NODES, base_configs

CURRENT = "/opt/network-lab/host-fabric"


def guest(*args):
    return command("lxc", "exec", "--project", PROJECT, "fabric", "--", *args)


def prerequisites():
    owned("fabric", 2, "8GiB")
    for carrier in ("nlab-probe-pf0", "nlab-probe-pf1", "nlab-r2-pf0", "nlab-r2-pf1"):
        if not int(Path(f"/sys/class/net/{carrier}/bridge/group_fwd_mask").read_text(), 0) & 16384:
            raise RuntimeError("Host LLDP forwarding prerequisite is missing: " + carrier)
    run(
        "lxc",
        "exec",
        "--project",
        PROJECT,
        "fabric",
        "--",
        "cloud-init",
        "status",
        "--wait",
        timeout=1800,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument(
        "--check-prerequisites",
        action="store_true",
        help="Check owned fabric bootstrap and host LLDP readiness without configuring it",
    )
    args = parser.parse_args()
    os.umask(0o077)
    if args.check_prerequisites:
        prerequisites()
        print(
            "Owned fabric bootstrap and host LLDP prerequisites passed; no configuration changed"
        )
        return
    fabric_uuid = owned("fabric", 2, "8GiB")
    identities = {name: owned(name, 2, "4GiB") for name in HOSTS}
    directory = ROOT / f"runtime/fabric-seed-{time.time_ns()}"
    (directory / "frr").mkdir(mode=0o700, parents=True)
    configs = base_configs()
    for node, config in configs.items():
        write_atomic(directory / f"frr/{node}.conf", config, 0o600)
    for name in ("daemons", "Dockerfile", "entrypoint.sh"):
        write_atomic(
            directory / "frr" / name, (ROOT / "containerlab/frr" / name).read_text(), 0o600
        )
    write_atomic(
        directory / "topology.clab.yml",
        (ROOT / "containerlab/topology.clab.yml").read_text(),
        0o600,
    )
    manifest = {
        "origins": [],
        "origin_racks": {},
        "worker_uuids": identities,
        "peers": {
            node: {
                ip: {"asn": int(asn)}
                for ip, asn in re.findall(
                    r"^ neighbor ([0-9.]+) remote-as ([0-9]+)$", config, re.MULTILINE
                )
            }
            for node, config in configs.items()
        },
        "expected_routes": {
            node: sorted(
                f"10.205.250.{number}/32" for other, number in NODES.items() if other != node
            )
            for node in NODES
        },
    }
    write_atomic(directory / "public-plan.json", json.dumps(manifest, indent=2) + "\n", 0o600)
    receipt = {"fabric_uuid": fabric_uuid, "apply_requested": args.apply, "compiled": time.time()}
    write_atomic(directory / "receipt.json", json.dumps(receipt, indent=2) + "\n", 0o600)
    if not args.apply:
        print("Seed compiled without guest changes: " + str(directory))
        return
    installed = json.loads(
        guest(
            "python3",
            "-c",
            "import json,pathlib; print(json.dumps([p for p in "
            f"({CURRENT!r},'/etc/systemd/system/network-lab-provider.service',"
            "'/etc/systemd/system/network-lab-gateways.service') if pathlib.Path(p).exists()]))",
        )
    )
    if installed:
        raise RuntimeError(
            "Seed bootstrap refuses an installed fabric; retain its compiled service stages"
        )
    prerequisites()
    containers = guest("docker", "ps", "-aq", "--filter", "label=containerlab=maas-bird-fabric")
    if containers:
        raise RuntimeError("Seed bootstrap refuses existing topology containers")
    run(sys.executable, str(ROOT / "scripts/wire_fabric.py"))
    guest("install", "-d", "-m", "700", CURRENT, CURRENT + "/frr")
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.name != "receipt.json":
            run(
                "lxc",
                "file",
                "push",
                "--project",
                PROJECT,
                str(path),
                "fabric" + CURRENT + "/" + str(path.relative_to(directory)),
                "--mode=0600",
            )
    run(
        "lxc",
        "exec",
        "--project",
        PROJECT,
        "fabric",
        "--",
        "docker",
        "build",
        "-t",
        "network-lab-frr:10.7.1",
        CURRENT + "/frr",
    )
    for node in NODES:
        result = subprocess.run(
            [
                "lxc",
                "exec",
                "--project",
                PROJECT,
                "fabric",
                "--",
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "vtysh",
                "--mount",
                f"type=bind,src={CURRENT}/frr/{node}.conf,dst=/tmp/parser.conf,readonly",
                "network-lab-frr:10.7.1",
                "-C",
                "-f",
                "/tmp/parser.conf",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        if result.stdout.strip() or result.stderr.strip():
            raise RuntimeError("Native seed FRR parser reported diagnostics")
    run(
        "lxc",
        "file",
        "push",
        "--project",
        PROJECT,
        str(ROOT / "containerlab/bootstrap/network-lab-fabric.service"),
        "fabric/etc/systemd/system/network-lab-fabric.service",
        "--mode=0644",
    )
    guest("systemctl", "daemon-reload")
    guest("systemctl", "enable", "--now", "network-lab-fabric")
    receipt["service_started"] = time.time()
    write_atomic(directory / "receipt.json", json.dumps(receipt, indent=2) + "\n", 0o600)
    print("Seed fabric service started; routing and packet acceptance remain required")


if __name__ == "__main__":
    main()
