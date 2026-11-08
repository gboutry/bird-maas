"""Native FRR core contexts before host BIRD activation."""

import json
import subprocess
import time
from pathlib import Path

import pytest
from maas_bird.runtime import write_atomic

PROJECT = "maas-bird-network-lab-tf"
TORS = ("r1a", "r1b", "r2a", "r2b")
NODES = ("spine0", "spine1", *TORS)
CONTEXTS = (("default", 0, None), ("access", 3420, 6200), ("replication", 3430, 6300))


def execute(node, *args):
    return subprocess.check_output(
        ["lxc", "exec", "--project", PROJECT, node, "--", *args], text=True, timeout=30
    )


@pytest.fixture(scope="module", autouse=True)
def owned():
    for node in NODES:
        row = json.loads(
            subprocess.check_output(
                ["lxc", "query", f"/1.0/instances/{node}?project={PROJECT}"],
                text=True,
                timeout=30,
            )
        )
        assert row["type"] == "container"
        assert row["status"] == "Running"
        assert row["config"]["user.maas-bird-owner"] == "network-lab"
        assert row["expanded_config"].get("security.privileged", "false") == "false"
        peers = TORS if node.startswith("spine") else ("spine0", "spine1")
        for peer in peers:
            host_name = row["config"][f"volatile.{peer}.host_name"]
            link = json.loads(
                subprocess.check_output(
                    ["ip", "-j", "link", "show", host_name], text=True, timeout=30
                )
            )[0]
            assert link["mtu"] >= 1550


@pytest.mark.parametrize("node", NODES)
@pytest.mark.parametrize("vrf,vid,table", CONTEXTS)
def test_native_core_bgp_bfd_and_context(node, vrf, vid, table):
    summary = json.loads(execute(node, "vtysh", "-c", "show bgp vrf all summary json"))
    peers = summary[vrf]["ipv4Unicast"]["peers"]
    core = TORS if node.startswith("spine") else ("spine0", "spine1")
    interfaces = {name if not vid else f"{name}.{vid}" for name in core}
    assert {key for key in peers if not key.startswith("fe80:")} == interfaces
    assert all(peers[key]["state"] == "Established" for key in interfaces)
    bfd = json.loads(execute(node, "vtysh", "-c", "show bfd peers json"))
    observed = {
        peer.get("interface"): peer["status"]
        for peer in bfd
        if peer["vrf"] == vrf and peer.get("interface") in interfaces
    }
    assert observed == dict.fromkeys(interfaces, "up")
    if table is not None:
        link = json.loads(execute(node, "ip", "-j", "-d", "link", "show", vrf))[0]
        assert link["linkinfo"]["info_kind"] == "vrf"
        assert link["linkinfo"]["info_data"]["table"] == table
        assert "UP" in link["flags"]
    for interface in interfaces:
        link = json.loads(execute(node, "ip", "-j", "address", "show", "dev", interface))[0]
        assert link["mtu"] >= 1550
        assert not any(item["family"] == "inet" for item in link["addr_info"])
        assert any(
            item["family"] == "inet6" and item["scope"] == "link" for item in link["addr_info"]
        )
        if table is not None:
            assert link["master"] == vrf


@pytest.mark.parametrize("node", TORS)
def test_native_host_vlan_contexts(node):
    for vid in range(3400, 3405):
        row = json.loads(
            execute(node, "ip", "-j", "-d", "address", "show", "dev", f"fast0.{vid}")
        )[0]
        assert row["link"] == "fast0"
        assert row["linkinfo"]["info_data"]["id"] == vid
        expected = "access" if vid < 3402 else "replication" if vid < 3404 else None
        assert row.get("master") == expected
        assert [item["local"] for item in row["addr_info"]] == ["fe80::1"]


@pytest.mark.parametrize("node", TORS)
def test_native_ipv4_loopback_packets_after_context_configuration(node):
    destination = "10.205.250.13" if node.startswith("r1") else "10.205.250.11"
    routes = json.loads(execute(node, "ip", "-j", "route", "show", f"{destination}/32"))
    assert len(routes) == 1
    assert {hop["dev"] for hop in routes[0]["nexthops"]} == {"spine0", "spine1"}
    assert all(hop["via"]["family"] == "inet6" for hop in routes[0]["nexthops"])
    source = f"10.205.250.{11 + TORS.index(node)}"
    execute(node, "ping", "-n", "-c", "2", "-W", "2", "-I", source, destination)


def test_networkd_reconfiguration_keeps_frr_kernel_routes():
    destination = "10.205.250.13/32"
    before = json.loads(execute("r1a", "ip", "-j", "route", "show", destination))
    assert len(before) == 1
    assert len(before[0]["nexthops"]) == 2
    execute("r1a", "networkctl", "reconfigure", "spine0", "spine1")
    after = json.loads(execute("r1a", "ip", "-j", "route", "show", destination))
    assert after == before
    execute("r1a", "ping", "-n", "-c", "2", "-W", "2", "-I", "10.205.250.11", "10.205.250.13")


