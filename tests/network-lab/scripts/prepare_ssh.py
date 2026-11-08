"""Prepare scoped SSH configuration and trust native keys through owned LXD VMs."""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time

from commission import HOSTS, PROJECT, ROOT, command
from maas_bird.runtime import write_atomic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("controller", "workers", "all"), default="controller")
    args = parser.parse_args()
    os.umask(0o077)
    runtime = ROOT / "runtime"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = runtime / "juju-bootstrap-ssh.conf"
    blocks = []
    nodes = {
        "juju": (
            "10.203.240.12",
            runtime / "juju-controller-key",
            runtime / "juju-controller-known-hosts",
        )
    }
    for name, host in HOSTS.items():
        base = runtime if name == "probe" else runtime / name
        known = base / ("deployed-known-hosts" if name == "probe" else "loopback32-known-hosts")
        nodes[name] = (host["management_ip"], base / "ssh-key", known)
    if not config.exists():
        for address, key, known in nodes.values():

            def quoted(path):
                return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'

            blocks.append(
                f"Host {address}\n    IdentityFile {quoted(key)}\n"
                "    IdentitiesOnly yes\n    StrictHostKeyChecking yes\n"
                f"    UserKnownHostsFile {quoted(known)}\n    ControlMaster no\n"
            )
        write_atomic(config, "\n".join(blocks), 0o600)
    if config.is_symlink() or config.stat().st_uid != os.geteuid():
        raise RuntimeError("Lab SSH configuration must be an owned regular file")
    config.chmod(0o600)
    records = []
    if args.scope in ("controller", "all"):
        instance = json.loads(command("lxc", "query", f"/1.0/instances/juju?project={PROJECT}"))
        if (
            instance["type"] != "virtual-machine"
            or instance["config"].get("user.maas-bird-owner") != "network-lab"
            or instance["config"]["limits.cpu"] != "2"
            or instance["config"]["limits.memory"] != "4GiB"
        ):
            raise RuntimeError("SSH bootstrap requires the owned, capped controller VM")
        prefix = ("lxc", "exec", "--project", PROJECT, "juju", "--")
        native = command(*prefix, "cat", "/sys/class/dmi/id/product_uuid")
        if native != instance["config"]["volatile.uuid"]:
            raise RuntimeError("Controller native UUID differs from the owned LXD VM")
        public = command(*prefix, "cat", "/etc/ssh/ssh_host_ed25519_key.pub").split()
        if len(public) < 2 or public[0] != "ssh-ed25519":
            raise RuntimeError("Expected a native Ed25519 controller host key")
        address, key, expected_known = nodes["juju"]
        options = command("/usr/bin/ssh", "-G", "-F", str(config), address).splitlines()
        values = {}
        for line in options:
            name, _, value = line.partition(" ")
            values.setdefault(name, []).append(value)
        if (
            values["stricthostkeychecking"] not in (["true"], ["yes"])
            or values["identitiesonly"] not in (["true"], ["yes"])
            or values["controlmaster"] not in (["false"], ["no"])
            or values["identityfile"] != [str(key)]
            or shlex.split(values["userknownhostsfile"][0]) != [str(expected_known)]
        ):
            raise RuntimeError("Existing controller SSH profile differs from the scoped policy")
        if not key.is_file() or key.stat().st_mode & 0o077:
            raise RuntimeError("Controller private key must exist and be private")
        write_atomic(expected_known, address + " " + " ".join(public[:2]) + "\n", 0o600)
        ssh_uuid = command(
            "/usr/bin/ssh",
            "-F",
            str(config),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "ubuntu@" + address,
            "sudo cat /sys/class/dmi/id/product_uuid",
        )
        if ssh_uuid != native:
            raise RuntimeError("Strict controller SSH UUID differs from the LXD channel")
        records.append({"name": "juju", "uuid": native, "strict_ssh_verified": True})
        print("Controller native host key and strict SSH UUID verified", flush=True)
    if args.scope in ("workers", "all"):
        for name in HOSTS:
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/trust_guest.py"), "--name", name],
                check=True,
                timeout=120,
            )
            records.append({"name": name, "native_trust_helper_completed": True})
    combined = runtime / "juju-native-known-hosts"
    trusted = []
    for address, _, known in nodes.values():
        if known.exists():
            for row in known.read_text().splitlines():
                fields = row.split()
                if fields and fields[0] == address:
                    if len(fields) != 3 or fields[1] != "ssh-ed25519":
                        raise RuntimeError("Expected a native Ed25519 scoped host-key entry")
                    trusted.append(row)
    write_atomic(combined, "\n".join(trusted) + "\n", 0o600)
    wrapper = runtime / "juju-bootstrap-bin"
    wrapper.mkdir(mode=0o700, exist_ok=True)
    options = [
        "/usr/bin/ssh",
        "-F",
        str(config),
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "UserKnownHostsFile=" + str(combined),
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "HostKeyAlgorithms=ssh-ed25519",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
    ]
    write_atomic(wrapper / "ssh", "#!/bin/sh\nexec " + shlex.join(options) + ' "$@"\n', 0o700)
    effective = command(
        str(wrapper / "ssh"),
        "-G",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "10.203.240.12",
    )
    if (
        "stricthostkeychecking true\n" not in effective
        or "userknownhostsfile " + str(combined) + "\n" not in effective
    ):
        raise RuntimeError("Scoped SSH wrapper did not retain its explicit trust policy")
    write_atomic(
        runtime / f"ssh-setup-{time.time_ns()}.json",
        json.dumps({"scope": args.scope, "records": records, "completed": time.time()}, indent=2)
        + "\n",
        0o600,
    )


if __name__ == "__main__":
    main()
