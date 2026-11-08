"""Exercise silent management access loss while guest NIC carriers remain up."""

import json
import re
import subprocess
import tempfile
import time
from contextlib import ExitStack
from ipaddress import ip_interface
from typing import Any

import pytest
from maas_bird.runtime import write_atomic
from test_native_path_measurements import _flow, _replies, _stop
from test_native_tor_failover import instance
from test_native_workers import PROJECT, ROOT, WORKERS, guest


def _state(stage):
    return {
        key: value["value"]
        for key, value in json.loads(
            (ROOT / f"runtime/terraform/lab/{stage}/terraform.tfstate").read_text()
        )["outputs"].items()
    }


def _wait(observe, expected, limit=15):
    started = time.monotonic()
    while True:
        value = observe()
        if value == expected:
            return time.monotonic() - started
        assert time.monotonic() - started < limit, value
        time.sleep(0.2)


def _stream_progress(streams, name):
    before = {
        label: len(_replies(streams[(name, label)][1])) for label in ("management", "general")
    }

    def advancing():
        for label in before:
            process = streams[(name, label)][0]
            assert process.poll() is None, f"{name}/{label} stream exited: {process.returncode}"
        return all(
            len(_replies(streams[(name, label)][1])) >= count + 5
            for label, count in before.items()
        )

    _wait(advancing, True)
    return {
        "before": before,
        "after": {label: len(_replies(streams[(name, label)][1])) for label in before},
    }


def _routing_identity(name, worker):
    links = json.loads(guest(name, "ip", "-j", "-d", "link", "show"))
    fast = {worker["interfaces"][nic]["name"] for nic in ("pf0", "pf1")}
    fast |= {vlan["name"] for vlan in worker["vlans"].values()}
    snapshot = {
        link["ifname"]: {
            key: link.get(key)
            for key in (
                "ifindex",
                "address",
                "permaddr",
                "flags",
                "link",
                "link_index",
                "linkinfo",
            )
        }
        for link in links
        if link["ifname"] in fast
    }
    assert len(snapshot) == 12
    bgp = guest(name, "birdc", "show", "protocols")
    sessions = re.findall(r"(?m)^(\S+)\s+BGP\s+.*Established", bgp)
    assert len(sessions) == 10
    bfd = guest(name, "birdc", "show", "bfd", "sessions")
    scopes = re.findall(r"(?m)^fe80:\S*\s+(\S+)\s+Up\s", bfd)
    assert len(scopes) == 10
    return {"fast_links": snapshot, "bgp": sorted(sessions), "bfd_interfaces": sorted(scopes)}


def _bond_identity(name, worker):
    links = json.loads(guest(name, "ip", "-j", "-d", "link", "show"))
    bond = next(link for link in links if link["ifname"] == "bond-mgmt")
    for nic in ("mgmt0", "mgmt1"):
        link = next(link for link in links if link["ifname"] == worker["interfaces"][nic]["name"])
        assert link.get("permaddr", link["address"]) == worker["interfaces"][nic]["mac"]
    addresses = json.loads(guest(name, "ip", "-j", "-4", "address", "show", "bond-mgmt"))[0]
    return {
        "mac": bond["address"],
        "addresses": sorted((row["local"], row["prefixlen"]) for row in addresses["addr_info"]),
    }


