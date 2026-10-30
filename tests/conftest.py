import importlib.util
from ipaddress import IPv6Address
from pathlib import Path

import pytest
import yaml
from maas_bird.discovery import LocalLink
from maas_bird.models import Advertisements, Attachments, Identity, Inventory
from ops import testing

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def inputs():
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


@pytest.fixture
def charm_context():
    def create(name):
        path = ROOT / "charms" / name
        spec = importlib.util.spec_from_file_location(
            name.replace("-", "_") + "_charm", path / "src/charm.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls = {
            "bird": "BirdCharm",
            "maas-inventory": "MaasInventoryCharm",
            "dpu-attachments": "DpuAttachmentsCharm",
        }[name]
        metadata = yaml.safe_load((path / "charmcraft.yaml").read_text())
        return testing.Context(getattr(module, cls), meta=metadata)

    return create


@pytest.fixture
def numa_inputs(inputs):
    import dataclasses
    import json

    from maas_bird.models import Advertisements

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
