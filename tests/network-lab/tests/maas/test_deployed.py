"""Live checks of the owned MAAS-deployed probe, including reboot and bond faults.

Run explicitly after deploy_probe.py. These tests mutate only the owned guest.
They do not prove BGP or forwarding on the fast networks.
"""

import json
import re
import subprocess
import time
from itertools import pairwise
from pathlib import Path
from typing import TypedDict

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"
PROJECT = "maas-bird-network-lab"
pytestmark = pytest.mark.integration


class Failure(TypedDict):
    failed: str
    surviving: str
    failed_at: float
    verified_at: float


def command(*args, timeout=120, check=True):
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=timeout)


def ssh(text, *, check=True, timeout=120):
    return command(
        "ssh",
        "-i",
        str(RUNTIME / "ssh-key"),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "UserKnownHostsFile=" + str(RUNTIME / "deployed-known-hosts"),
        "-o",
        "ConnectTimeout=5",
        "ubuntu@10.203.240.20",
        text,
        check=check,
        timeout=timeout,
    )


def wait_cloud_init():
    result = ssh("sudo cloud-init status --wait --format json", check=False, timeout=600)
    status = json.loads(result.stdout)
    (RUNTIME / "cloud-init-network-gate.json").write_text(json.dumps(status, indent=2) + "\n")
    assert result.returncode in (0, 2)
    assert status["status"] == "done"
    assert status["errors"] == []
    return result


def snapshot(label):
    commands = {
        "addresses": "ip -j address show",
        "links": "ip -j -d link show",
        "local_routes": "ip -j -4 route show table local",
        "default_routes": "ip -j -4 route show default",
        "bond": "cat /proc/net/bonding/bond0",
        "boot_id": "cat /proc/sys/kernel/random/boot_id",
        "netplan": "sudo python3 -c 'import json,pathlib; "
        "print(json.dumps({p.name:p.read_text() for p in "
        'pathlib.Path("/etc/netplan").glob("*.yaml")}))'
        "'",
    }
    values = {name: ssh(cmd).stdout.strip() for name, cmd in commands.items()}
    (RUNTIME / f"validation-{label}.json").write_text(json.dumps(values, indent=2) + "\n")
    return values


def assert_network(values):
    addresses = {item["ifname"]: item for item in json.loads(values["addresses"])}
    links = {item["ifname"]: item for item in json.loads(values["links"])}
    routes = json.loads(values["local_routes"])
    for parent, address in (("enp7s0", "10.205.1.11"), ("enp8s0", "10.205.1.12")):
        name = parent + ".3404"
        assert any(
            a["local"] == address and a["prefixlen"] == 32 for a in addresses[name]["addr_info"]
        )
        assert links[name]["linkinfo"]["info_kind"] == "vlan"
        assert links[name]["linkinfo"]["info_data"]["id"] == 3404
        observed_parent = links[name].get("link")
        if observed_parent is None:
            observed_parent = next(
                item["ifname"]
                for item in links.values()
                if item["ifindex"] == links[name]["link_index"]
            )
        assert observed_parent == parent
        assert any(r.get("dst") == address and r.get("dev") == name for r in routes)
        assert "master" not in links[parent]
    assert "Bonding Mode: fault-tolerance (active-backup)" in values["bond"]
    assert {"enp5s0", "enp6s0"} == set(re.findall(r"Slave Interface: (\S+)", values["bond"]))
    assert any(a["local"] == "10.203.240.20" for a in addresses["bond0"]["addr_info"])
    defaults = json.loads(values["default_routes"])
    assert len(defaults) == 1 and defaults[0]["dev"] == "bond0"
    assert defaults[0]["gateway"] == "10.203.240.1"
    configs = [yaml.safe_load(text)["network"] for text in json.loads(values["netplan"]).values()]
    bond = next(
        config["bonds"]["bond0"] for config in configs if "bond0" in config.get("bonds", {})
    )
    assert bond["parameters"]["mode"] == "active-backup"
    assert set(bond["interfaces"]) == {"enp5s0", "enp6s0"}
    vlans = {name: value for config in configs for name, value in config.get("vlans", {}).items()}
    for name, address in (("enp7s0.3404", "10.205.1.11/32"), ("enp8s0.3404", "10.205.1.12/32")):
        assert vlans[name]["id"] == 3404
        assert vlans[name]["addresses"] == [address]


