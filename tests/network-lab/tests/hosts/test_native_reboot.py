"""Owned native worker restart: persistent networking and real Juju execution."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from maas_bird.runtime import write_atomic

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "maas-bird-network-lab-tf"
WORKERS = ("probe", "probe-alt", "probe-r2a", "probe-r2b")
pytestmark = pytest.mark.integration


def command(*args, timeout=30, check=True, env=None):
    return subprocess.run(
        args, capture_output=True, text=True, check=check, timeout=timeout, env=env
    )


def guest(name, *args, **kwargs):
    return command("lxc", "exec", "--project", PROJECT, name, "--", *args, **kwargs)


def instance(name):
    row = json.loads(command("lxc", "query", f"/1.0/instances/{name}?project={PROJECT}").stdout)
    assert row["type"] == "virtual-machine"
    assert row["config"]["user.maas-bird-owner"] == "network-lab"
    assert row["config"]["limits.cpu"] == "1"
    assert row["config"]["limits.memory"] == "4GiB"
    return row


def juju_boot_id(machine):
    response = json.loads(
        command(
            "/snap/juju/current/bin/juju",
            "exec",
            "--model=nlab-tf:network-lab",
            f"--machine={machine}",
            "--wait=30s",
            "--format=json",
            "--",
            "cat",
            "/proc/sys/kernel/random/boot_id",
            timeout=45,
            env={**os.environ, "JUJU_DATA": str(ROOT / "runtime/terraform/juju-client")},
        ).stdout
    )[machine]
    assert response["status"] == "completed" and response["results"]["return-code"] == 0
    return response["results"]["stdout"].strip()


def network_state(name):
    rows = json.loads(guest(name, "ip", "-j", "-d", "address", "show").stdout)
    vlans = [row for row in rows if row.get("linkinfo", {}).get("info_kind") == "vlan"]
    assert len(vlans) == 10
    expected = {"lo", "bond-mgmt", *(row["ifname"] for row in vlans)}
    addresses = {
        row["ifname"]: sorted(
            (address["family"], address["local"], address["prefixlen"])
            for address in row["addr_info"]
            if row["ifname"] != "bond-mgmt" or address["family"] == "inet"
        )
        for row in rows
        if row["ifname"] in expected
    }
    roles = json.loads((ROOT / "maas/network-model/logical-roles.json").read_text())["roles"]
    index = WORKERS.index(name)
    origins = {
        address for family, address, prefix in addresses["lo"] if family == "inet" and prefix == 32
    }
    assert origins == {
        role["address_block"].rsplit(".", 1)[0] + f".{20 + index}" for role in roles
    }
    assert addresses["bond-mgmt"] == [("inet", f"10.203.241.{40 + 10 * index}", 24)]
    bond = next(row for row in rows if row["ifname"] == "bond-mgmt")
    assert bond["linkinfo"]["info_data"]["mode"] == "active-backup"
    parents = {}
    for row in vlans:
        parent = next(
            candidate
            for candidate in rows
            if candidate["ifname"] == row.get("link")
            or candidate["ifindex"] == row.get("link_index")
        )
        assert "master" not in parent
        parents[row["ifname"]] = {
            "parent_mac": parent.get("permaddr", parent["address"]),
            "vlan": row["linkinfo"]["info_data"]["id"],
        }
    rules = json.loads(guest(name, "ip", "-j", "-4", "rule", "show").stdout)
    policy = []
    guards = {}
    for role in roles:
        source = role["address_block"].rsplit(".", 1)[0] + f".{20 + index}"
        selected = [
            row
            for row in rules
            if row.get("src") == source
            and str(row.get("table")) == str(role["table"])
            and row.get("priority") == role["rule_priority"]
        ]
        assert len(selected) == 1
        policy.append(
            {"source": source, "table": role["table"], "priority": role["rule_priority"]}
        )
        table = json.loads(
            guest(name, "ip", "-j", "route", "show", "table", str(role["table"])).stdout
        )
        guards[role["id"]] = [
            row
            for row in table
            if row.get("type") == "unreachable"
            and row.get("dst") == "default"
            and row.get("metric") == 42760
        ]
        assert len(guards[role["id"]]) == 1
    return {
        "netplan": yaml.safe_load(guest(name, "netplan", "get").stdout),
        "addresses": addresses,
        "parents": parents,
        "policy": policy,
        "guards": guards,
    }


def cloud_init(name):
    result = guest(
        name, "cloud-init", "status", "--wait", "--format=json", check=False, timeout=120
    )
    data = json.loads(result.stdout)
    assert result.returncode in (0, 2) and data["status"] == "done" and data["errors"] == []
    return {"returncode": result.returncode, "status": data}


@pytest.mark.parametrize("name", WORKERS)
def test_native_worker_network_and_juju_survive_restart(name):
    row = instance(name)
    assert row["status"] == "Running"
    workers = json.loads(
        (ROOT / "runtime/terraform/lab/40-networking/terraform.tfstate").read_text()
    )["outputs"]["workers"]["value"]
    machines = json.loads(
        (ROOT / "runtime/terraform/lab/50-juju-machines/terraform.tfstate").read_text()
    )["outputs"]["machines"]["value"]
    uuid, machine = row["config"]["volatile.uuid"], machines[name]
    assert workers[name]["system_uuid"] == uuid
    node_id = workers[name]["system_id"]
    node = json.loads(guest("maas", "maas", "lab", "machine", "read", node_id).stdout)
    assert node["status_name"] == "Deployed" and node["hardware_uuid"] == uuid
    boot = guest(name, "cat", "/proc/sys/kernel/random/boot_id").stdout.strip()
    assert juju_boot_id(machine) == boot
    before = network_state(name)
    receipt = {
        "name": name,
        "uuid": uuid,
        "machine": machine,
        "system_id": node_id,
        "before_boot_id": boot,
        "before": before,
        "cloud_init_before": cloud_init(name),
    }
    checkpoint = f"native-reboot-{time.time_ns()}"
    command("lxc", "snapshot", "--project", PROJECT, name, checkpoint, "--no-expiry", timeout=120)
    receipt["snapshot"] = checkpoint
    try:
        command("lxc", "restart", "--project", PROJECT, name, "--timeout=60", timeout=90)
        deadline = time.monotonic() + 180
        while True:
            try:
                result = guest(
                    name, "cat", "/proc/sys/kernel/random/boot_id", check=False, timeout=10
                )
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)
                continue
            if result.returncode == 0 and result.stdout.strip() != boot:
                break
            assert time.monotonic() < deadline, result.stderr
            time.sleep(1)
        receipt["after_boot_id"] = result.stdout.strip()
        receipt["cloud_init_after"] = cloud_init(name)
        after = network_state(name)
        receipt["after"] = after
        assert after == before
        assert guest(name, "cat", "/sys/class/dmi/id/product_uuid").stdout.strip().lower() == uuid
        release = guest(name, "uname", "-r").stdout.strip()
        depends = guest(
            name, "dpkg-query", "-W", "-f=${Depends}", "linux-image-generic-hwe-24.04"
        ).stdout
        assert not release.startswith("6.8.") and f"linux-image-{release}" in depends
        receipt["kernel"] = release
        packet = guest(
            name, "ping", "-n", "-c", "3", "-W", "1", "-I", "bond-mgmt", "10.203.241.10"
        ).stdout
        assert "3 packets transmitted, 3 received" in packet
        receipt["management_packets"] = packet
        assert juju_boot_id(machine) == receipt["after_boot_id"]
        node = json.loads(guest("maas", "maas", "lab", "machine", "read", node_id).stdout)
        assert node["status_name"] == "Deployed" and node["hardware_uuid"] == uuid
        assert instance(name)["config"]["volatile.uuid"] == uuid
        receipt["passed"] = True
    finally:
        try:
            current = instance(name)
            assert current["config"]["volatile.uuid"] == uuid
            if current["status"] == "Stopped":
                command("lxc", "start", "--project", PROJECT, name, timeout=90)
            if receipt.get("passed", False):
                command("lxc", "delete", "--project", PROJECT, f"{name}/{checkpoint}", timeout=90)
            receipt["snapshot_retained"] = not receipt.get("passed", False)
        finally:
            directory = ROOT / "runtime/terraform/acceptance/worker-reboots"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_atomic(
                directory / f"{name}-{time.time_ns()}.json",
                json.dumps(receipt, indent=2) + "\n",
                0o600,
            )
