"""Enroll a redeployed guest's SSH host key through the owned LXD channel."""

import argparse
import json
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

import deploy_ovn as lab
from maas_bird.runtime import write_atomic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", choices=tuple(lab.HOSTS), required=True)
    args = parser.parse_args()
    name = args.name
    runtime = lab.ROOT / "runtime"
    node_runtime = runtime if name == "probe" else runtime / name
    enrollment = json.loads((node_runtime / "commission.json").read_text())
    instance = json.loads(
        lab.command("lxc", "query", f"/1.0/instances/{name}?project={lab.PROJECT}")
    )
    if (
        instance["config"].get("user.maas-bird-owner") != "network-lab"
        or instance["config"]["volatile.uuid"] != enrollment["lxd_uuid"]
    ):
        raise RuntimeError("SSH trust requires the enrolled, owned lab VM")
    native = lab.command(
        "lxc",
        "exec",
        "--project",
        lab.PROJECT,
        name,
        "--",
        "cat",
        "/sys/class/dmi/id/product_uuid",
    )
    if native != enrollment["lxd_uuid"]:
        raise RuntimeError("LXD guest UUID differs from the enrolled machine")
    public = lab.command(
        "lxc",
        "exec",
        "--project",
        lab.PROJECT,
        name,
        "--",
        "cat",
        "/etc/ssh/ssh_host_ed25519_key.pub",
    )
    fields = public.split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519":
        raise RuntimeError("Expected a native Ed25519 host key")
    key = " ".join(fields[:2])
    address = lab.HOSTS[name]["management_ip"]
    config = lab.command(
        "/usr/bin/ssh", "-G", "-F", str(runtime / "juju-bootstrap-ssh.conf"), address
    )
    hosts = shlex.split(
        next(
            line.partition(" ")[2]
            for line in config.splitlines()
            if line.startswith("userknownhostsfile ")
        )
    )
    if len(hosts) != 1:
        raise RuntimeError("Expected one lab-scoped known-hosts file")
    known = Path(hosts[0]).resolve()
    if not known.is_relative_to(runtime.resolve()):
        raise RuntimeError("Refusing SSH trust outside the lab runtime")
    with tempfile.TemporaryDirectory(dir=runtime) as temporary:
        candidate = Path(temporary) / "known-hosts"
        write_atomic(candidate, known.read_text() if known.exists() else "", 0o600)
        subprocess.run(
            ["ssh-keygen", "-R", address, "-f", str(candidate)], check=True, capture_output=True
        )
        write_atomic(known, candidate.read_text() + f"{address} {key}\n", 0o600)
    if lab.ssh(name, "sudo", "cat", "/sys/class/dmi/id/product_uuid") != native:
        raise RuntimeError("Strict SSH UUID differs from the LXD channel")
    fingerprint = subprocess.check_output(
        ["ssh-keygen", "-lf", "/dev/stdin"], input=key + "\n", text=True
    ).strip()
    receipt = {
        "name": name,
        "uuid": native,
        "address": address,
        "known_hosts": str(known),
        "fingerprint": fingerprint,
        "verified_at": time.time(),
    }
    write_atomic(
        node_runtime / f"ssh-trust-{time.time_ns()}.json",
        json.dumps(receipt, indent=2) + "\n",
        0o600,
    )
    print(f"{name}: native LXD host key and strict SSH UUID verified")


if __name__ == "__main__":
    main()
