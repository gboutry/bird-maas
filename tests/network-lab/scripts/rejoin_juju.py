"""Reallocate an actually redeployed worker using Juju's native SSH provider.

An explicit stale-record replacement retains the LXD VM. Other colocated
applications prevent replacement. Existing agent provisioning is observed,
never restarted merely because an observation deadline expires.
"""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

import deploy_ovn as lab
import jubilant
from commission import maas
from juju_workers import worker_binding
from maas_bird.runtime import write_atomic
from render_host_fabric import WORKERS

MODEL = "network-lab:network-lab"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", choices=tuple(lab.HOSTS), required=True)
    parser.add_argument("--replace-stale", action="store_true")
    parser.add_argument(
        "--agents-only",
        action="store_true",
        help="Allocate or verify native agents before applications are deployed",
    )
    args = parser.parse_args()
    name = args.name
    runtime = lab.ROOT / "runtime"
    node_runtime = runtime if name == "probe" else runtime / name
    enrollment = json.loads((node_runtime / "commission.json").read_text())
    instance = json.loads(
        lab.command("lxc", "query", f"/1.0/instances/{name}?project={lab.PROJECT}")
    )
    machine = maas("machine", "read", enrollment["system_id"])
    identity = enrollment["lxd_uuid"]
    if (
        instance["type"] != "virtual-machine"
        or instance["config"].get("user.maas-bird-owner") != "network-lab"
        or instance["config"]["limits.cpu"] != "2"
        or instance["config"]["limits.memory"] != "4GiB"
        or instance["config"]["volatile.uuid"] != identity
        or machine["status_name"] != "Deployed"
        or machine["hardware_uuid"] != identity
        or machine["hostname"] != name
        or lab.ssh(name, "sudo", "cat", "/sys/class/dmi/id/product_uuid") != identity
    ):
        raise RuntimeError("Reallocation requires the owned, natively identified MAAS deployment")
    juju = jubilant.Juju(model=MODEL)
    apps = {"bird-" + WORKERS[name], "nic-" + WORKERS[name]}
    endpoint = "manual:" + lab.HOSTS[name]["management_ip"]

    def status():
        return json.loads(juju.cli("status", "--format=json"))

    def matching(current):
        found = [
            k for k, v in current.get("machines", {}).items() if v.get("instance-id") == endpoint
        ]
        if len(found) > 1:
            raise RuntimeError("Ambiguous manual machine identity")
        return found

    current = status()
    old = matching(current)
    native_agents = json.loads(
        lab.ssh(
            name,
            "sudo",
            "python3",
            "-c",
            "import json,pathlib; print(json.dumps([p.name for p in "
            'pathlib.Path("/var/lib/juju/agents").glob("machine-*")]))',
        )
    )
    retired = None
    if old and not native_agents:
        units = {
            u
            for a in current.get("applications", {}).values()
            for u, v in a.get("units", {}).items()
            if v.get("machine") == old[0]
        }
        if units:
            if not args.replace_stale:
                raise RuntimeError("Use --replace-stale to retire the wiped worker's record")
            if len(units) != 2 or {u.split("/")[0] for u in units} != apps:
                raise RuntimeError(
                    "Refusing to retire a machine with other colocated applications"
                )
            retired = old[0]
            juju.cli(
                "remove-machine", retired, "--force", "--no-wait", "--keep-instance", "--no-prompt"
            )
            deadline = time.monotonic() + 180
            while retired in status()["machines"]:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Stale machine removal is incomplete; observe it before retrying"
                    )
                time.sleep(3)
            old = []
    if not old:
        if native_agents:
            raise RuntimeError("Native agent state exists without its manual machine record")
        address = lab.HOSTS[name]["management_ip"]
        config = lab.command(
            "/usr/bin/ssh", "-T", "-G", "-F", str(runtime / "juju-bootstrap-ssh.conf"), address
        )
        keys = [
            Path(row.partition(" ")[2]).resolve()
            for row in config.splitlines()
            if row.startswith("identityfile ")
        ]
        if (
            len(keys) != 1
            or not keys[0].is_relative_to(runtime.resolve())
            or keys[0].stat().st_mode & 0o077
        ):
            raise RuntimeError("Expected one private, lab-scoped SSH identity")
        environment = dict(os.environ)
        environment["PATH"] = (
            str(runtime / "juju-bootstrap-bin") + os.pathsep + environment["PATH"]
        )
        subprocess.run(
            [
                "/snap/juju/current/bin/juju",
                "add-machine",
                "-m",
                MODEL,
                "ssh:ubuntu@" + address,
                "--private-key",
                str(keys[0]),
            ],
            env=environment,
            check=True,
            timeout=600,
        )
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        current = status()
        found = matching(current)
        if (
            found
            and current["machines"][found[0]].get("juju-status", {}).get("current") == "started"
        ):
            break
        time.sleep(5)
    else:
        raise TimeoutError("Manual agent is not ready; its existing record has been retained")
    selected = found[0]
    if (
        juju.exec("sudo", "cat", "/sys/class/dmi/id/product_uuid", machine=selected).stdout.strip()
        != identity
    ):
        raise RuntimeError("Native Juju machine UUID differs from MAAS and LXD")
    for app in sorted(apps if not args.agents_only else set()):
        units = current["applications"][app].get("units", {})
        if units:
            if worker_binding(juju, name, app).machine != selected:
                raise RuntimeError("Application is placed on another machine")
        else:
            juju.cli("add-unit", app, "--to", selected)
    receipt = {
        "name": name,
        "uuid": identity,
        "retired_machine": retired,
        "machine": selected,
        "applications": sorted(apps) if not args.agents_only else [],
        "agents_only": args.agents_only,
        "completed": time.time(),
        "boundary": "Native manual agent and unit placement; "
        "charm and forwarding acceptance remain separate",
    }
    write_atomic(
        node_runtime / f"juju-rejoin-{time.time_ns()}.json",
        json.dumps(receipt, indent=2) + "\n",
        0o600,
    )
    detail = "application placement deferred" if args.agents_only else "units placed"
    print(f"{name}: native agent on machine {selected}; {detail}, acceptance remains required")


if __name__ == "__main__":
    main()