def core_service_health(selected, stopped):
    deadline = time.monotonic() + 45
    while True:
        try:
            state = {}
            for node in TORS:
                summary = json.loads(execute(node, "vtysh", "-c", "show bgp vrf all summary json"))
                sessions = json.loads(execute(node, "vtysh", "-c", "show bfd peers json"))
                for vrf, vid, _ in CONTEXTS:
                    for peer in ("spine0", "spine1"):
                        interface = peer + (f".{vid}" if vid else "")
                        failed = stopped and peer == selected
                        up = (
                            summary[vrf]["ipv4Unicast"]["peers"][interface]["state"]
                            == "Established"
                        )
                        assert up != failed, (node, vrf, interface)
                        bfd = next(
                            row
                            for row in sessions
                            if row.get("interface") == interface and row["vrf"] == vrf
                        )
                        assert bfd["status"] in ({"down", "init"} if failed else {"up"})
                physical = json.loads(execute(node, "ip", "-j", "link", "show", selected))[0]
                assert "UP" in physical["flags"] and "LOWER_UP" in physical["flags"]
                destination = "10.205.250.13" if node.startswith("r1") else "10.205.250.11"
                routes = json.loads(
                    execute(node, "ip", "-j", "route", "show", f"{destination}/32")
                )
                assert len(routes) == 1
                hops = routes[0].get("nexthops", [routes[0]])
                assert {hop["dev"] for hop in hops} == {"spine0", "spine1"} - (
                    {selected} if stopped else set()
                )
                assert all(hop["via"]["family"] == "inet6" for hop in hops)
                packets = execute(
                    node,
                    "ping",
                    "-n",
                    "-c",
                    "3",
                    "-i",
                    "0.2",
                    "-W",
                    "1",
                    "-M",
                    "do",
                    "-s",
                    "1472",
                    "-I",
                    f"10.205.250.{11 + TORS.index(node)}",
                    destination,
                )
                assert "3 packets transmitted, 3 received" in packets
                state[node] = {
                    "bgp": summary,
                    "bfd": sessions,
                    "route": routes,
                    "packets": packets,
                    "link": physical,
                }
            return state
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def kernel_bgp_paths():
    paths = {}
    for node in NODES:
        routes = json.loads(
            execute(node, "ip", "-j", "route", "show", "table", "all", "protocol", "bgp")
        )
        paths[node] = {
            f"{route.get('table', 'main')}/{route['dst']}": sorted(
                (hop["dev"], hop["via"]["family"], hop["via"]["host"], hop.get("weight", 1))
                for hop in route.get("nexthops", [route])
            )
            for route in routes
        }
    return paths


@pytest.mark.parametrize("node", NODES)
def test_native_core_networkd_restart_keeps_kernel_paths(node):
    before = kernel_bgp_paths()
    assert all(before.values()), before
    core_service_health("spine0", False)
    recovered = False
    receipt = {"node": node, "before": before}
    try:
        execute(node, "systemctl", "restart", "systemd-networkd")
        receipt["health"] = core_service_health("spine0", False)
        receipt["after"] = kernel_bgp_paths()
        assert receipt["after"] == before
        recovered = True
    finally:
        receipt["recovered"] = recovered
        receipt["observed_paths"] = kernel_bgp_paths()
        directory = (
            Path(__file__).resolve().parents[2] / "runtime/terraform/acceptance/frr-restarts"
        )
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_atomic(
            directory / f"networkd-{node}-{time.time_ns()}.json",
            json.dumps(receipt, indent=2) + "\n",
            0o600,
        )
        if not recovered:
            execute(node, "systemctl", "restart", "frr")
            core_service_health("spine0", False)


@pytest.mark.parametrize("spine", ("spine0", "spine1"))
def test_native_spine_frr_stop_and_start_preserves_surviving_forwarding(spine):
    before_pid = execute(spine, "pidof", "bgpd").strip()
    configuration = execute(spine, "sha256sum", "/etc/frr/frr.conf")
    receipt = {
        "spine": spine,
        "before_bgpd_pid": before_pid,
        "configuration_sha256": configuration,
        "before": core_service_health(spine, False),
    }
    try:
        receipt["stop_started"] = time.monotonic()
        execute(spine, "systemctl", "stop", "frr")
        status = subprocess.run(
            ["lxc", "exec", "--project", PROJECT, spine, "--", "systemctl", "is-active", "frr"],
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert status.returncode != 0 and status.stdout.strip() == "inactive"
        receipt["surviving"] = core_service_health(spine, True)
        receipt["surviving_observed"] = time.monotonic()
        receipt["fault_assertions_passed"] = True
    finally:
        try:
            execute(spine, "systemctl", "start", "frr")
            receipt["restored"] = core_service_health(spine, False)
            assert execute(spine, "systemctl", "is-active", "frr").strip() == "active"
            after_pid = execute(spine, "pidof", "bgpd").strip()
            assert after_pid and after_pid != before_pid
            assert execute(spine, "sha256sum", "/etc/frr/frr.conf") == configuration
            receipt["after_bgpd_pid"] = after_pid
            receipt["restored_at"] = time.monotonic()
        finally:
            directory = (
                Path(__file__).resolve().parents[2] / "runtime/terraform/acceptance/frr-restarts"
            )
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_atomic(
                directory / f"{spine}-{time.time_ns()}.json",
                json.dumps(receipt, indent=2) + "\n",
                0o600,
            )
