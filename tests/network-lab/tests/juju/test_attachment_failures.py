"""Inject an owned rack-cabling error and require native admission rejection."""

import importlib
import json
import subprocess
import time
from pathlib import Path

import jubilant
import pytest

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "maas-bird-network-lab"
pytestmark = pytest.mark.integration


def lxc(*args):
    return subprocess.check_output(["lxc", *args], text=True, timeout=120)


def neighbors(juju, observer_unit):
    result = juju.exec("sudo", "lldpcli", "-f", "json", "show", "neighbors", unit=observer_unit)
    document = json.loads(result.stdout)
    rows = document["lldp"].get("interface", [])
    if isinstance(rows, dict):
        rows = [rows]
    chassis = {
        details["id"]["value"]
        for row in rows
        for details in row.get("enp7s0", {}).get("chassis", {}).values()
        if details["id"]["type"] == "mac"
    }
    return chassis, document


def wait_neighbor(juju, observer_unit, chassis):
    deadline = time.monotonic() + 150
    while True:
        found, document = neighbors(juju, observer_unit)
        if found == {chassis}:
            return document
        assert time.monotonic() < deadline, document
        time.sleep(2)


def refresh(juju):
    current = juju.cli("config", "nic-r1b", "attachments").strip()
    # A formatting-only config event requests a fresh native observation.
    value = json.dumps(json.loads(current), indent=None if "\n" in current else 2)
    juju.config("nic-r1b", {"attachments": value})


def attachment_state(juju, bird_unit, observer_unit):
    info = json.loads(juju.cli("show-unit", bird_unit, "--format=json"))[bird_unit]
    relation = next(r for r in info["relation-info"] if r["endpoint"] == "attachments")
    cards = json.loads(relation["related-units"][observer_unit]["data"]["cards"])
    return {a["id"]: a for card in cards for a in card["attachments"]}


def advertise(tor):
    lxc(
        "exec",
        "fabric",
        "--project",
        PROJECT,
        "--",
        "docker",
        "exec",
        "clab-maas-bird-fabric-" + tor,
        "lldpcli",
        "update",
    )


def test_wrong_rack_cabling_withdraws_only_the_affected_attachment():
    vm = json.loads(lxc("query", f"/1.0/instances/probe-alt?project={PROJECT}"))
    assert vm["config"]["user.maas-bird-owner"] == "network-lab"
    assert vm["devices"]["pf0"]["network"] == "nlab-probe-pf0"
    assert vm["devices"]["pf0"]["vlan.tagged"] == "3400-3404"
    for name in ("fabric", "probe-r2a", "probe-r2b"):
        owned = json.loads(lxc("query", f"/1.0/instances/{name}?project={PROJECT}"))
        assert owned["config"]["user.maas-bird-owner"] == "network-lab"
    juju = jubilant.Juju(model="network-lab:network-lab")
    observers = ("nic-r1a", "nic-r1b", "nic-r2a", "nic-r2b")
    juju.wait(lambda s: jubilant.all_active(s, *observers), timeout=180)
    workers = importlib.import_module("juju_workers")
    bird = workers.worker_binding(juju, "probe-alt", "bird-r1b")
    observer = workers.worker_binding(juju, "probe-alt", "nic-r1b")
    assert observer.machine == bird.machine
    uid = juju.exec(
        "sudo", "cat", "/sys/class/dmi/id/product_uuid", unit=observer.unit
    ).stdout.strip()
    assert uid == vm["config"]["volatile.uuid"]
    result = {"started": time.time(), "before": attachment_state(juju, bird.unit, observer.unit)}
    assert len(result["before"]) == 10
    assert all(a["ready"] is True for a in result["before"].values())
    try:
        try:
            lxc(
                "config",
                "device",
                "set",
                "probe-alt",
                "pf0",
                "network=nlab-r2-pf0",
                "--project",
                PROJECT,
            )
            advertise("r2a")
            result["wrong_rack_lldp"] = wait_neighbor(juju, observer.unit, "02:00:00:fa:00:03")
            refresh(juju)
            juju.wait(lambda s: jubilant.all_waiting(s, "nic-r1b"), timeout=180)
            state = attachment_state(juju, bird.unit, observer.unit)
            result["wrong_rack_attachments"] = state
            rejected = state["probe-alt-gp-r1a"]
            assert rejected["ready"] is False
            assert rejected["observed_function"] == {"controller": 0, "pf": 0}
            assert rejected["chassis_id"] == "02:00:00:fa:00:01"
            assert state["probe-alt-gp-r1b"]["ready"] is True
            affected = [a for identity, a in state.items() if identity.endswith("-r1a")]
            surviving = [a for identity, a in state.items() if identity.endswith("-r1b")]
            assert len(affected) == len(surviving) == 5
            assert all(a["ready"] is False for a in affected)
            assert all(a["observed_function"] == {"controller": 0, "pf": 0} for a in affected)
            assert all(a["ready"] is True for a in surviving)
            assert jubilant.all_active(juju.status(), "nic-r1a", "nic-r2a", "nic-r2b")
        finally:
            lxc(
                "config",
                "device",
                "set",
                "probe-alt",
                "pf0",
                "network=nlab-probe-pf0",
                "--project",
                PROJECT,
            )
            advertise("r1a")
            result["restored_lldp"] = wait_neighbor(juju, observer.unit, "02:00:00:fa:00:01")
            refresh(juju)
            juju.wait(lambda s: jubilant.all_active(s, *observers), timeout=180)
            result["restored_attachments"] = attachment_state(juju, bird.unit, observer.unit)
            assert all(a["ready"] is True for a in result["restored_attachments"].values())
            result["restored_at"] = time.time()
    finally:
        output = ROOT / "runtime/attachment-faults"
        output.mkdir(parents=True, exist_ok=True)
        (output / "wrong-rack.json").write_text(json.dumps(result, indent=2) + "\n")
