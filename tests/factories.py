"""Fresh routing inputs shared by fixtures and generated tests; no pytest lifecycle."""

import dataclasses
import json
from ipaddress import IPv6Address
from pathlib import Path

import yaml
from maas_bird.discovery import LocalLink
from maas_bird.models import Advertisements, Attachments, Identity, Inventory

ROOT = Path(__file__).resolve().parents[1]


def make_inputs():
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
        {"import-prefixes": "10.60.11.0/24,10.60.12.0/24", "export-allowlist": "10.60.11.0/24"}
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