def _access_ready(switch):
    if instance(switch)["status"] != "Running":
        return False
    result = subprocess.run(
        [
            "lxc",
            "exec",
            "--project",
            PROJECT,
            switch,
            "--",
            "sh",
            "-ceu",
            'test "$(cat /sys/class/net/br-access/brif/uplink/state)" = 3; '
            'test "$(cat /sys/class/net/br-access/brif/downlink/state)" = 3',
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.returncode == 0


@pytest.mark.parametrize("side", ("a", "b"))
def test_native_access_switch_loss_with_guest_carriers_up(side):
    switches = _state("15-management-access")["switches"]
    networks = _state("00-networks")
    machines = _state("30-machines")["machines"]
    workers = _state("40-networking")["workers"]
    assert set(switches) == {"a", "b"}
    assert switches["a"]["network"] != switches["b"]["network"]
    switch_identities = {}
    for slot in ("a", "b"):
        switch = switches[slot]
        identity = instance(switch["name"])
        assert identity["type"] == "container" and identity["status"] == "Running"
        assert identity["config"]["volatile.uuid"] == switch["uuid"]
        assert identity["config"]["user.maas-bird-owner"] == "network-lab"
        assert identity["config"]["limits.cpu"] == "1"
        assert identity["config"]["limits.memory"] == "768MiB"
        assert identity["expanded_config"].get("security.privileged", "false") == "false"
        assert _access_ready(switch["name"])
        switch_identities[slot] = {
            "devices": identity["devices"],
            "config": {
                key: value
                for key, value in identity["config"].items()
                if not key.startswith("volatile.") or key == "volatile.uuid"
            },
        }
    switch = switches[side]["name"]
    peer_switch = switches["b" if side == "a" else "a"]["name"]
    network = ip_interface(networks["management_cidr"]).network
    gateway, controller = str(network.network_address + 1), str(network.network_address + 10)
    originals, selected, others, bond_before, routing_before = {}, {}, {}, {}, {}
    for name in WORKERS:
        worker, identity = workers[name], instance(name)
        assert identity["type"] == "virtual-machine" and identity["status"] == "Running"
        assert identity["config"]["volatile.uuid"] == worker["system_uuid"]
        assert identity["config"]["user.maas-bird-owner"] == "network-lab"
        for index, slot in enumerate(("a", "b")):
            nic = f"mgmt{index}"
            assert machines[name]["management_networks"][nic] == switches[slot]["network"]
            assert identity["devices"][nic]["network"] == switches[slot]["network"]
        selected[name] = worker["interfaces"]["mgmt0" if side == "a" else "mgmt1"]["name"]
        others[name] = worker["interfaces"]["mgmt1" if side == "a" else "mgmt0"]["name"]
        originals[name] = guest(
            name, "cat", "/sys/class/net/bond-mgmt/bonding/active_slave"
        ).strip()
        assert originals[name] in (selected[name], others[name])
        for parameter, expected in {
            "miimon": "0",
            "arp_interval": "200",
            "arp_validate": "active 1",
            "fail_over_mac": "none 0",
            "arp_ip_target": gateway,
        }.items():
            assert (
                guest(name, "cat", f"/sys/class/net/bond-mgmt/bonding/{parameter}").strip()
                == expected
            )
        bond_before[name] = _bond_identity(name, worker)
        routing_before[name] = _routing_identity(name, worker)
    receipt: dict[str, Any] = {
        "side": side,
        "switch": switch,
        "switch_uuid": switches[side]["uuid"],
        "original_active": originals,
        "before_bonds": bond_before,
        "before_routing": routing_before,
        "management_target": controller,
        "arp_target": gateway,
        "phases": {},
        "flows": {},
        "workers": {},
    }

    def restore():
        current = instance(switch)
        assert current["config"]["volatile.uuid"] == switches[side]["uuid"]
        assert current["config"]["user.maas-bird-owner"] == "network-lab"
        if current["status"] != "Running":
            subprocess.run(["lxc", "start", "--project", PROJECT, switch], check=True, timeout=20)
        _wait(lambda: _access_ready(switch), True, 30)
        for name in WORKERS:
            original = originals[name]
            _wait(
                lambda name=name, original=original: guest(
                    name, "cat", f"/sys/class/net/{original}/bonding_slave/mii_status"
                ).strip(),
                "up",
                30,
            )
            guest(name, "ip", "link", "set", "bond-mgmt", "type", "bond", "active_slave", original)
            _wait(
                lambda name=name: guest(
                    name, "cat", "/sys/class/net/bond-mgmt/bonding/active_slave"
                ).strip(),
                original,
            )

    streams = {}
    try:
        for name in WORKERS:
            guest(
                name,
                "ip",
                "link",
                "set",
                "bond-mgmt",
                "type",
                "bond",
                "active_slave",
                selected[name],
            )
            _wait(
                lambda name=name: guest(
                    name, "cat", "/sys/class/net/bond-mgmt/bonding/active_slave"
                ).strip(),
                selected[name],
            )
        with ExitStack() as stack:
            for name in WORKERS:
                remote = next(
                    host for host in WORKERS if workers[host]["rack"] != workers[name]["rack"]
                )
                source, target = (
                    f"10.205.10.{20 + WORKERS.index(name)}",
                    f"10.205.10.{20 + WORKERS.index(remote)}",
                )
                for label, address, destination in (
                    ("management", "bond-mgmt", controller),
                    ("general", source, target),
                ):
                    output, errors = (
                        stack.enter_context(tempfile.TemporaryFile()) for _ in range(2)
                    )
                    process = subprocess.Popen(
                        [
                            "lxc",
                            "exec",
                            "--project",
                            PROJECT,
                            name,
                            "--",
                            "timeout",
                            "150",
                            "ping",
                            "-D",
                            "-n",
                            "-i",
                            "0.1",
                            "-W",
                            "1",
                            "-I",
                            address,
                            destination,
                        ],
                        stdout=output,
                        stderr=errors,
                    )
                    streams[(name, label)] = (process, output, errors)
                    stack.callback(_stop, process)
            try:

                def startup_ready():
                    assert all(value[0].poll() is None for value in streams.values())
                    return all(len(_replies(value[1])) >= 5 for value in streams.values())

                _wait(startup_ready, True)
                receipt["phases"]["fault_started"] = time.time()
                started = time.monotonic()
                assert instance(switch)["config"]["volatile.uuid"] == switches[side]["uuid"]
                subprocess.run(
                    ["lxc", "stop", "--project", PROJECT, switch, "--force"],
                    check=True,
                    timeout=20,
                )
                assert instance(switch)["status"] == "Stopped"
                for name in WORKERS:
                    assert (
                        guest(name, "cat", f"/sys/class/net/{selected[name]}/carrier").strip()
                        == "1"
                    )
                    _wait(
                        lambda name=name: guest(
                            name, "cat", "/sys/class/net/bond-mgmt/bonding/active_slave"
                        ).strip(),
                        others[name],
                    )
                    receipt["workers"][name] = {
                        "active_change_observed_since_fault_seconds": time.monotonic() - started,
                        "physical_carrier_during_loss": 1,
                    }
                    receipt["workers"][name]["held_stream_progress"] = _stream_progress(
                        streams, name
                    )
                    assert _bond_identity(name, workers[name]) == bond_before[name]
                    assert _routing_identity(name, workers[name]) == routing_before[name]
                    assert (
                        guest(name, "cat", f"/sys/class/net/{selected[name]}/carrier").strip()
                        == "1"
                    )
                receipt["held_fdb"] = json.loads(
                    guest(peer_switch, "bridge", "-j", "fdb", "show", "br", "br-access")
                )
                receipt["phases"]["held_checked"] = time.time()
            finally:
                try:
                    restore()
                    receipt["phases"]["restored"] = time.time()
                    for name in WORKERS:
                        receipt.setdefault("restored_stream_progress", {})[name] = (
                            _stream_progress(streams, name)
                        )
                finally:
                    for (name, label), (process, output, errors) in streams.items():
                        _stop(process)
                        receipt["flows"][f"{name}/{label}"] = _flow(process, output, errors)
    finally:
        try:
            restore()
            receipt["after_bonds"] = {
                name: _bond_identity(name, workers[name]) for name in WORKERS
            }
            receipt["after_routing"] = {
                name: _routing_identity(name, workers[name]) for name in WORKERS
            }
            for slot in ("a", "b"):
                current = instance(switches[slot]["name"])
                assert current["status"] == "Running"
                assert current["devices"] == switch_identities[slot]["devices"]
                assert {
                    key: value
                    for key, value in current["config"].items()
                    if not key.startswith("volatile.") or key == "volatile.uuid"
                } == switch_identities[slot]["config"]
        finally:
            directory = ROOT / "runtime/terraform/acceptance/management-access"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_atomic(
                directory / f"{side}-{time.time_ns()}.json",
                json.dumps(receipt, indent=2) + "\n",
                0o600,
            )
    assert receipt["after_bonds"] == bond_before
    assert receipt["after_routing"] == routing_before
    for flow in receipt["flows"].values():
        assert flow["transmitted"] and flow["received"]
        assert flow["duplicate_replies"] == flow["reordered_replies"] == 0
    for name in WORKERS:
        gp = receipt["flows"][f"{name}/general"]
        sequences = [int(value) for value in re.findall(r"icmp_seq=(\d+)", gp["stdout"])]
        assert len(set(sequences)) == max(sequences) - min(sequences) + 1, gp
