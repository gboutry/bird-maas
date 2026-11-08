"""Collect native guest observations without relaxing the strict live test suite."""

import argparse
import json
import subprocess
import time

from commission import HOSTS, PROJECT, ROOT, command, maas


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", choices=tuple(HOSTS), default="probe")
    parser.add_argument(
        "--model", choices=("interface32", "point31", "loopback32"), default="interface32"
    )
    args = parser.parse_args()
    runtime = ROOT / "runtime"
    if args.name != "probe":
        runtime = runtime / args.name
    receipt = json.loads((runtime / "commission.json").read_text())
    instance = json.loads(command("lxc", "query", f"/1.0/instances/{args.name}?project={PROJECT}"))
    machine = maas("machine", "read", receipt["system_id"])
    if (
        instance["config"].get("user.maas-bird-owner") != "network-lab"
        or instance["config"]["volatile.uuid"] != receipt["lxd_uuid"]
        or machine["hardware_uuid"] != receipt["lxd_uuid"]
        or machine["status_name"] != "Deployed"
    ):
        raise RuntimeError("Inspection requires the owned, MAAS-deployed VM")
    address = HOSTS[args.name]["management_ip"]
    ssh = [
        "ssh",
        "-i",
        str(runtime / "ssh-key"),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "UserKnownHostsFile=" + str(runtime / f"{args.model}-known-hosts"),
        "ubuntu@" + address,
    ]
    uuid = subprocess.check_output(
        [*ssh, "sudo cat /sys/class/dmi/id/product_uuid"], text=True, timeout=30
    ).strip()
    if uuid != receipt["lxd_uuid"]:
        raise RuntimeError("SSH native UUID does not match the owned VM")
    commands = {
        "cloud_init": "sudo cloud-init status --format json",
        "addresses": "ip -j address show",
        "links": "ip -j -d link show",
        "routes": "ip -j -4 route show table all",
        "rules": "ip -j -4 rule show",
        "bond": "cat /proc/net/bonding/bond0",
        "boot_id": "cat /proc/sys/kernel/random/boot_id",
        "versions": "uname -r; cloud-init --version",
        "netplan": "sudo python3 -c 'import json,pathlib; "
        "print(json.dumps({p.name:p.read_text() for p in "
        'pathlib.Path("/etc/netplan").glob("*.yaml")}))' + "'",
    }
    observations = {}
    for name, text in commands.items():
        result = subprocess.run([*ssh, text], text=True, capture_output=True, timeout=120)
        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError:
            output = result.stdout
        observations[name] = {
            "exit_code": result.returncode,
            "output": output,
            "stderr": result.stderr,
        }
    document = {
        "name": args.name,
        "model": args.model,
        "system_id": receipt["system_id"],
        "native_uuid": uuid,
        "captured_at": time.time(),
        "observations": observations,
        "boundary": "Read-only native observations. Nonzero statuses are retained. "
        "This does not mark the strict deployment test suite passed.",
    }
    path = runtime / f"observations-{args.model}.json"
    path.write_text(json.dumps(document, indent=2) + "\n")
    print(f"Saved native observations in {path}")


if __name__ == "__main__":
    main()
