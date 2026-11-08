"""Live FRR fabric checks; no claim about host BIRD, EVPN or OVN gateways."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODES = {"spine0": 1, "spine1": 2, "r1a": 11, "r1b": 12, "r2a": 13, "r2b": 14}
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("name", NODES)
def test_bgp_bfd_and_installed_loopbacks(name, router, request, host_manifest):
    bgp = json.loads(router(name, "vtysh", "-c", "show bgp summary json"))
    bfd = json.loads(router(name, "vtysh", "-c", "show bfd peers json"))
    routes = json.loads(router(name, "ip", "-j", "route", "show", "protocol", "bgp"))
    snapshot = {"bgp": bgp, "bfd": bfd, "routes": routes}
    output = ROOT / "runtime" / "frr-underlay" / request.config.getoption("--frr-lab")
    output.mkdir(parents=True, exist_ok=True)
    (output / f"{name}.json").write_text(json.dumps(snapshot, indent=2) + "\n")
    peers = bgp["ipv4Unicast"]["peers"]
    expected_peers = (
        len(host_manifest["peers"][name])
        if host_manifest
        else (4 if name.startswith("spine") else 2)
    )
    assert len(peers) == expected_peers
    if host_manifest:
        assert set(peers) == set(host_manifest["peers"][name])
        assert all(
            peers[ip]["remoteAs"] == p["asn"] for ip, p in host_manifest["peers"][name].items()
        )
        assert {p["peer"] for p in bfd} == set(host_manifest["peers"][name])
    assert all(peer["state"] == "Established" for peer in peers.values())
    assert len(bfd) == expected_peers
    assert all(peer["status"] == "up" for peer in bfd)
    expected = {f"10.205.250.{last}" for other, last in NODES.items() if other != name}
    if host_manifest:
        expected = {p.removesuffix("/32") for p in host_manifest["expected_routes"][name]}
    assert {route["dst"].removesuffix("/32") for route in routes} == expected
    for route in routes:
        hops = route.get("nexthops", [route])
        assert all(hop["dev"] != "eth0" for hop in hops)
        if host_manifest and name.startswith("spine"):
            rack = host_manifest["origin_racks"].get(route["dst"].removesuffix("/32"))
            if rack is not None:
                expected_devices = {"eth1", "eth2"} if rack == "r1" else {"eth3", "eth4"}
                assert {hop["dev"] for hop in hops} == expected_devices
        if not name.startswith("spine") and route["dst"] in {
            f"10.205.250.{last}" for other, last in NODES.items() if other != name and last > 2
        }:
            assert {hop["dev"] for hop in hops} == {"eth1", "eth2"}


@pytest.mark.parametrize("name", NODES)
def test_loopback_dataplane_and_management_guard(name, router):
    for other, last in NODES.items():
        if other != name:
            router(
                name,
                "ping",
                "-n",
                "-I",
                f"10.205.250.{NODES[name]}",
                "-c",
                "2",
                "-W",
                "1",
                f"10.205.250.{last}",
            )
    guard = json.loads(router(name, "ip", "-j", "route", "show", "10.205.0.0/16"))
    assert len(guard) == 1
    assert guard[0]["type"] == "unreachable"
    assert guard[0]["metric"] == 42760