@pytest.fixture
def guest():
    receipt = json.loads((RUNTIME / "deploy-interface32.json").read_text())
    instance = json.loads(
        command("lxc", "query", f"/1.0/instances/probe?project={PROJECT}").stdout
    )
    assert instance["config"].get("user.maas-bird-owner") == "network-lab"
    machine = json.loads(
        command(
            "lxc",
            "exec",
            "--project",
            PROJECT,
            "maas",
            "--",
            "maas",
            "lab",
            "machine",
            "read",
            receipt["system_id"],
        ).stdout
    )
    assert machine["status_name"] == "Deployed"
    assert machine["hardware_uuid"] == instance["config"]["volatile.uuid"]
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        result = ssh("sudo cat /sys/class/dmi/id/product_uuid", check=False)
        if result.returncode == 0:
            break
        time.sleep(3)
    else:
        pytest.fail("Deployed guest did not become reachable over SSH")
    uuid = result.stdout.strip()
    assert uuid == machine["hardware_uuid"]
    wait_cloud_init()


def test_cloud_init_clean(guest):
    # Retain the original strict lifecycle requirement as its own result.
    assert wait_cloud_init().returncode == 0


def test_installed_network(guest):
    assert_network(snapshot("installed"))


def test_reboot_persistence(guest):
    before = snapshot("before-reboot")
    reboot = ssh("sudo systemctl reboot", check=False)
    assert reboot.returncode in (0, 255)
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        result = ssh("cat /proc/sys/kernel/random/boot_id", check=False)
        if result.returncode == 0 and result.stdout.strip() != before["boot_id"]:
            break
        time.sleep(3)
    else:
        pytest.fail("No successful SSH connection with a new boot ID")
    wait_cloud_init()
    after = snapshot("after-reboot")
    assert after["boot_id"] != before["boot_id"]
    assert_network(after)


def test_management_bond_both_directions(guest):
    samples: list[Failure] = []
    probe_path = RUNTIME / "management-failover-ping.txt"
    with probe_path.open("w") as output:
        probe = subprocess.Popen(
            ["ping", "-D", "-n", "-i", "0.05", "10.203.240.20"],
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            time.sleep(1)
            for _ in range(2):
                active = ssh("cat /sys/class/net/bond0/bonding/active_slave").stdout.strip()
                assert active in {"enp5s0", "enp6s0"}
                failed_at = time.time()
                try:
                    down = ssh(f"sudo ip link set {active} down", check=False)
                    assert down.returncode in (0, 255)
                    expected = "enp6s0" if active == "enp5s0" else "enp5s0"
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        current = ssh("cat /sys/class/net/bond0/bonding/active_slave", check=False)
                        if current.returncode == 0 and current.stdout.strip() == expected:
                            break
                        time.sleep(0.2)
                    else:
                        pytest.fail("Management bond did not select its surviving NIC")
                    time.sleep(1)
                    samples.append(
                        {
                            "failed": active,
                            "surviving": expected,
                            "failed_at": failed_at,
                            "verified_at": time.time(),
                        }
                    )
                finally:
                    ssh(f"sudo ip link set {active} up")
        finally:
            probe.terminate()
            probe.wait(timeout=5)
    replies = [
        (float(t), int(seq))
        for t, seq in re.findall(r"\[([0-9.]+)\].*icmp_seq=(\d+)", probe_path.read_text())
    ]
    assert len(replies) > 10
    for sample in samples:
        assert any(sample["failed_at"] < t < sample["verified_at"] for t, _ in replies)
    assert {s["failed"] for s in samples} == {"enp5s0", "enp6s0"}
    result = {
        "failures": samples,
        "replies": len(replies),
        "max_reply_gap_seconds": max(b[0] - a[0] for a, b in pairwise(replies)),
        "boundary": "ICMP reply gap includes 50 ms probe spacing and scheduling",
    }
    (RUNTIME / "management-failover.json").write_text(json.dumps(result, indent=2) + "\n")
    assert_network(snapshot("after-bond-faults"))
