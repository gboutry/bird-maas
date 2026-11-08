"""Generate ten logical role attachments without inventing physical VF/NUMA data."""

import argparse
import json
from collections.abc import Iterable
from copy import deepcopy
from ipaddress import IPv4Interface, IPv4Network
from pathlib import Path
from typing import TypedDict

import yaml
from commission import HOSTS, ROOT
from maas_bird.models import RoutingDomain
from maas_bird.runtime import write_atomic


class RecipeDumper(yaml.SafeDumper):
    pass


def multiline(dumper, value):
    return dumper.represent_scalar(
        "tag:yaml.org,2002:str", value, style="|" if "\n" in value else None
    )


RecipeDumper.add_representer(str, multiline)


class NativeBinding(TypedDict):
    machine_id: str
    macs: dict[str, str]


def roles():
    result = json.loads((ROOT / "maas/network-model/logical-roles.json").read_text())["roles"]
    if {r["slot"] for r in result} != set(range(5)) or len(result) != 5:
        raise ValueError("Exactly five logical role slots are required")
    if len({r["id"] for r in result}) != 5 or {r["vlan"] for r in result} != set(
        range(3400, 3405)
    ):
        raise ValueError("Role identities and VLAN allocations must be unique")
    return result


def address(name, role):
    block = IPv4Network(role["address_block"])
    if block.prefixlen != 24 or not block.subnet_of(IPv4Network("10.205.0.0/16")):
        raise ValueError("Role pools must be reserved laboratory /24s")
    slot = int(IPv4Interface(HOSTS[name]["loopback"]).ip) & 255
    return str(block.network_address + slot) + "/32"


def local_address(name, role, side):
    slot = int(IPv4Interface(HOSTS[name]["loopback"]).ip) & 255
    return f"fe80::a0:{role['slot']:x}:{slot * 2 + side:x}/64"


def netplan(name):
    allocation = roles()
    interfaces = ("enp7s0", "enp8s0")
    return {
        "network": {
            "version": 2,
            "ethernets": {
                "lo": {
                    "match": {"name": "lo"},
                    "accept-ra": False,
                    "link-local": [],
                    "optional": True,
                    "addresses": [address(name, role) for role in allocation],
                    "routes": [
                        {
                            "to": "0.0.0.0/0",
                            "type": "unreachable",
                            "table": r["table"],
                            "metric": 42760,
                        }
                        for r in allocation
                    ]
                    + [
                        {
                            "to": "10.205.0.0/16",
                            "type": "unreachable",
                            "table": 254,
                            "metric": 42760,
                        }
                    ],
                    "routing-policy": [
                        {
                            "from": address(name, r),
                            "table": r["table"],
                            "priority": r["rule_priority"],
                        }
                        for r in allocation
                    ],
                },
                **{
                    i: {"accept-ra": False, "link-local": [], "optional": True} for i in interfaces
                },
            },
            "vlans": {
                f"{interface}.{r['vlan']}": {
                    "id": r["vlan"],
                    "link": interface,
                    "accept-ra": False,
                    "optional": True,
                    "link-local": ["ipv6"] if r["id"] == "general" else [],
                    **(
                        {"addresses": [local_address(name, r, side)]}
                        if r["id"] != "general"
                        else {}
                    ),
                }
                for r in allocation
                for side, interface in enumerate(interfaces)
            },
        }
    }


