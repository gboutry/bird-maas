"""Native EVPN/VXLAN provider L2 transport; no OVN gateway-chassis claim."""

import importlib
import ipaddress
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODES = ("spine0", "spine1", "r1a", "r1b", "r2a", "r2b")
TORS = {"r1a": 11, "r1b": 12, "r2a": 13, "r2b": 14}
pytestmark = pytest.mark.integration


def router(guest, node, *args):
    return guest("docker", "exec", "clab-maas-bird-fabric-" + node, *args)


@pytest.mark.parametrize("node", NODES)
def test_exact_native_evpn_core_neighbors(provider, node):
    _, guest = provider
    core = (ROOT / f"containerlab/frr/{node}.conf").read_text()
    expected = dict(re.findall(r"^ neighbor ([0-9.]+) remote-as ([0-9]+)$", core, re.MULTILINE))
    summary = json.loads(router(guest, node, "vtysh", "-c", "show bgp l2vpn evpn summary json"))
    asn = re.search(r"^router bgp ([0-9]+)$", core, re.MULTILINE)
    assert asn is not None and summary["as"] == int(asn[1])
    assert summary["vrfName"] == "default"
    peers = summary["peers"]
    assert len(peers) == (4 if node.startswith("spine") else 2)
    assert set(peers) == set(expected)
    assert all(
        p["state"] == "Established" and p["remoteAs"] == int(expected[ip])
        for ip, p in peers.items()
    )


@pytest.mark.parametrize("node", TORS)
def test_exact_vxlan_parameters_and_routed_core_mtu(provider, node):
    model, guest = provider
    links = {
        r["ifname"]: r for r in json.loads(router(guest, node, "ip", "-j", "-d", "link", "show"))
    }
    bridge, tunnel = links["br-provider"], links["vx3405"]
    assert bridge["linkinfo"]["info_kind"] == "bridge"
    assert tunnel["linkinfo"]["info_kind"] == "vxlan"
    assert bridge["ifalias"] == tunnel["ifalias"] == "network-lab-provider"
    assert bridge["mtu"] == tunnel["mtu"] == model["mtu"] == 1500
    assert "UP" in bridge["flags"] and "UP" in tunnel["flags"]
    data = tunnel["linkinfo"]["info_data"]
    assert data["id"] == model["vni"] == 3405
    assert data["local"] == f"10.205.250.{TORS[node]}"
    assert data["port"] == model["vxlan_port"] == 4789
    assert data["learning"] is False
    assert tunnel["master"] == "br-provider"
    for interface in ("eth1", "eth2"):
        assert links[interface]["mtu"] >= model["mtu"] + 50
        assert "master" not in links[interface]
    addresses = json.loads(
        router(guest, node, "ip", "-j", "address", "show", "dev", "br-provider")
    )
    assert addresses[0]["addr_info"] == []


@pytest.mark.parametrize("source", ("nlab-provider-r1", "nlab-provider-r2"))
def test_cross_rack_provider_frames_at_full_mtu(provider, source):
    model, guest = provider
    endpoints = model["endpoints"]
    target = next(n for n in endpoints if n != source)
    local, remote = endpoints[source], endpoints[target]
    source_ip = str(ipaddress.ip_interface(local["address"]).ip)
    target_ip = str(ipaddress.ip_interface(remote["address"]).ip)
    route = json.loads(
        guest("ip", "netns", "exec", source, "ip", "-j", "route", "get", target_ip)
    )[0]
    assert route["dev"] == "provider0" and route["prefsrc"] == source_ip
    assert "gateway" not in route
    packets = guest(
        "ip",
        "netns",
        "exec",
        source,
        "ping",
        "-n",
        "-M",
        "do",
        "-s",
        "1472",
        "-c",
        "3",
        "-i",
        "0.2",
        "-W",
        "1",
        target_ip,
    )
    assert "3 packets transmitted, 3 received, 0% packet loss" in packets
    neighbors = json.loads(
        guest("ip", "netns", "exec", source, "ip", "-j", "neighbor", "show", "to", target_ip)
    )
    assert len(neighbors) == 1 and neighbors[0]["lladdr"] == remote["mac"]
    assert neighbors[0]["dev"] == "provider0"


@pytest.mark.parametrize("node", TORS)
def test_remote_macs_are_installed_by_evpn_on_the_expected_vtep(provider, node):
    model, guest = provider
    for namespace, endpoint in model["endpoints"].items():
        other = next(c for n, c in model["endpoints"].items() if n != namespace)
        guest(
            "ip",
            "netns",
            "exec",
            namespace,
            "ping",
            "-n",
            "-c",
            "1",
            "-W",
            "1",
            str(ipaddress.ip_interface(other["address"]).ip),
        )
        rows = json.loads(router(guest, node, "bridge", "-j", "fdb", "show"))
        selected = [
            r
            for r in rows
            if r["mac"] == endpoint["mac"]
            and r.get("ifname") == ("eth4" if node == endpoint["tor"] else "vx3405")
        ]
        assert selected
        database = json.loads(router(guest, node, "vtysh", "-c", "show evpn mac vni 3405 json"))
        required = {e["mac"] for e in model["endpoints"].values()}
        allowed = set(required)
        gateways = importlib.import_module("routing_expectations").gateway_intent()
        if gateways:
            allowed |= {g["provider_mac"] for g in gateways["gateways"].values()}
            allowed.add(gateways["logical_router"]["provider_mac"])
        assert required <= set(database["macs"]) <= allowed
        entry = database["macs"][endpoint["mac"]]
        assert entry["isDuplicate"] is False
        if node != endpoint["tor"]:
            expected = f"10.205.250.{TORS[endpoint['tor']]}"
            masters = [r for r in selected if r.get("master") == "br-provider"]
            encap = [r for r in selected if "self" in r["flags"]]
            assert masters and all("extern_learn" in r["flags"] for r in masters)
            assert len(encap) == 1 and encap[0]["dst"] == expected
            assert "extern_learn" in encap[0]["flags"]
            assert entry["type"] == "remote" and entry["remoteVtep"] == expected
        else:
            assert entry["type"] == "local" and entry["intf"] == "eth4"
