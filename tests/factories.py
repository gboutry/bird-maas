"""Fresh routing inputs shared by fixtures and generated tests; no pytest lifecycle."""

import dataclasses
import json
from ipaddress import IPv6Address
from pathlib import Path
from typing import Any

import yaml
from maas_bird.discovery import LocalLink
from maas_bird.models import Advertisements, Attachments, Identity, Inventory

ROOT = Path(__file__).resolve().parents[1]


def make_inputs():
    """Build a complete single-domain host on two cards, using charm config defaults."""
    inventory = Inventory.model_validate(
        {
            "hosts": [
                {
                    "machine_id": "abc123",
                    "system_uuid": "host-uuid",
                    "rack": "a",
                    "local_asn": 4200001011,
                    "router_id": "10.255.11.11",
                    "peers": [
                        {"id": "tor1", "chassis_id": "aa:aa:aa:aa:aa:01", "asn": 65011},
                        {"id": "tor2", "chassis_id": "aa:aa:aa:aa:aa:02", "asn": 65012},
                    ],
                }
            ]
        }
    )
    attachments = []
    links = []
    for n in [1, 2]:
        attachments.append(
            Attachments.model_validate(
                {
                    "cards": [
                        {
                            "identity": {"scheme": "devlink-serial", "value": f"card{n}"},
                            "attachments": [
                                {
                                    "id": f"path{n}",
                                    "function": {"controller": 0, "pf": 0, "vf": 63},
                                    "domain": "storage-public",
                                    "vlan": 3414,
                                    "chassis_id": f"aa:aa:aa:aa:aa:0{n}",
                                    "peer_link_local": "fe80::1",
                                    "ready": True,
                                }
                            ],
                        }
                    ]
                }
            )
        )
        links.append(
            LocalLink(
                f"fabric{n}",
                Identity(scheme="devlink-serial", value=f"card{n}"),
                0,
                0,
                63,
                3414,
                IPv6Address("fe80::11"),
            )
        )
    ads = [
        Advertisements.model_validate(
            {
                "advertisements": [
                    {
                        "machine_id": "abc123",
                        "domain": "storage-public",
                        "prefix": "10.60.11.11/32",
                        "ready": True,
                    }
                ]
            }
        )
    ]
    config = {
        key: value.get("default")
        for key, value in yaml.safe_load((ROOT / "charms/bird/charmcraft.yaml").read_text())[
            "config"
        ]["options"].items()
    }
    config.update(
        {
            "routing-domain": "storage-public",
            "kernel-table": 3100,
            "routing-contexts": "[]",
            "import-prefixes": "10.60.11.0/24,10.60.12.0/24",
            "export-allowlist": "10.60.11.0/24",
        }
    )
    return dict(
        uid="host-uuid",
        inventory=inventory,
        attachments=attachments,
        advertisements=ads,
        local_links=links,
        config=config,
    )


def with_numa_contexts(inputs):
    """Add node-specific source/table bindings to a caller-owned fresh fixture."""
    inputs["local_links"] = [
        dataclasses.replace(link, numa_node=n) for n, link in enumerate(inputs["local_links"])
    ]
    contexts = [
        dict(
            id=f"numa{n}",
            machine_id="abc123",
            numa_node=n,
            table=3100 + n,
            rule_priority=10000 + n * 100,
            sources=[f"10.60.11.{11 + n}/32"],
        )
        for n in range(2)
    ]
    inputs["config"]["routing-contexts"] = json.dumps(contexts)
    inputs["config"]["protected-prefixes"] = "172.20.0.0/24,172.21.0.0/24"
    inputs["advertisements"] = [
        Advertisements.model_validate(
            {
                "advertisements": [
                    dict(
                        machine_id="abc123",
                        domain="storage-public",
                        prefix=f"10.60.11.{11 + n}/32",
                        context=f"numa{n}",
                        ready=True,
                    )
                    for n in range(2)
                ]
            }
        )
    ]
    return inputs


def with_routing_domains(numa_inputs):
    """Two domains on separate verified functions of the same two host cards."""

    data = numa_inputs
    data["config"]["import-prefixes"] = "203.0.113.0/24"
    public = json.loads(data["config"]["routing-contexts"])
    replica = []
    for n, context in enumerate(public):
        replica.append(
            context
            | {
                "table": 3200 + n,
                "rule_priority": 11000 + n * 100,
                "sources": [f"172.22.0.{11 + n}/32"],
            }
        )
    data["config"]["routing-contexts"] = "[]"
    data["config"]["routing-domains"] = json.dumps(
        [
            {
                "id": "storage-public",
                "import_prefixes": ["203.0.113.0/24"],
                "export_allowlist": ["10.60.11.0/24"],
                "contexts": public,
                "bfd_min_tx_ms": 50,
                "bfd_min_rx_ms": 50,
            },
            {
                "id": "storage-replica",
                "import_prefixes": ["198.51.100.0/24"],
                "export_allowlist": ["172.22.0.0/24"],
                "contexts": replica,
                "bfd_min_tx_ms": 300,
                "bfd_min_rx_ms": 300,
            },
        ]
    )
    for index, doc in enumerate(data["attachments"]):
        raw = doc.model_dump(mode="json")
        attachment = raw["cards"][0]["attachments"][0]
        replica_attachment = attachment | {
            "id": f"replica-path{index + 1}",
            "domain": "storage-replica",
            "vlan": 3415,
            "function": {"controller": 0, "pf": 0, "vf": 62},
        }
        raw["cards"][0]["attachments"].append(replica_attachment)
        data["attachments"][index] = Attachments.model_validate(raw)
        data["local_links"].append(
            dataclasses.replace(
                data["local_links"][index], interface=f"replica{index + 1}", vf=62, vlan=3415
            )
        )
    raw = data["advertisements"][0].model_dump(mode="json")
    raw["advertisements"].extend(
        [
            {
                "machine_id": "abc123",
                "domain": "storage-replica",
                "context": f"numa{n}",
                "prefix": f"172.22.0.{11 + n}/32",
                "ready": True,
            }
            for n in range(2)
        ]
    )
    data["advertisements"] = [Advertisements.model_validate(raw)]
    return data


