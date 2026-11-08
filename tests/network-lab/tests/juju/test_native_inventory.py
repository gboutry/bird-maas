"""Live MAAS authority through packed charms and tagged host-to-ToR adjacencies."""

import importlib
import json
import subprocess
from pathlib import Path

import jubilant
import pytest
from maas_bird.inventory import read_response
from maas_bird.models import Attachments

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "maas-bird-network-lab"
pytestmark = pytest.mark.integration
WORKERS = [
    ("probe", "bird-r1a", "r1"),
    ("probe-alt", "bird-r1b", "r1"),
    ("probe-r2a", "bird-r2a", "r2"),
    ("probe-r2b", "bird-r2b", "r2"),
]


@pytest.mark.parametrize(
    ("name", "app", "rack_id"),
    WORKERS,
)
def test_native_maas_inventory(monkeypatch, name, app, rack_id):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    maas = importlib.import_module("commission").maas

    instance = json.loads(
        subprocess.check_output(
            ["lxc", "query", f"/1.0/instances/{name}?project={PROJECT}"], text=True
        )
    )
    assert instance["config"]["user.maas-bird-owner"] == "network-lab"
    juju = jubilant.Juju(model="network-lab:network-lab")
    status = juju.wait(lambda s: jubilant.all_active(s, "inventory"), timeout=180)
    binding = importlib.import_module("juju_workers").worker_binding(juju, name, app)
    unit_name = binding.unit
    assert status.apps[app].units[unit_name].machine == binding.machine
    observed_uuid = juju.exec(
        "sudo", "cat", "/sys/class/dmi/id/product_uuid", unit=unit_name
    ).stdout.strip()
    assert observed_uuid == instance["config"]["volatile.uuid"]
    unit = json.loads(juju.cli("show-unit", unit_name, "--format=json"))[unit_name]
    relations = [r for r in unit["relation-info"] if r["endpoint"] == "inventory"]
    assert len(relations) == 1
    response = read_response(relations[0]["application-data"], unit_name)
    assert response.scope_state == "ready"
    assert response.system_uuid == observed_uuid
    assert response.rack == rack_id
    assert response.host is not None
    machine = maas("machine", "read", response.host.machine_id)
    assert machine["status_name"] == "Deployed"
    assert machine["hostname"] == name
    assert machine["hardware_uuid"] == observed_uuid
    assert [t for t in machine["tag_names"] if t.startswith("rack-")] == ["rack-" + rack_id]
    policy = importlib.import_module("lab_policy").read("inventory.yaml")["inventory"]
    identities = json.loads(policy["host-identities"])
    allocation = next(i for i in identities if i["machine_id"] == machine["system_id"])
    assert response.host.local_asn == allocation["local_asn"]
    assert str(response.host.router_id) == allocation["router_id"]
    rack = next(r for r in json.loads(policy["rack-profiles"]) if r["id"] == rack_id)
    assert response.host.peers is not None
    assert [p.model_dump(mode="json") for p in response.host.peers] == rack["peers"]
    assert set(response.host.model_dump()) == {
        "machine_id",
        "system_uuid",
        "rack",
        "local_asn",
        "router_id",
        "peers",
    }
    assert "api-key" not in json.dumps(relations)


@pytest.mark.parametrize(("name", "app", "rack_id"), WORKERS)
@pytest.mark.parametrize("parent", ("enp7s0", "enp8s0"))
def test_host_to_tor_tagged_carrier(name, app, rack_id, parent):
    """Probe both physical carriers directly; this does not assert host routing."""
    instance = json.loads(
        subprocess.check_output(
            ["lxc", "query", f"/1.0/instances/{name}?project={PROJECT}"], text=True
        )
    )
    assert instance["config"]["user.maas-bird-owner"] == "network-lab"
    juju = jubilant.Juju(model="network-lab:network-lab")
    binding = importlib.import_module("juju_workers").worker_binding(juju, name, app)
    unit = binding.unit
    assert juju.status().apps[app].units[unit].machine == binding.machine
    native_uuid = juju.exec(
        "sudo", "cat", "/sys/class/dmi/id/product_uuid", unit=unit
    ).stdout.strip()
    assert native_uuid == instance["config"]["volatile.uuid"]
    carrier = "pf0" if parent == "enp7s0" else "pf1"
    expected_network = "nlab-probe-" if rack_id == "r1" else "nlab-r2-"
    assert instance["devices"][carrier]["network"] == expected_network + carrier
    assert instance["devices"][carrier]["vlan.tagged"] == "3400-3404"
    result = juju.exec(
        "ping", "-6", "-c", "3", "-W", "2", "-I", parent + ".3404", "fe80::1", unit=unit
    )
    assert "3 packets transmitted, 3 received, 0% packet loss" in result.stdout


