"""Fresh policy binds native machines to explicit, manually managed racks."""

import importlib
from pathlib import Path
from typing import TypedDict

import pytest


class Machine(TypedDict):
    system_id: str
    hardware_uuid: str
    tag_names: list[str]


@pytest.fixture
def policy(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent / "scripts"))
    return importlib.import_module("prepare_policy")


def machines(policy) -> dict[str, Machine]:
    return {
        name: {"system_id": name, "hardware_uuid": name + "-uuid", "tag_names": ["virtual"]}
        for name in policy.HOSTS
    }


def test_fresh_assignments_are_verified_and_reused(policy, monkeypatch):
    nodes = machines(policy)
    tags = set()
    mutations = []

    def maas(*args):
        if args == ("tags", "read"):
            return [{"name": tag, "definition": ""} for tag in sorted(tags)]
        if args[:2] == ("machine", "read"):
            return nodes[args[2]]
        mutations.append(args)
        if args[:2] == ("tags", "create"):
            tags.add(args[2].removeprefix("name="))
        else:
            assert args[:2] == ("tag", "update-nodes")
            nodes[args[3].removeprefix("add=")]["tag_names"].append(args[2])

    monkeypatch.setattr(policy, "maas", maas)
    policy.ensure_rack_tags(nodes)
    assert tags == {"rack-r1", "rack-r2"}
    assert len(mutations) == 6
    for name, node in nodes.items():
        assert node["tag_names"] == ["virtual", f"rack-r{policy.HOSTS[name]['rack']}"]
    policy.ensure_rack_tags(nodes)
    assert len(mutations) == 6


def test_conflicting_rack_rejects_before_any_mutation(policy, monkeypatch):
    nodes = machines(policy)
    nodes["probe-r2b"]["tag_names"].append("rack-r1")
    monkeypatch.setattr(policy, "maas", lambda *args: pytest.fail("Unexpected MAAS call"))
    with pytest.raises(RuntimeError, match="Conflicting native rack assignment"):
        policy.ensure_rack_tags(nodes)


def test_dynamic_rack_tag_rejects_before_creating_missing_tag(policy, monkeypatch):
    def maas(*args):
        assert args == ("tags", "read")
        return [{"name": "rack-r2", "definition": "//node"}]

    monkeypatch.setattr(policy, "maas", maas)
    with pytest.raises(RuntimeError, match="manually managed"):
        policy.ensure_rack_tags(machines(policy))


def test_replaced_machine_is_rejected_on_native_readback(policy, monkeypatch):
    nodes = machines(policy)

    def maas(*args):
        if args == ("tags", "read"):
            return [{"name": tag, "definition": ""} for tag in ("rack-r1", "rack-r2")]
        if args[:2] == ("tag", "update-nodes"):
            return None
        assert args[:2] == ("machine", "read")
        return {**nodes[args[2]], "hardware_uuid": "replacement", "tag_names": ["rack-r1"]}

    monkeypatch.setattr(policy, "maas", maas)
    with pytest.raises(RuntimeError, match="did not match the owned worker"):
        policy.ensure_rack_tags(nodes)