def rack_inputs(inputs=None):
    """Explicit production fixture: one rack, multi-domain intent, scoped producers."""
    data = inputs or with_routing_domains(with_numa_contexts(make_inputs()))
    if not json.loads(data["config"]["routing-domains"]):
        contexts = json.loads(data["config"]["routing-contexts"])
        if not contexts:
            data = with_numa_contexts(data)
            contexts = json.loads(data["config"]["routing-contexts"])
        data["config"]["routing-domains"] = json.dumps(
            [
                {
                    "id": data["config"]["routing-domain"],
                    "import_prefixes": data["config"]["import-prefixes"].split(","),
                    "export_allowlist": data["config"]["export-allowlist"].split(","),
                    "contexts": contexts,
                }
            ]
        )
    data["config"].update(rack="a", **{"routing-contexts": "[]"})
    data["inventory"] = data["inventory"].model_copy(update={"rack": "a"})
    data["attachments"] = [doc.model_copy(update={"rack": "a"}) for doc in data["attachments"]]
    advertisements = []
    grouped = {}
    for doc in data["advertisements"]:
        for ad in doc.advertisements or ():
            grouped.setdefault(ad.domain, []).append(ad)
    for domain, rows in grouped.items():
        advertisements.append(
            Advertisements(
                rack="a", machine_id="abc123", domain=domain, advertisements=tuple(rows)
            )
        )
    data["advertisements"] = advertisements
    for key in (
        "routing-domain",
        "kernel-table",
        "routing-contexts",
        "import-prefixes",
        "export-allowlist",
    ):
        data["config"].pop(key, None)
    return data


def ten_vf_inputs():
    """Five VFs on each card: four dedicated Ceph paths and one neutral path.

    Each Ceph context gets its own two-interface failure domain. General traffic
    has one pair and no NUMA preference. These are independent allocated inputs,
    not observations of any production host.
    """
    data = rack_inputs()
    domains = json.loads(data["config"]["routing-domains"])
    domains.append(
        dict(
            id="general",
            import_prefixes=["192.168.51.0/24"],
            export_allowlist=["192.168.50.0/24"],
            contexts=[
                dict(
                    id="any-numa",
                    machine_id="abc123",
                    numa_node=None,
                    table=3300,
                    rule_priority=12000,
                    sources=["192.168.50.11/32"],
                )
            ],
        )
    )
    cards: list[dict[str, Any]] = [
        dict(identity={"scheme": "devlink-serial", "value": f"card{n + 1}"}, attachments=[])
        for n in range(2)
    ]
    data["local_links"] = []
    ads = []
    vf = 55
    for domain in domains:
        records = []
        for context in domain["contexts"]:
            stem = f"{domain['id']}-{context['id']}"
            context["attachment_ids"] = [f"{stem}-tor{n + 1}" for n in range(2)]
            for n in range(2):
                name = f"vf{vf}c{n}"
                vlan = 3500 + vf - 55
                attachment = dict(
                    id=f"{stem}-tor{n + 1}",
                    domain=domain["id"],
                    function={"controller": 0, "pf": 0, "vf": vf},
                    vlan=vlan,
                    chassis_id=f"aa:aa:aa:aa:aa:0{n + 1}",
                    peer_link_local="fe80::1",
                    ready=True,
                )
                cards[n]["attachments"].append(attachment)
                data["local_links"].append(
                    LocalLink(
                        name,
                        Identity(scheme="devlink-serial", value=f"card{n + 1}"),
                        0,
                        0,
                        vf,
                        vlan,
                        IPv6Address(f"fe80::{n + 11}"),
                        n,
                    )
                )
            records.extend(
                dict(
                    machine_id="abc123",
                    domain=domain["id"],
                    context=context["id"],
                    prefix=source,
                    ready=True,
                )
                for source in context["sources"]
            )
            vf += 1
        ads.append(
            Advertisements.model_validate(
                dict(rack="a", machine_id="abc123", domain=domain["id"], advertisements=records)
            )
        )
    data["config"]["routing-domains"] = json.dumps(domains)
    data["attachments"] = [
        Attachments.model_validate(dict(rack="a", cards=[card])) for card in cards
    ]
    data["advertisements"] = ads
    return data