def configurations(
    members: Iterable[str] | None = None, *, bindings: dict[str, NativeBinding] | None = None
):
    member_names = set[str](HOSTS) if members is None else set(members)
    if not member_names or not member_names <= set(HOSTS):
        raise ValueError("Role membership must name enrolled workers")
    if bindings is not None and set(bindings) != member_names:
        raise ValueError("Native identity bindings must cover exactly the selected members")
    allocation = roles()
    bird = yaml.safe_load((ROOT / "juju/bird/general.yaml").read_text())
    native = yaml.safe_load((ROOT / "juju/bird/attachments.yaml").read_text())
    result, attachments, nodes = {}, {}, {}
    for app, config in bird.items():
        observer = app.replace("bird-", "nic-", 1)
        base = json.loads(native[observer]["attachments"])
        names = {a["id"].rsplit("-gp-", 1)[0] for a in base}
        if len(names) != 1 or len(base) != 2:
            raise ValueError("Require one existing, admitted GP pair per worker")
        name = names.pop()
        if name not in HOSTS or {a["uplink"] for a in base} != {"enp7s0", "enp8s0"}:
            raise ValueError("The base GP pair does not match the worker allocation")
        if name not in member_names:
            continue
        base.sort(key=lambda a: a["uplink"])
        if bindings is not None:
            binding = bindings[name]
            if set(binding["macs"]) != {"enp7s0", "enp8s0"}:
                raise ValueError("Native identity binding requires both fast parents")
            for parent in base:
                parent["identity"] = {
                    "scheme": "permanent-mac",
                    "value": binding["macs"][parent["uplink"]],
                }
        general = json.loads(config["routing-domains"])
        if len(general) != 1 or general[0]["id"] != "general" or len(general[0]["contexts"]) != 1:
            raise ValueError("Require the verified single-context GP recipe")
        machine_id = (
            bindings[name]["machine_id"]
            if bindings is not None
            else general[0]["contexts"][0]["machine_id"]
        )
        general[0]["contexts"][0]["machine_id"] = machine_id
        domain_rows = [deepcopy(general[0])]
        attachment_rows = []
        node = {}
        for role in allocation:
            ids = []
            for parent in base:
                row = deepcopy(parent)
                tor = parent["id"].rsplit("-", 1)[-1]
                row.update(
                    {
                        "id": parent["id"]
                        if role["id"] == "general"
                        else f"{name}-{role['id']}-{tor}",
                        "domain": role["domain"],
                        "vlan": role["vlan"],
                    }
                )
                ids.append(row["id"])
                attachment_rows.append(row)
            node[role["id"]] = {
                **role,
                "address": address(name, role),
                "attachment_ids": ids,
                "numa_node": None,
            }
        for domain in ("ceph-access", "ceph-replication"):
            selected = [r for r in allocation if r["domain"] == domain]
            row = {
                "id": domain,
                "kernel_ecmp_limit": 2,
                "import_prefixes": sorted(
                    address(n, r)
                    for n in HOSTS
                    if n in member_names and n != name
                    for r in selected
                ),
                "export_allowlist": [address(name, r) for r in selected],
                "contexts": [
                    {
                        "id": r["context"],
                        "machine_id": machine_id,
                        "numa_node": None,
                        "attachment_ids": node[r["id"]]["attachment_ids"],
                        "table": r["table"],
                        "rule_priority": r["rule_priority"],
                        "sources": [address(name, r)],
                    }
                    for r in selected
                ],
            }
            RoutingDomain.model_validate(row)
            domain_rows.append(row)
        result[app] = {
            **config,
            "enabled": False,
            "routing-domains": json.dumps(domain_rows, indent=2),
        }
        attachments[observer] = {
            "backend": "native-nic",
            "attachments": json.dumps(attachment_rows, indent=2),
        }
        nodes[name] = node
    if set(nodes) != member_names:
        raise ValueError("Generated role recipes must cover exactly the declared members")
    return result, attachments, nodes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--members", nargs="+", choices=tuple(HOSTS))
    parser.add_argument(
        "--output", type=Path, help="Write a staged recipe under ignored runtime storage"
    )
    args = parser.parse_args()
    if args.members is not None and args.output is None:
        parser.error("Explicit member recipes require a runtime --output directory")
    output = args.output.resolve() if args.output else None
    if output is not None:
        if not output.is_relative_to((ROOT / "runtime").resolve()):
            parser.error("Staged recipes must stay under runtime storage")
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
    bird, native, nodes = configurations(args.members)
    mode = 0o600 if output else 0o644
    write_atomic(
        output / "all-roles.yaml" if output else ROOT / "juju/bird/all-roles.yaml",
        yaml.dump(bird, Dumper=RecipeDumper, sort_keys=False),
        mode,
    )
    write_atomic(
        output / "all-attachments.yaml" if output else ROOT / "juju/bird/all-attachments.yaml",
        yaml.dump(native, Dumper=RecipeDumper, sort_keys=False),
        mode,
    )
    write_atomic(
        output / "role-addresses.json"
        if output
        else ROOT / "maas/network-model/role-addresses.json",
        json.dumps(nodes, indent=2) + "\n",
        mode,
    )
    directory = output / "role-netplan" if output else ROOT / "maas/network-model/role-netplan"
    directory.mkdir(exist_ok=True)
    for name in nodes:
        write_atomic(
            directory / (name + ".yaml"), yaml.safe_dump(netplan(name), sort_keys=False), mode
        )
    if output:
        write_atomic(
            output / "membership.json",
            json.dumps({"members": sorted(nodes)}, indent=2) + "\n",
            mode,
        )
    print(f"Generated 3 domains, 5 contexts and 10 attachments for each of {len(nodes)} members")


if __name__ == "__main__":
    main()
