"""Native MAAS and guest acceptance for five logical VLAN roles per fast NIC.

Run after a roles deployment and trusted SSH host-key enrollment. These checks
do not establish physical SR-IOV, NUMA locality or storage traffic forwarding.
"""

import importlib
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOSTS = json.loads((ROOT / "maas/network-model/hosts.json").read_text())
pytestmark = pytest.mark.integration


@pytest.fixture
def role_worker(request, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    lab = importlib.import_module("deploy_ovn")
    deploy = importlib.import_module("deploy_probe")
    recipe = importlib.import_module("generate_roles")
    name = request.param
    instance = json.loads(
        lab.command("lxc", "query", f"/1.0/instances/{name}?project={lab.PROJECT}")
    )
    assert instance["config"].get("user.maas-bird-owner") == "network-lab"
    assert instance["config"]["limits.cpu"] == "2"
    assert instance["config"]["limits.memory"] == "4GiB"
    runtime = ROOT / "runtime" / (name if name != "probe" else "")
    enrollment = json.loads((runtime / "commission.json").read_text())
    assert instance["config"]["volatile.uuid"] == enrollment["lxd_uuid"]
    machine = deploy.maas("machine", "read", enrollment["system_id"])
    assert machine["status_name"] == "Deployed"
    assert machine["hostname"] == name
    assert machine["hardware_uuid"] == enrollment["lxd_uuid"]
    native = lab.ssh(name, "sudo", "cat", "/sys/class/dmi/id/product_uuid")
    assert native == enrollment["lxd_uuid"]
    return name, lab, deploy, recipe, machine


@pytest.mark.parametrize("role_worker", HOSTS, indirect=True, ids=HOSTS)
def test_maas_role_graph_and_native_hwe(role_worker):
    name, lab, deploy, _, machine = role_worker
    deploy.validate_configuration(machine, name, "loopback32", HOSTS[name]["management_ip"])
    deploy.validate_roles(machine)
    assert len([i for i in machine["interface_set"] if i["type"] == "vlan"]) == 10
    depends = lab.ssh(name, "dpkg-query", "-W", "-f=${Depends}", "linux-image-generic-hwe-24.04")
    expected = re.search(r"linux-image-([0-9.]+-[0-9]+-generic)", depends)
    assert expected is not None
    assert lab.ssh(name, "uname", "-r") == expected[1]


@pytest.mark.parametrize("role_worker", HOSTS, indirect=True, ids=HOSTS)
def test_native_role_parents_addresses_and_source_guards(role_worker):
    name, lab, _, recipe, _ = role_worker
    links = {v["ifname"]: v for v in json.loads(lab.ssh(name, "ip", "-d", "-j", "link", "show"))}
    addresses = {
        v["ifname"]: v["addr_info"]
        for v in json.loads(lab.ssh(name, "ip", "-j", "address", "show"))
    }
    rules = json.loads(lab.ssh(name, "ip", "-j", "-4", "-N", "rule", "show"))
    allocation = recipe.roles()
    loopbacks = {
        a["local"] + "/32"
        for a in addresses["lo"]
        if a["family"] == "inet" and a["prefixlen"] == 32
    }
    assert loopbacks == {recipe.address(name, r) for r in allocation}
    for role in allocation:
        source = recipe.address(name, role).split("/")[0]
        selected = [r for r in rules if r["priority"] == role["rule_priority"]]
        assert len(selected) == 1
        assert selected[0]["src"].removesuffix("/32") == source
        assert str(selected[0]["table"]) == str(role["table"])
        assert str(selected[0]["protocol"]) == "4"
        routes = json.loads(
            lab.ssh(name, "ip", "-j", "-4", "-N", "route", "show", "table", str(role["table"]))
        )
        guards = [r for r in routes if r.get("dst") == "default"]
        assert len(guards) == 1
        assert str(guards[0]["type"]) == "7"
        assert str(guards[0]["protocol"]) == "4"
        assert guards[0]["metric"] == 42760
        for side, parent in enumerate(("enp7s0", "enp8s0")):
            child = f"{parent}.{role['vlan']}"
            link = links[child]
            actual_parent = link.get("link")
            if actual_parent is None:
                actual_parent = next(
                    v["ifname"] for v in links.values() if v["ifindex"] == link["link_index"]
                )
            assert actual_parent == parent
            assert link["linkinfo"]["info_kind"] == "vlan"
            assert link["linkinfo"]["info_data"]["id"] == role["vlan"]
            assert link["mtu"] == links[parent]["mtu"] == 1500
            assert "master" not in links[parent] and "master" not in link
            assert not any(a["family"] == "inet" for a in addresses[child])
            if role["id"] != "general":
                assert {a["local"] for a in addresses[child] if a["family"] == "inet6"} == {
                    recipe.local_address(name, role, side).split("/")[0]
                }
    main = json.loads(lab.ssh(name, "ip", "-j", "-4", "-N", "route", "show", "table", "254"))
    guard = [r for r in main if r.get("dst") == "10.205.0.0/16"]
    assert len(guard) == 1 and str(guard[0]["type"]) == "7" and guard[0]["metric"] == 42760


@pytest.mark.parametrize("role_worker", HOSTS, indirect=True, ids=HOSTS)
def test_every_role_has_full_size_tor_link_local_reachability(role_worker):
    name, lab, _, recipe, _ = role_worker
    for parent in ("enp7s0", "enp8s0"):
        for role in recipe.roles():
            packets = lab.ssh(
                name,
                "ping",
                "-6",
                "-n",
                "-M",
                "do",
                "-s",
                "1452",
                "-I",
                f"{parent}.{role['vlan']}",
                "-c",
                "3",
                "-i",
                "0.2",
                "-W",
                "1",
                "fe80::1",
            )
            assert "3 packets transmitted, 3 received, 0% packet loss" in packets


@pytest.mark.parametrize("role_worker", HOSTS, indirect=True, ids=HOSTS)
def test_role_boot_network_online_completed_successfully(role_worker):
    name, lab, _, _, _ = role_worker
    fields = dict(
        row.split("=", 1)
        for row in lab.ssh(
            name,
            "systemctl",
            "show",
            "systemd-networkd-wait-online.service",
            "-p",
            "Result",
            "-p",
            "ExecMainStatus",
            "-p",
            "ExecMainStartTimestampMonotonic",
            "-p",
            "ExecMainExitTimestampMonotonic",
        ).splitlines()
    )
    assert int(fields["ExecMainStartTimestampMonotonic"]) > 0
    assert int(fields["ExecMainExitTimestampMonotonic"]) >= int(
        fields["ExecMainStartTimestampMonotonic"]
    )
    assert fields["Result"] == "success" and fields["ExecMainStatus"] == "0"


@pytest.mark.parametrize("role_worker", HOSTS, indirect=True, ids=HOSTS)
def test_role_deployment_cloud_init_completed_without_errors(role_worker):
    name, lab, _, _, _ = role_worker
    completed = subprocess.run(
        lab.ssh_args(name, "sudo", "cloud-init", "status", "--wait", "--format", "json"),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    status = json.loads(completed.stdout)
    assert status["status"] == "done" and status["errors"] == []
