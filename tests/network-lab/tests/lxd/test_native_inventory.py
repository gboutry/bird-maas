"""Native MAAS inventory, image and commissioning acceptance."""

import json
import subprocess
from pathlib import Path

import pytest

PROJECT = "maas-bird-network-lab-tf"
WORKERS = ("probe", "probe-alt", "probe-r2a", "probe-r2b")


def maas(*args):
    return json.loads(
        subprocess.check_output(
            ["lxc", "exec", "--project", PROJECT, "maas", "--", "maas", "lab", *args],
            text=True,
            timeout=60,
        )
    )


def lxd(path):
    return json.loads(subprocess.check_output(["lxc", "query", path], text=True, timeout=30))


@pytest.fixture(scope="module")
def native_nodes():
    return maas("machines", "read")


def test_isolated_pf_vlan_inventory():
    roles = json.loads(
        (Path(__file__).resolve().parents[2] / "maas/network-model/logical-roles.json").read_text()
    )["roles"]
    expected_vids = {role["vlan"] for role in roles}
    fabrics = maas("fabrics", "read")
    selected = [
        fabric for fabric in fabrics if fabric["name"] in {"r1-pf0", "r1-pf1", "r2-pf0", "r2-pf1"}
    ]
    assert len(selected) == 4
    assert len({fabric["id"] for fabric in selected}) == 4
    database_ids = set()
    for fabric in selected:
        vlans = [vlan for vlan in fabric["vlans"] if vlan["vid"] != 0]
        assert {vlan["vid"] for vlan in vlans} == expected_vids
        assert all(vlan["dhcp_on"] is False for vlan in vlans)
        database_ids.update(vlan["id"] for vlan in vlans)
    assert len(database_ids) == 20


def test_management_dhcp_and_addresses():
    subnet = next(item for item in maas("subnets", "read") if item["cidr"] == "10.203.241.0/24")
    assert subnet["gateway_ip"] == "10.203.241.1"
    assert subnet["vlan"]["dhcp_on"] is True
    racks = maas("rack-controllers", "read")
    assert len(racks) == 1
    assert subnet["vlan"]["primary_rack"] == racks[0]["system_id"]
    ranges = maas("ipranges", "read")
    dynamic = [item for item in ranges if item["type"] == "dynamic"]
    assert [(item["start_ip"], item["end_ip"]) for item in dynamic] == [
        ("10.203.241.200", "10.203.241.239")
    ]


def test_noble_hwe_image_files_complete():
    matches = [
        item
        for item in maas("boot-resources", "read")
        if item["name"] == "ubuntu/noble" and item["architecture"] == "amd64/hwe-24.04"
    ]
    assert len(matches) == 1
    image = maas("boot-resource", "read", str(matches[0]["id"]))
    assert image["sets"]
    assert all(item["complete"] for item in image["sets"].values())
    assert all(item["files"] for item in image["sets"].values())


@pytest.mark.parametrize("name", (*WORKERS, "juju"))
def test_native_commissioning_identity(name, native_nodes):
    matches = [node for node in native_nodes if node["hostname"] == name]
    assert len(matches) == 1
    node = matches[0]
    instance = lxd(f"/1.0/instances/{name}?project={PROJECT}")
    assert instance["config"]["user.maas-bird-owner"] == "network-lab"
    assert instance["config"]["limits.cpu"] == "1"
    assert instance["config"]["limits.memory"] == "4GiB"
    assert node["hardware_uuid"] == instance["config"]["volatile.uuid"]
    assert node["commissioning_status"] == 2
    assert node["commissioning_status_name"] == "Passed"
    assert node["power_type"] == "lxd"
    assert node["min_hwe_kernel"] == "hwe-24.04"
    expected = {
        device["hwaddr"] for device in instance["devices"].values() if device["type"] == "nic"
    }
    actual = {
        interface["mac_address"]
        for interface in node["interface_set"]
        if interface["type"] == "physical"
    }
    assert actual == expected
    assert len(actual) == (2 if name == "juju" else 4)


def test_power_trust_stays_restricted():
    entries = json.loads(
        subprocess.check_output(
            ["lxc", "config", "trust", "list", "--format=json"], text=True, timeout=30
        )
    )
    matches = [entry for entry in entries if entry["name"] == f"{PROJECT}-power"]
    assert len(matches) == 1
    assert matches[0]["restricted"] is True
    assert matches[0]["projects"] == [PROJECT]


