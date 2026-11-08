"""Fresh provisioning initializes distinct rack fabrics before machine changes."""

import importlib
from pathlib import Path
from typing import TypedDict

import pytest


class Vlan(TypedDict):
    id: int
    vid: int


class Fabric(TypedDict):
    id: int
    name: str
    vlans: list[Vlan]


@pytest.fixture
def deployment(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent / "scripts"))
    return importlib.import_module("deploy")


def test_missing_routing_domains_create_four_fabrics_and_gp_vlans(deployment, monkeypatch):
    fabrics: list[Fabric] = [{"id": 0, "name": "fabric-0", "vlans": [{"id": 1, "vid": 0}]}]

    def maas(*args):
        if args == ("fabrics", "read"):
            return fabrics
        if args[:2] == ("fabrics", "create"):
            number = len(fabrics)
            fabric: Fabric = {
                "id": number,
                "name": args[2].removeprefix("name="),
                "vlans": [{"id": 10 * number, "vid": 0}],
            }
            fabrics.append(fabric)
            return fabric
        assert args[:2] == ("vlans", "create")
        fabric = next(f for f in fabrics if str(f["id"]) == args[2])
        vlan: Vlan = {"id": 10 * fabric["id"] + 1, "vid": 3404}
        fabric["vlans"].append(vlan)
        return vlan

    monkeypatch.setattr(deployment, "maas", maas)
    first = deployment.ensure_routing_fabrics()
    assert {(f["rack"], f["tor"]) for f in first} == {(1, "a"), (1, "b"), (2, "a"), (2, "b")}
    assert len({f["fabric_id"] for f in first}) == 4
    assert len(fabrics) == 5
    assert deployment.ensure_routing_fabrics() == first
    assert len(fabrics) == 5
    assert all(len(f["vlans"]) == 2 for f in fabrics[1:])


def test_existing_compatibility_fabrics_are_reused_without_mutations(deployment, monkeypatch):
    fabrics = [
        {
            "id": i,
            "name": f"compat-known-r{rack}-{tor}",
            "vlans": [{"id": 10 * i, "vid": 0}, {"id": 10 * i + 1, "vid": 3404}],
        }
        for i, (rack, tor) in enumerate(((1, "a"), (1, "b"), (2, "a"), (2, "b")), 1)
    ]

    def maas(*args):
        assert args == ("fabrics", "read")
        return fabrics

    monkeypatch.setattr(deployment, "maas", maas)
    assert {f["fabric_id"] for f in deployment.ensure_routing_fabrics()} == {1, 2, 3, 4}


def test_ambiguous_domain_refuses_before_creating_missing_fabrics(deployment, monkeypatch):
    fabrics = [{"id": i, "name": f"compat-{i}-r2-b", "vlans": []} for i in (1, 2)]

    def maas(*args):
        assert args == ("fabrics", "read")
        return fabrics

    monkeypatch.setattr(deployment, "maas", maas)
    with pytest.raises(RuntimeError, match="Ambiguous MAAS routing fabric"):
        deployment.ensure_routing_fabrics()
