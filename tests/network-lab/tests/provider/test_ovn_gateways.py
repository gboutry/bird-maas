"""Actual isolated OVN gateways, centralized NAT and provider packets."""

import ipaddress
import json
from pathlib import Path

import pytest
from maas_bird.runtime import write_atomic

ROOT = Path(__file__).resolve().parents[2]
WORKERS = ("probe", "probe-alt", "probe-r2a", "probe-r2b")
pytestmark = pytest.mark.integration


def ovs(lab, namespace, *args):
    return lab.fabric(
        "ip",
        "netns",
        "exec",
        namespace,
        "ovs-vsctl",
        "--db=unix:/run/network-lab/gateways/" + namespace + "/db.sock",
        *args,
    )


def test_exact_chassis_roles_and_independent_namespace_processes(gateways):
    lab, model, endpoints, _ = gateways
    result = json.loads(
        lab.fabric("ovn-sbctl", "--format=json", "--columns=name,other_config", "list", "Chassis")
    )
    rows = {name: dict(config[1]) for name, config in result["data"]}
    assert set(rows) == {*(v["uuid"] for v in endpoints.values()), *model["gateways"]}
    for name, config in model["gateways"].items():
        assert "enable-chassis-as-gw" in rows[name]["ovn-cms-options"]
        assert (
            ovs(lab, name, "get", "Open_vSwitch", ".", "external_ids:system-id").strip('"') == name
        )
        assert (
            ovs(lab, name, "get", "Open_vSwitch", ".", "external_ids:ovn-encap-ip").strip('"')
            == config["vtep"].split("/")[0]
        )
        ports = set(ovs(lab, name, "list-ports", "br-ex").splitlines())
        assert "provider0" in ports and not ports & {"transport0", "control0"}
        assert all(p == "provider0" or p.startswith("patch-") for p in ports)
        script = (
            "import os,pathlib,json; n=pathlib.Path('/run/netns/'+os.environ['NS']).stat(); "
            "base=pathlib.Path('/run/network-lab/gateways')/os.environ['NS']; r={};\n"
            "for c in ('ovsdb-server','ovs-vswitchd','ovn-controller'):\n"
            " p=int((base/(c+'.pid')).read_text()); "
            "s=pathlib.Path('/proc/'+str(p)+'/ns/net').stat(); "
            "r[c]={'pid':p,'same_namespace':(s.st_dev,s.st_ino)==(n.st_dev,n.st_ino)}\n"
            "print(json.dumps(r))"
        )
        processes = json.loads(lab.fabric("env", "NS=" + name, "python3", "-c", script))
        assert len({p["pid"] for p in processes.values()}) == 3
        assert all(p["same_namespace"] for p in processes.values())
        for bridge in ("br-int", "br-ex"):
            lab.fabric("test", "-S", f"/run/network-lab/gateways/{name}/{bridge}.mgmt")
    for value in endpoints.values():
        assert "enable-chassis-as-gw" not in rows[value["uuid"]].get("ovn-cms-options", "")


@pytest.mark.parametrize("name", WORKERS)
def test_gateway_vteps_use_only_declared_gp_paths(gateways, name):
    lab, model, _, expectations = gateways
    expected = expectations.general_paths(name)
    routes = json.loads(lab.ssh(name, "ip", "-j", "-4", "-N", "route", "show", "table", "4400"))
    learned = {r["dst"].removesuffix("/32"): r for r in routes if str(r.get("protocol")) == "12"}
    assert set(learned) == set(expected)
    for destination, devices in expected.items():
        hops = learned[destination].get("nexthops", [learned[destination]])
        assert len(hops) == len(devices) and {h["dev"] for h in hops} == devices
    source = lab.HOSTS[name]["loopback"].split("/")[0]
    for config in model["gateways"].values():
        text = lab.ssh(
            name,
            "ping",
            "-n",
            "-I",
            source,
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
            config["vtep"].split("/")[0],
        )
        assert "3 packets transmitted, 3 received, 0% packet loss" in text


def test_router_redirect_is_bound_to_the_highest_priority_gateway(gateways):
    lab, model, _, _ = gateways
    result = json.loads(
        lab.fabric("ovn-sbctl", "--format=json", "--columns=name,_uuid", "list", "Chassis")
    )
    chassis = {uuid[1]: name for name, uuid in result["data"]}
    ports = json.loads(
        lab.fabric(
            "ovn-sbctl",
            "--format=json",
            "--columns=logical_port,chassis,type",
            "find",
            "Port_Binding",
            "type=chassisredirect",
        )
    )["data"]
    assert len(ports) == 1
    port, binding, kind = ports[0]
    assert port == "cr-provider-router-to-external" and kind == "chassisredirect"
    desired = max(model["gateways"], key=lambda n: model["gateways"][n]["priority"])
    assert chassis[binding[1]] == desired


@pytest.mark.parametrize("external", ("nlab-provider-r1", "nlab-provider-r2"))
@pytest.mark.parametrize("name", WORKERS)
def test_external_endpoint_reaches_tenant_floating_ip(gateways, name, external):
    lab, _, endpoints, _ = gateways
    text = lab.fabric(
        "ip",
        "netns",
        "exec",
        external,
        "ping",
        "-n",
        "-M",
        "do",
        "-s",
        "1372",
        "-c",
        "3",
        "-i",
        "0.2",
        "-W",
        "1",
        endpoints[name]["floating"],
    )
    assert "3 packets transmitted, 3 received, 0% packet loss" in text
    write_atomic(
        ROOT / f"runtime/gateways/{external}-to-{name}.json",
        json.dumps({"external": external, "target": endpoints[name], "packets": text}, indent=2)
        + "\n",
        0o600,
    )


@pytest.mark.parametrize("external", ("192.0.2.10", "192.0.2.20"))
@pytest.mark.parametrize("name", WORKERS)
def test_tenant_reaches_provider_endpoint_through_gateway(gateways, name, external):
    lab, model, _, _ = gateways
    routes = json.loads(
        lab.ssh(
            name,
            "sudo",
            "ip",
            "netns",
            "exec",
            "nlab-tenant",
            "ip",
            "-j",
            "route",
            "show",
            "default",
        )
    )
    assert len(routes) == 1 and routes[0]["dev"] == "nlab-peer"
    assert routes[0]["gateway"] == str(
        ipaddress.ip_interface(model["logical_router"]["tenant_address"]).ip
    )
    text = lab.ssh(
        name,
        "sudo",
        "ip",
        "netns",
        "exec",
        "nlab-tenant",
        "ping",
        "-n",
        "-M",
        "do",
        "-s",
        "1372",
        "-c",
        "3",
        "-i",
        "0.2",
        "-W",
        "1",
        external,
    )
    assert "3 packets transmitted, 3 received, 0% packet loss" in text
    write_atomic(
        ROOT / f"runtime/gateways/{name}-to-{external}.json",
        json.dumps({"source": name, "external": external, "packets": text}, indent=2) + "\n",
        0o600,
    )