@pytest.mark.parametrize(("name", "app", "rack_id"), WORKERS)
def test_native_attachment_readiness(name, app, rack_id):
    juju = jubilant.Juju(model="network-lab:network-lab")
    observer = app.replace("bird-", "nic-")
    status = juju.wait(lambda s: jubilant.all_active(s, observer), timeout=180)
    bindings = importlib.import_module("juju_workers")
    bird = bindings.worker_binding(juju, name, app)
    observed = bindings.worker_binding(juju, name, observer)
    assert status.apps[observer].units[observed.unit].machine == bird.machine
    policy = importlib.import_module("lab_policy").read("all-attachments.yaml")[observer]
    intended = json.loads(policy["attachments"])
    assert len(intended) == 10
    neighbors = json.loads(
        juju.exec("sudo", "lldpcli", "-f", "json", "show", "neighbors", unit=observed.unit).stdout
    )["lldp"]["interface"]
    if isinstance(neighbors, dict):
        neighbors = [neighbors]
    for attachment in intended:
        rows = [r[attachment["uplink"]] for r in neighbors if attachment["uplink"] in r]
        assert len(rows) == 1
        chassis = rows[0]["chassis"].values()
        assert {c["id"]["type"] for c in chassis} == {"mac"}
        assert {c["id"]["value"] for c in chassis} == {attachment["chassis_id"]}
    unit = bird.unit
    info = json.loads(juju.cli("show-unit", unit, "--format=json"))[unit]
    relation = next(r for r in info["relation-info"] if r["endpoint"] == "attachments")
    bag = relation["related-units"][observed.unit]["data"]
    document = Attachments(cards=json.loads(bag["cards"]))
    assert document.cards is not None and len(document.cards) == 2
    actual = [a for c in document.cards for a in c.attachments or ()]
    assert {a.id for a in actual} == {a["id"] for a in intended}
    assert all(a.ready is True and a.observed_function == a.function for a in actual)


@pytest.mark.parametrize(("name", "app", "rack_id"), WORKERS)
def test_persistent_general_source_policy(name, app, rack_id):
    juju = jubilant.Juju(model="network-lab:network-lab")
    unit = importlib.import_module("juju_workers").worker_binding(juju, name, app).unit
    hosts = json.loads((ROOT / "maas/network-model/hosts.json").read_text())
    source = hosts[name]["loopback"].split("/")[0]
    addresses = json.loads(
        juju.exec("ip", "-j", "-4", "address", "show", "dev", "lo", unit=unit).stdout
    )
    assert any(a["local"] == source and a["prefixlen"] == 32 for a in addresses[0]["addr_info"])
    rules = json.loads(juju.exec("ip", "-j", "-4", "rule", "show", unit=unit).stdout)
    managed = [r for r in rules if r["priority"] == 14000]
    assert len(managed) == 1
    assert managed[0]["src"] == source
    assert str(managed[0]["table"]) == "4400"
    assert managed[0]["protocol"] == "static"
    routes = json.loads(
        juju.exec("ip", "-j", "-4", "route", "show", "table", "4400", unit=unit).stdout
    )
    assert any(
        r.get("type") == "unreachable"
        and r.get("dst") == "default"
        and r.get("protocol") == "static"
        and r.get("metric") == 42760
        for r in routes
    )
    probe = juju.exec(
        "python3",
        "-c",
        "import subprocess,json,sys; r=subprocess.run(['ip','-4','route','get',"
        "'10.203.240.10','from',sys.argv[1]],capture_output=True,text=True); "
        "print(json.dumps({'return_code':r.returncode,'stdout':r.stdout,'stderr':r.stderr}))",
        source,
        unit=unit,
    )
    assert json.loads(probe.stdout)["return_code"] == 2
    control = juju.exec("ping", "-c", "2", "-W", "2", "10.203.240.10", unit=unit)
    assert "2 packets transmitted, 2 received, 0% packet loss" in control.stdout
