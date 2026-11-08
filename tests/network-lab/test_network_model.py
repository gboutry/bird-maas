"""Check MAAS-global address uniqueness and independent rack/PF carriers."""

import base64
import importlib
import json
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
HOSTS = json.loads((ROOT / "maas/network-model/hosts.json").read_text())


def test_four_host_addresses_do_not_overlap():
    assert len(HOSTS) == 4
    assert [host["rack"] for host in HOSTS.values()].count(1) == 2
    assert [host["rack"] for host in HOSTS.values()].count(2) == 2
    management = [IPv4Address(host["management_ip"]) for host in HOSTS.values()]
    assert len(set(management)) == 4
    for address in management:
        assert address in IPv4Network("10.203.240.0/24")
        assert address not in IPv4Network("10.203.240.96/27")
        assert not 100 <= int(str(address).split(".")[-1]) <= 199
    fast = [
        IPv4Interface(cidr).network
        for host in HOSTS.values()
        for model in ("interface32", "point31")
        for cidr in host[model]
    ]
    fast.extend(IPv4Interface(host["loopback"]).network for host in HOSTS.values())
    for index, subnet in enumerate(fast):
        assert not any(subnet.overlaps(other) for other in fast[index + 1 :])


@pytest.mark.parametrize("name", HOSTS)
def test_deployment_loopback_source_guard_and_management_separation(name, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    deploy = importlib.import_module("deploy_probe")
    encoded = deploy.loopback_user_data(name, tmp_path).removeprefix("user_data=")
    cloud = json.loads(base64.b64decode(encoded).decode().split("\n", 1)[1])
    assert cloud["runcmd"] == [["netplan", "apply"]]
    assert len(cloud["write_files"]) == 1
    lo = json.loads(cloud["write_files"][0]["content"])["network"]["ethernets"]["lo"]
    assert lo["addresses"] == [HOSTS[name]["loopback"]]
    assert lo["routing-policy"] == [
        {"from": HOSTS[name]["loopback"], "table": 4400, "priority": 14000}
    ]
    assert lo["routes"] == [
        {"to": "0.0.0.0/0", "type": "unreachable", "table": 4400, "metric": 42760}
    ]
    assert IPv4Interface(lo["addresses"][0]).ip != IPv4Address(HOSTS[name]["management_ip"])
    network = json.loads(cloud["write_files"][0]["content"])["network"]
    for parent in ("enp7s0", "enp8s0"):
        assert network["ethernets"][parent] == {
            "accept-ra": False,
            "link-local": [],
            "optional": True,
        }
        assert network["vlans"][parent + ".3404"] == {
            "accept-ra": False,
            "link-local": ["ipv6"],
            "optional": True,
        }
    assert lo["accept-ra"] is False
    assert lo["optional"] is True


@pytest.mark.parametrize("name", HOSTS)
def test_all_role_deployment_keeps_parent_dependencies_and_source_guards(
    name, tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    deploy = importlib.import_module("deploy_probe")
    encoded = deploy.loopback_user_data(name, tmp_path, logical_roles=True).removeprefix(
        "user_data="
    )
    cloud = json.loads(base64.b64decode(encoded).decode().split("\n", 1)[1])
    network = json.loads(cloud["write_files"][0]["content"])["network"]
    assert cloud["packages"] == ["lldpd"]
    assert cloud["write_files"][1] == {
        "path": "/etc/default/lldpd",
        "permissions": "0644",
        "owner": "root:root",
        "content": (ROOT / "juju/bird/lldpd.default").read_text(),
    }
    assert cloud["runcmd"] == [
        ["netplan", "apply"],
        ["systemctl", "enable", "lldpd"],
        ["systemctl", "restart", "lldpd"],
    ]
    assert "bonds" not in network and "bridges" not in network
    assert set(network["vlans"]) == {
        f"{parent}.{vid}" for parent in ("enp7s0", "enp8s0") for vid in range(3400, 3405)
    }
    for parent in ("enp7s0", "enp8s0"):
        children = [v for v in network["vlans"].values() if v["link"] == parent]
        assert len(children) == 5
        assert {v["id"] for v in children} == set(range(3400, 3405))
    lo = network["ethernets"]["lo"]
    assert len(lo["addresses"]) == 5
    assert len(set(lo["addresses"])) == 5
    assert HOSTS[name]["loopback"] in lo["addresses"]
    assert {r["from"] for r in lo["routing-policy"]} == set(lo["addresses"])
    assert {r["table"] for r in lo["routing-policy"]} == {4100, 4101, 4200, 4201, 4400}
    assert len({r["priority"] for r in lo["routing-policy"]}) == 5
    assert {r["table"] for r in lo["routes"] if r["to"] == "0.0.0.0/0"} == {
        4100,
        4101,
        4200,
        4201,
        4400,
    }
    assert all(r["type"] == "unreachable" for r in lo["routes"])
    assert any(r["table"] == 254 and r["to"] == "10.205.0.0/16" for r in lo["routes"])
    local_addresses = [
        address for vlan in network["vlans"].values() for address in vlan.get("addresses", [])
    ]
    assert len(local_addresses) == len(set(local_addresses)) == 8


def test_storage_imports_include_every_remote_role_and_exclude_local_origins(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    recipe = importlib.import_module("generate_roles")
    bird, _, _ = recipe.configurations()
    for name, suffix in (
        ("probe", "r1a"),
        ("probe-alt", "r1b"),
        ("probe-r2a", "r2a"),
        ("probe-r2b", "r2b"),
    ):
        domains = json.loads(bird["bird-" + suffix]["routing-domains"])
        for domain in domains:
            if domain["id"] == "general":
                continue
            selected = [r for r in recipe.roles() if r["domain"] == domain["id"]]
            assert set(domain["import_prefixes"]) == {
                recipe.address(other, role)
                for other in HOSTS
                if other != name
                for role in selected
            }
            assert len(domain["import_prefixes"]) == 6
            assert set(domain["import_prefixes"]).isdisjoint(domain["export_allowlist"])


def test_explicit_storage_membership_retains_all_routes_between_declared_members(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    recipe = importlib.import_module("generate_roles")
    members = {"probe", "probe-r2a"}
    bird, native, nodes = recipe.configurations(members)
    assert set(nodes) == members
    assert set(bird) == {"bird-r1a", "bird-r2a"}
    assert set(native) == {"nic-r1a", "nic-r2a"}
    for name, app in (("probe", "bird-r1a"), ("probe-r2a", "bird-r2a")):
        domains = json.loads(bird[app]["routing-domains"])
        for domain in domains:
            if domain["id"] == "general":
                continue
            selected = [r for r in recipe.roles() if r["domain"] == domain["id"]]
            assert set(domain["import_prefixes"]) == {
                recipe.address(other, role)
                for other in members
                if other != name
                for role in selected
            }
            assert len(domain["import_prefixes"]) == 2
    with pytest.raises(ValueError, match="enrolled"):
        recipe.configurations({"not-a-lab-worker"})


def test_role_recipe_binds_every_context_and_parent_to_fresh_enrollment(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    recipe = importlib.import_module("generate_roles")
    bindings = {
        name: {
            "machine_id": "fresh" + str(index),
            "macs": {
                "enp7s0": f"02:00:00:01:00:{index:02x}",
                "enp8s0": f"02:00:00:02:00:{index:02x}",
            },
        }
        for index, name in enumerate(HOSTS, 1)
    }
    bird, native, _ = recipe.configurations(bindings=bindings)
    for name, suffix in zip(HOSTS, ("r1a", "r1b", "r2a", "r2b"), strict=True):
        domains = json.loads(bird["bird-" + suffix]["routing-domains"])
        assert len(domains) == 3
        assert {c["machine_id"] for d in domains for c in d["contexts"]} == {
            bindings[name]["machine_id"]
        }
        attachments = json.loads(native["nic-" + suffix]["attachments"])
        assert len(attachments) == 10
        for attachment in attachments:
            assert attachment["identity"] == {
                "scheme": "permanent-mac",
                "value": bindings[name]["macs"][attachment["uplink"]],
            }
    with pytest.raises(ValueError, match="exactly"):
        recipe.configurations(bindings={"probe": bindings["probe"]})


@pytest.mark.parametrize("drift", ("extra-worker", "uuid", "machine-id"))
def test_runtime_policy_refuses_foreign_or_stale_enrollment(monkeypatch, tmp_path, drift):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    policy = importlib.import_module("lab_policy")
    monkeypatch.setattr(policy, "ROOT", tmp_path)
    record = {"worker_uuids": {}, "bindings": {}}
    for index, name in enumerate(HOSTS):
        directory = tmp_path / "runtime" if name == "probe" else tmp_path / "runtime" / name
        directory.mkdir(parents=True, exist_ok=True)
        enrollment = {"lxd_uuid": "owned-" + str(index), "system_id": "machine-" + str(index)}
        (directory / "commission.json").write_text(json.dumps(enrollment))
        record["worker_uuids"][name] = enrollment["lxd_uuid"]
        record["bindings"][name] = {"machine_id": enrollment["system_id"]}
    directory = tmp_path / "runtime/policy"
    directory.mkdir()
    manifest = directory / "bindings.json"
    manifest.write_text(json.dumps(record))
    assert policy.bindings() == record["bindings"]
    if drift == "extra-worker":
        record["worker_uuids"]["fabric"] = "outside-worker-cohort"
    elif drift == "uuid":
        record["worker_uuids"]["probe"] = "different-vm"
    else:
        record["bindings"]["probe"]["machine_id"] = "different-maas-machine"
    manifest.write_text(json.dumps(record))
    with pytest.raises(RuntimeError):
        policy.bindings()


@pytest.mark.parametrize("name", HOSTS)
def test_declared_gateway_paths_follow_rack_and_surviving_parent(name, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    expectations = importlib.import_module("routing_expectations")
    model = json.loads((ROOT / "containerlab/gateways.json").read_text())
    monkeypatch.setattr(expectations, "gateway_intent", lambda: model)
    paths = expectations.general_paths(name)
    assert len(paths) == 5
    for other, host in HOSTS.items():
        if other != name:
            assert paths[host["loopback"].split("/")[0]] == {"enp7s0.3404", "enp8s0.3404"}
    for gateway in model["gateways"].values():
        address = gateway["vtep"].split("/")[0]
        expected = (
            {"enp7s0.3404"}
            if gateway["rack"] == HOSTS[name]["rack"]
            else {"enp7s0.3404", "enp8s0.3404"}
        )
        assert paths[address] == expected
    for parent in ("enp7s0", "enp8s0"):
        assert all(
            devices == {parent + ".3404"}
            for devices in expectations.general_paths(name, parents=(parent,)).values()
        )
    assert all(not devices for devices in expectations.general_paths(name, parents=()).values())


def test_gateway_stage_requires_exact_tracked_intent(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    expectations = importlib.import_module("routing_expectations")
    monkeypatch.setattr(expectations, "ROOT", tmp_path)
    assert expectations.gateway_intent() is None
    manifest = tmp_path / "runtime/host-fabric/public-plan.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}\n")
    assert expectations.gateway_intent() is None
    model = json.loads((ROOT / "containerlab/gateways.json").read_text())
    (tmp_path / "containerlab").mkdir()
    (tmp_path / "containerlab/gateways.json").write_text(json.dumps(model))
    manifest.write_text(json.dumps({"gateway_intent": model}))
    assert expectations.gateway_intent() == model
    model["gateways"]["nlab-gw-r1"]["tor"] = "r2b"
    manifest.write_text(json.dumps({"gateway_intent": model}))
    with pytest.raises(RuntimeError, match="differs from the tracked topology"):
        expectations.gateway_intent()
