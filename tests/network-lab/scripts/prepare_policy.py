"""Bind declarative role recipes to natively identified MAAS workers.

Fresh LXD instances receive new MACs and MAAS machine IDs. These bindings stay
in private runtime storage; tracked recipes supply role, rack and ASN intent.
"""

import json
import os
import re
import time

import deploy_ovn as lab
import yaml
from commission import HOSTS, ROOT, maas
from generate_roles import NativeBinding, configurations
from maas_bird.runtime import write_atomic


def ensure_rack_tags(machines):
    required = {}
    for name, machine in machines.items():
        tag = f"rack-r{HOSTS[name]['rack']}"
        existing = [t for t in machine.get("tag_names", []) if t.startswith("rack-")]
        if existing and existing != [tag]:
            raise RuntimeError("Conflicting native rack assignment: " + name)
        required[name] = tag
    tags = {t["name"]: t for t in maas("tags", "read")}
    if any(tags.get(tag, {}).get("definition") for tag in required.values()):
        raise RuntimeError("Lab rack assignments require manually managed MAAS tags")
    for tag in sorted(set(required.values())):
        if tag not in tags:
            maas("tags", "create", "name=" + tag)
    for name, tag in required.items():
        machine = machines[name]
        if tag not in machine.get("tag_names", []):
            maas("tag", "update-nodes", tag, "add=" + machine["system_id"])
        observed = maas("machine", "read", machine["system_id"])
        if observed["hardware_uuid"] != machine["hardware_uuid"] or [
            t for t in observed.get("tag_names", []) if t.startswith("rack-")
        ] != [tag]:
            raise RuntimeError("Native MAAS rack assignment did not match the owned worker")


def main():
    os.umask(0o077)
    owned = lab.owned()
    identities = {name: owned[name] for name in HOSTS}
    bindings: dict[str, NativeBinding] = {}
    machines = {}
    for name in HOSTS:
        runtime = ROOT / "runtime" if name == "probe" else ROOT / "runtime" / name
        enrollment = json.loads((runtime / "commission.json").read_text())
        machine = maas("machine", "read", enrollment["system_id"])
        if (
            enrollment["lxd_uuid"] != identities[name]
            or machine["hardware_uuid"] != identities[name]
            or machine["status_name"] != "Deployed"
            or machine["hostname"] != name
            or lab.ssh(name, "sudo", "cat", "/sys/class/dmi/id/product_uuid") != identities[name]
        ):
            raise RuntimeError("Policy requires matching native MAAS/LXD/SSH identity")
        interfaces = {i["name"]: i for i in machine["interface_set"]}
        macs = {}
        for parent in ("enp7s0", "enp8s0"):
            text = lab.ssh(name, "sudo", "ethtool", "-P", parent)
            match = re.fullmatch(r"Permanent address: ([0-9a-f]{2}(?::[0-9a-f]{2}){5})", text)
            if match is None or match[1] != interfaces[parent]["mac_address"]:
                raise RuntimeError("Native permanent MAC differs from commissioned MAAS identity")
            macs[parent] = match[1]
        if len(set(macs.values())) != 2:
            raise RuntimeError("The two fast parents require distinct native identities")
        bindings[name] = {"machine_id": machine["system_id"], "macs": macs}
        machines[name] = machine
    ensure_rack_tags(machines)
    bird, attachments, _ = configurations(bindings=bindings)
    inventory = yaml.safe_load((ROOT / "juju/bird/inventory.yaml").read_text())["inventory"]
    allocations = {i["router_id"]: i for i in json.loads(inventory["host-identities"])}
    inventory["host-identities"] = json.dumps(
        [
            {
                **allocations[host["loopback"].split("/")[0]],
                "machine_id": bindings[name]["machine_id"],
            }
            for name, host in HOSTS.items()
        ]
    )
    output = ROOT / "runtime/policy"
    output.mkdir(mode=0o700, exist_ok=True)
    for name, content in (
        ("all-roles.yaml", bird),
        ("all-attachments.yaml", attachments),
        ("inventory.yaml", {"inventory": inventory}),
    ):
        write_atomic(output / name, yaml.safe_dump(content), 0o600)
    write_atomic(
        output / "bindings.json",
        json.dumps(
            {"prepared": time.time(), "worker_uuids": identities, "bindings": bindings}, indent=2
        )
        + "\n",
        0o600,
    )
    print("Prepared native role identities for all four workers; no Juju configuration changed")


if __name__ == "__main__":
    main()
