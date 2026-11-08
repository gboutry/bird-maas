"""Live acceptance of the native LXD FRR underlay stage."""

import json
import subprocess
import time

import pytest

PROJECT = "maas-bird-network-lab-tf"
TORS = ("r1a", "r1b", "r2a", "r2b")
NODES = ("spine0", "spine1", *TORS)


def execute(node, *args):
    return subprocess.check_output(
        ["lxc", "exec", "--project", PROJECT, node, "--", *args],
        text=True,
        timeout=30,
    )


def routes(node, destination):
    return json.loads(execute(node, "ip", "-j", "route", "show", destination))


@pytest.fixture(scope="module", autouse=True)
def owned_nodes():
    for node in NODES:
        data = json.loads(
            subprocess.check_output(
                ["lxc", "query", f"/1.0/instances/{node}?project={PROJECT}"],
                text=True,
                timeout=30,
            )
        )
        assert data["type"] == "container"
        assert data["expanded_config"].get("security.privileged", "false") == "false"
        assert data["config"]["user.maas-bird-owner"] == "network-lab"
        assert data["config"]["limits.cpu"] == "1"
        assert data["config"]["limits.memory"] == "768MiB"
        assert data["status"] == "Running"


@pytest.mark.parametrize("node", NODES)
def test_native_daemons_and_unnumbered_peering(node):
    status = json.loads(execute(node, "cloud-init", "status", "--format=json"))
    assert status["status"] == "done", status
    assert status["extended_status"] == "done", status
    assert status["errors"] == [], status
    assert status["recoverable_errors"] == {}, status
    assert execute(node, "dpkg-query", "-W", "-f=${Version}", "frr") == ("10.7.1-0~ubuntu24.04.1")
    assert execute(node, "systemctl", "is-active", "frr").strip() == "active"
    peers = json.loads(execute(node, "vtysh", "-c", "show bgp summary json"))["ipv4Unicast"][
        "peers"
    ]
    expected = set(TORS) if node.startswith("spine") else {"spine0", "spine1"}
    assert set(peers) == expected
    assert all(peer["state"] == "Established" for peer in peers.values()), peers
    bfd = json.loads(execute(node, "vtysh", "-c", "show bfd peers json"))
    assert len(bfd) == len(expected)
    assert all(peer["status"] == "up" for peer in bfd), bfd
    addresses = json.loads(execute(node, "ip", "-j", "address", "show"))
    core = {link["ifname"]: link["addr_info"] for link in addresses}
    for name in expected:
        assert not any(address["family"] == "inet" for address in core[name])
        assert any(
            address["family"] == "inet6" and address["scope"] == "link" for address in core[name]
        )


@pytest.mark.parametrize("node", TORS)
def test_loopback_ecmp_forwarding(node):
    target = "r2a" if node.startswith("r1") else "r1a"
    destination = "10.205.250.13" if target == "r2a" else "10.205.250.11"
    route = routes(node, f"{destination}/32")
    assert len(route) == 1
    assert route[0]["protocol"] == "bgp"
    nexthops = route[0]["nexthops"]
    assert {hop["dev"] for hop in nexthops} == {"spine0", "spine1"}
    assert all(hop["via"]["family"] == "inet6" for hop in nexthops)
    source = f"10.205.250.{11 + TORS.index(node)}"
    execute(node, "ping", "-n", "-c", "3", "-W", "2", "-I", source, destination)


def test_single_uplink_failure_and_recovery():
    node, destination = "r1a", "10.205.250.13/32"
    assert len(routes(node, destination)[0]["nexthops"]) == 2
    try:
        execute(node, "ip", "link", "set", "spine0", "down")
        deadline = time.monotonic() + 15
        while True:
            route = routes(node, destination)
            if route and route[0].get("dev") == "spine1":
                break
            assert time.monotonic() < deadline, route
            time.sleep(0.5)
        execute(node, "ping", "-n", "-c", "3", "-W", "2", "-I", "10.205.250.11", "10.205.250.13")
    finally:
        execute(node, "ip", "link", "set", "spine0", "up")
    deadline = time.monotonic() + 30
    while True:
        route = routes(node, destination)
        if route and len(route[0].get("nexthops", [])) == 2:
            break
        assert time.monotonic() < deadline, route
        time.sleep(0.5)
    execute(node, "ping", "-n", "-c", "3", "-W", "2", "-I", "10.205.250.11", "10.205.250.13")


@pytest.mark.parametrize("node", NODES)
def test_native_lldp_identity_and_interfaces(node):
    configuration = execute(node, "lldpcli", "-f", "keyvalue", "show", "configuration")
    expected = "r1a,r1b,r2a,r2b" if node.startswith("spine") else "fast0"
    chassis_interface = "r1a" if node.startswith("spine") else "fast0"
    assert f"configuration.config.iface-pattern={expected}\n" in configuration
    assert f"configuration.config.cid-pattern={chassis_interface}\n" in configuration
    mac = execute(node, "cat", f"/sys/class/net/{chassis_interface}/address").strip()
    chassis = execute(node, "lldpcli", "-f", "keyvalue", "show", "chassis")
    assert f"local-chassis.chassis.mac={mac}\n" in chassis
    assert execute(node, "systemctl", "is-active", "lldpd").strip() == "active"