@pytest.mark.parametrize("name", WORKERS)
def test_native_bond_and_vlan_parents(name, native_nodes):
    node = next(node for node in native_nodes if node["hostname"] == name)
    instance = lxd(f"/1.0/instances/{name}?project={PROJECT}")
    physical = {
        interface["mac_address"]: interface["name"]
        for interface in node["interface_set"]
        if interface["type"] == "physical"
    }
    bond = [interface for interface in node["interface_set"] if interface["type"] == "bond"]
    assert len(bond) == 1
    assert bond[0]["name"] == "bond-mgmt"
    assert bond[0]["params"]["bond_mode"] == "active-backup"
    assert set(bond[0]["parents"]) == {
        physical[instance["devices"][nic]["hwaddr"]] for nic in ("mgmt0", "mgmt1")
    }
    expected_ip = f"10.203.241.{40 + WORKERS.index(name) * 10}"
    assert [link["ip_address"] for link in bond[0]["links"] if link["mode"] == "static"] == [
        expected_ip
    ]
    vlans = [interface for interface in node["interface_set"] if interface["type"] == "vlan"]
    assert len(vlans) == 10
    for nic in ("pf0", "pf1"):
        parent = physical[instance["devices"][nic]["hwaddr"]]
        children = [interface for interface in vlans if interface["parents"] == [parent]]
        assert {interface["vlan"]["vid"] for interface in children} == set(range(3400, 3405))
    rack = 1 if name in WORKERS[:2] else 2
    assert f"rack-r{rack}" in node["tag_names"]


def state_outputs(stage):
    path = (
        Path(__file__).resolve().parents[2] / "runtime/terraform/lab" / stage / "terraform.tfstate"
    )
    return {name: item["value"] for name, item in json.loads(path.read_text())["outputs"].items()}


def test_native_access_topology_and_resource_budget():
    instances = lxd(f"/1.0/instances?project={PROJECT}&recursion=1")
    containers = {"spine0", "spine1", "r1a", "r1b", "r2a", "r2b", "access-a", "access-b"}
    vms = {*WORKERS, "maas", "juju", "gateway-r1", "gateway-r2"}
    assert {item["name"] for item in instances} == containers | vms
    assert {item["name"] for item in instances if item["type"] == "container"} == containers
    assert {item["name"] for item in instances if item["type"] == "virtual-machine"} == vms
    sizes = {"768MiB": 768, "2GiB": 2048, "4GiB": 4096, "8GiB": 8192}
    assert all(item["config"]["user.maas-bird-owner"] == "network-lab" for item in instances)
    assert all(item["config"]["limits.cpu"] == "1" for item in instances)
    assert sum(sizes[item["config"]["limits.memory"]] for item in instances) == 38 * 1024
    project = lxd(f"/1.0/projects/{PROJECT}")
    assert project["config"]["limits.cpu"] == "16"
    assert project["config"]["limits.memory"] == "56GiB"
    networks = state_outputs("00-networks")
    expected = {
        networks["management_network"],
        *networks["management_access_networks"].values(),
        *networks["fast_networks"].values(),
        *networks["core_networks"].values(),
    }
    assert len(expected) == 15
    prefix = networks["management_network"].removesuffix("-mgmt")
    actual_networks = {
        network["name"]
        for network in lxd("/1.0/networks?recursion=1")
        if network["name"].startswith(prefix + "-")
    }
    assert actual_networks == expected
    for name in expected:
        network = lxd(f"/1.0/networks/{name}")
        assert network["type"] == "bridge"
        assert network["config"]["bridge.driver"] == "openvswitch"
        assert network["config"]["user.maas-bird-owner"] == "network-lab"
    for name in networks["management_access_networks"].values():
        network = lxd(f"/1.0/networks/{name}")
        assert network["config"]["ipv4.address"] == network["config"]["ipv6.address"] == "none"
        assert network["config"]["ipv4.dhcp"] == "false"


@pytest.mark.parametrize("name", WORKERS)
def test_native_management_access_attachments(name):
    networks = state_outputs("00-networks")
    switches = state_outputs("15-management-access")["switches"]
    machines = state_outputs("30-machines")["machines"]
    instance = lxd(f"/1.0/instances/{name}?project={PROJECT}")
    for nic, side in (("mgmt0", "a"), ("mgmt1", "b")):
        network = networks["management_access_networks"][side]
        assert (
            instance["devices"][nic]["network"]
            == machines[name]["management_networks"][nic]
            == network
        )
        identity = switches[side]
        switch = lxd(f"/1.0/instances/{identity['name']}?project={PROJECT}")
        assert switch["name"] == f"access-{side}" and switch["type"] == "container"
        assert switch["config"]["volatile.uuid"] == identity["uuid"]
        assert switch["config"]["user.maas-bird-owner"] == "network-lab"
        assert (
            switch["config"]["limits.cpu"] == "1" and switch["config"]["limits.memory"] == "768MiB"
        )
        assert switch["devices"]["downlink"]["network"] == identity["network"] == network
        assert switch["devices"]["uplink"]["network"] == networks["management_network"]
    assert instance["devices"]["mgmt0"]["network"] != instance["devices"]["mgmt1"]["network"]
    rack = 2 if name.startswith("probe-r2") else 1
    for pf in (0, 1):
        assert (
            instance["devices"][f"pf{pf}"]["network"]
            == networks["fast_networks"][f"r{rack}-pf{pf}"]
        )
    juju = lxd(f"/1.0/instances/juju?project={PROJECT}")
    assert (
        juju["devices"]["mgmt0"]["network"]
        == juju["devices"]["mgmt1"]["network"]
        == networks["management_network"]
    )
