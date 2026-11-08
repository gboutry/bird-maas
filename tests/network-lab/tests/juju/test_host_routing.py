"""Real host BIRD protocols, dedicated kernel tables and managed-source packets."""

import importlib
import json
from decimal import Decimal
from pathlib import Path

import jubilant
import pytest
from maas_bird.render import protocol_name

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "maas-bird-network-lab"
HOSTS = json.loads((ROOT / "maas/network-model/hosts.json").read_text())
WORKERS = [("probe", "r1a"), ("probe-alt", "r1b"), ("probe-r2a", "r2a"), ("probe-r2b", "r2b")]
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def live_juju():
    juju = jubilant.Juju(model="network-lab:network-lab")
    juju.wait(
        lambda s: jubilant.all_active(s, *("bird-" + suffix for _, suffix in WORKERS)),
        timeout=240,
    )
    return juju


def owned_worker(juju, name, suffix):
    bindings = importlib.import_module("juju_workers")
    return bindings.worker_binding(juju, name, "bird-" + suffix).unit


@pytest.mark.parametrize(("name", "suffix"), WORKERS)
def test_bird_sessions_bfd_and_exact_kernel_routes(live_juju, name, suffix):
    juju = live_juju
    unit = owned_worker(juju, name, suffix)
    assert juju.exec("systemctl", "is-active", "bird", unit=unit).stdout.strip() == "active"
    intended = importlib.import_module("lab_policy").read("all-attachments.yaml")["nic-" + suffix]
    attachments = json.loads(intended["attachments"])
    assert len(attachments) == 10
    expected_protocols = {protocol_name(a["domain"], a["id"]) for a in attachments}
    protocols = juju.exec("sudo", "birdc", "show", "protocols", unit=unit).stdout
    rows = [line.split() for line in protocols.splitlines()]
    bgp = {r[0]: r for r in rows if len(r) >= 6 and r[1] == "BGP"}
    assert set(bgp) == expected_protocols
    assert all(r[3] == "up" and "Established" in r[5:] for r in bgp.values())
    bfd = juju.exec("sudo", "birdc", "show", "bfd", "sessions", unit=unit).stdout
    sessions = [r.split() for r in bfd.splitlines() if r.strip().startswith("fe80:")]
    assert len(sessions) == 10
    assert {r[1] for r in sessions} == {f"{a['uplink']}.{a['vlan']}" for a in attachments}
    assert all(len(r) == 6 and r[0] == "fe80::1" and r[2] == "Up" for r in sessions)
    assert all(
        Decimal(r[-2]) == Decimal("0.300") and Decimal(r[-1]) == Decimal("0.900") for r in sessions
    )
    routes = json.loads(
        juju.exec("ip", "-j", "-4", "-N", "route", "show", "table", "4400", unit=unit).stdout
    )
    learned = [r for r in routes if str(r.get("protocol")) == "12"]
    expected = importlib.import_module("routing_expectations").general_paths(name)
    assert {r["dst"].removesuffix("/32") for r in learned} == set(expected)
    guards = [r for r in routes if str(r.get("protocol")) != "12"]
    assert len(guards) == 1
    assert guards[0]["dst"] == "default" and str(guards[0]["type"]) == "7"
    assert str(guards[0]["protocol"]) == "4" and guards[0]["metric"] == 42760
    for route in learned:
        hops = route.get("nexthops", [route])
        devices = expected[route["dst"].removesuffix("/32")]
        assert len(hops) == len(devices) and {h["dev"] for h in hops} == devices
        assert all(h["via"]["family"] == "inet6" and h["via"]["host"] == "fe80::1" for h in hops)


@pytest.mark.parametrize(("name", "suffix"), WORKERS)
@pytest.mark.parametrize("payload", [56, 1472])
def test_every_directed_managed_path_and_full_underlay_mtu(live_juju, name, suffix, payload):
    juju = live_juju
    unit = owned_worker(juju, name, suffix)
    source = HOSTS[name]["loopback"].split("/")[0]
    for other, host in HOSTS.items():
        if other == name:
            continue
        target = host["loopback"].split("/")[0]
        result = juju.exec(
            "ping",
            "-n",
            "-M",
            "do",
            "-s",
            str(payload),
            "-I",
            source,
            "-c",
            "3",
            "-W",
            "1",
            target,
            unit=unit,
        )
        assert "3 packets transmitted, 3 received, 0% packet loss" in result.stdout


@pytest.mark.parametrize(("name", "suffix"), WORKERS)
def test_two_installed_gp_paths_with_reverse_path_filtering_retained(live_juju, name, suffix):
    juju = live_juju
    unit = owned_worker(juju, name, suffix)
    result = juju.exec("ip", "-j", "-4", "-N", "route", "show", "table", "4400", unit=unit)
    learned = [r for r in json.loads(result.stdout) if str(r.get("protocol")) == "12"]
    expected = importlib.import_module("routing_expectations").general_paths(name)
    assert {r["dst"].removesuffix("/32") for r in learned} == set(expected)
    for route in learned:
        hops = route.get("nexthops", [route])
        devices = expected[route["dst"].removesuffix("/32")]
        assert len(hops) == len(devices) and {h["dev"] for h in hops} == devices
    filtering = juju.exec(
        "cat",
        "/proc/sys/net/ipv4/conf/all/rp_filter",
        "/proc/sys/net/ipv4/conf/enp7s0.3404/rp_filter",
        "/proc/sys/net/ipv4/conf/enp8s0.3404/rp_filter",
        unit=unit,
    )
    assert filtering.stdout.split() == ["2", "2", "2"]
