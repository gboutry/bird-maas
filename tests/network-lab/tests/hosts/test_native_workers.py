"""Live worker identities, native networking and management failover."""

import json
import os
import re
import subprocess
import time
import uuid
from ipaddress import ip_interface, ip_network
from pathlib import Path

import pytest
from maas_bird.runtime import accepted, write_atomic

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "maas-bird-network-lab-tf"
WORKERS = ("probe", "probe-alt", "probe-r2a", "probe-r2b")


def guest(name, *args):
    return subprocess.check_output(
        ["lxc", "exec", "--project", PROJECT, name, "--", *args],
        text=True,
        timeout=30,
    )


@pytest.fixture(scope="module")
def inventory():
    values = json.loads(guest("maas", "maas", "lab", "machines", "read"))
    return {node["hostname"]: node for node in values}


@pytest.fixture(scope="module")
def agents():
    result = json.loads(
        subprocess.check_output(
            [
                "/snap/juju/current/bin/juju",
                "status",
                "--model=nlab-tf:network-lab",
                "--format=json",
            ],
            env={**os.environ, "JUJU_DATA": str(ROOT / "runtime/terraform/juju-client")},
            text=True,
            timeout=60,
        )
    )
    assert len(result["machines"]) == 4
    return {node["instance-id"]: {"id": name, **node} for name, node in result["machines"].items()}


@pytest.fixture(autouse=True)
def ownership(request):
    name = request.node.callspec.params["name"]
    instance = json.loads(
        subprocess.check_output(
            ["lxc", "query", f"/1.0/instances/{name}?project={PROJECT}"],
            text=True,
            timeout=30,
        )
    )
    assert instance["type"] == "virtual-machine"
    assert instance["status"] == "Running"
    assert instance["config"]["user.maas-bird-owner"] == "network-lab"
    assert instance["config"]["limits.cpu"] == "1"
    assert instance["config"]["limits.memory"] == "4GiB"
    return instance


@pytest.mark.parametrize("name", WORKERS)
def test_native_agent_and_hwe_identity(name, ownership, inventory, agents):
    node = inventory[name]
    assert node["status_name"] == "Deployed"
    uuid = guest(name, "cat", "/sys/class/dmi/id/product_uuid").strip().lower()
    assert uuid == node["hardware_uuid"] == ownership["config"]["volatile.uuid"]
    machine = agents[node["system_id"]]
    assert machine["juju-status"]["current"] == "started"
    assert machine["juju-status"]["version"] == "3.6.29"
    assert (
        guest(name, "systemctl", "is-active", f"jujud-machine-{machine['id']}").strip() == "active"
    )
    release = guest(name, "uname", "-r").strip()
    dependencies = guest(
        name, "dpkg-query", "-W", "-f=${Depends}", "linux-image-generic-hwe-24.04"
    )
    assert f"linux-image-{release}" in dependencies
    assert not release.startswith("6.8.")
    assert 'VERSION_ID="24.04"' in guest(name, "cat", "/etc/os-release")


@pytest.mark.parametrize("name", WORKERS)
def test_native_role_addresses_and_vlan_parents(name, ownership):
    roles = json.loads((ROOT / "maas/network-model/logical-roles.json").read_text())["roles"]
    links = json.loads(guest(name, "ip", "-j", "-d", "link", "show"))
    physical = {
        link.get("permaddr", link.get("address")): link
        for link in links
        if link.get("linkinfo", {}).get("info_kind") not in {"vlan", "bond", "openvswitch"}
    }
    index = WORKERS.index(name)
    addresses = json.loads(guest(name, "ip", "-j", "-4", "address", "show", "lo"))[0]["addr_info"]
    origins = {item["local"] for item in addresses if item["prefixlen"] == 32}
    expected = {role["address_block"].rsplit(".", 1)[0] + f".{20 + index}" for role in roles}
    assert origins == expected
    vlans = [link for link in links if link.get("linkinfo", {}).get("info_kind") == "vlan"]
    assert len(vlans) == 10
    for nic in ("pf0", "pf1"):
        parent = physical[ownership["devices"][nic]["hwaddr"]]
        assert "master" not in parent
        children = [
            link
            for link in vlans
            if link.get("link_index") == parent["ifindex"] or link.get("link") == parent["ifname"]
        ]
        assert {link["linkinfo"]["info_data"]["id"] for link in children} == set(range(3400, 3405))
    rules = json.loads(guest(name, "ip", "-j", "-4", "rule", "show"))
    for role in roles:
        source = role["address_block"].rsplit(".", 1)[0] + f".{20 + index}"
        assert any(
            rule.get("src") == source
            and str(rule.get("table")) == str(role["table"])
            and rule.get("priority") == role["rule_priority"]
            for rule in rules
        )
        routes = json.loads(guest(name, "ip", "-j", "route", "show", "table", str(role["table"])))
        assert any(
            route.get("type") == "unreachable"
            and route.get("dst") == "default"
            and route.get("metric") == 42760
            for route in routes
        )


@pytest.mark.parametrize("name", WORKERS)
def test_management_bond_configuration(name):
    bond = json.loads(guest(name, "ip", "-j", "-d", "link", "show", "bond-mgmt"))[0]
    assert bond["linkinfo"]["info_data"]["mode"] == "active-backup"
    slaves = guest(name, "cat", "/sys/class/net/bond-mgmt/bonding/slaves").split()
    assert len(slaves) == 2
    network = json.loads(
        (ROOT / "runtime/terraform/lab/00-networks/terraform.tfstate").read_text()
    )["outputs"]["management_cidr"]["value"]
    gateway = str(ip_interface(network).network.network_address + 1)
    for parameter, expected in {
        "miimon": "0",
        "arp_interval": "200",
        "arp_validate": "active 1",
        "arp_ip_target": gateway,
        "fail_over_mac": "none 0",
    }.items():
        assert (
            guest(name, "cat", f"/sys/class/net/bond-mgmt/bonding/{parameter}").strip() == expected
        )
    address = json.loads(guest(name, "ip", "-j", "-4", "address", "show", "bond-mgmt"))[0][
        "addr_info"
    ]
    assert {item["local"] for item in address} == {f"10.203.241.{40 + WORKERS.index(name) * 10}"}


@pytest.mark.parametrize("name", WORKERS)
@pytest.mark.parametrize("direction", (0, 1))
def test_management_failover_packets(name, direction):
    original = guest(name, "cat", "/sys/class/net/bond-mgmt/bonding/active_slave").strip()
    slaves = sorted(guest(name, "cat", "/sys/class/net/bond-mgmt/bonding/slaves").split())
    assert len(slaves) == 2
    selected = slaves[direction]
    try:
        guest(name, "ip", "link", "set", "bond-mgmt", "type", "bond", "active_slave", selected)
        guest(name, "ip", "link", "set", selected, "down")
        deadline = time.monotonic() + 10
        while True:
            active = guest(name, "cat", "/sys/class/net/bond-mgmt/bonding/active_slave").strip()
            if active in slaves and active != selected:
                break
            assert time.monotonic() < deadline, active
            time.sleep(0.2)
        guest(name, "ping", "-n", "-c", "3", "-W", "2", "-I", "bond-mgmt", "10.203.241.10")
    finally:
        guest(name, "ip", "link", "set", selected, "up")
        deadline = time.monotonic() + 10
        while (
            guest(name, "cat", f"/sys/class/net/{original}/bonding_slave/mii_status").strip()
            != "up"
        ):
            assert time.monotonic() < deadline
            time.sleep(0.2)
        guest(name, "ip", "link", "set", "bond-mgmt", "type", "bond", "active_slave", original)
    assert guest(name, "cat", "/sys/class/net/bond-mgmt/bonding/active_slave").strip() == original
    guest(name, "ping", "-n", "-c", "2", "-W", "2", "-I", "bond-mgmt", "10.203.241.10")


@pytest.mark.parametrize("name", WORKERS)
def test_strict_cloud_init_success(name):
    result = subprocess.run(
        ["lxc", "exec", "--project", PROJECT, name, "--", "cloud-init", "status", "--format=json"],
        text=True,
        capture_output=True,
        timeout=30,
    )
    status = json.loads(result.stdout)
    assert status["extended_status"] == "done", {
        "status": status["extended_status"],
        "warning_count": len(status.get("recoverable_errors", {}).get("WARNING", [])),
    }
    assert result.returncode == 0
    assert status["errors"] == []
    assert status["recoverable_errors"] == {}


@pytest.mark.parametrize("name", WORKERS)
def test_networkd_reconfiguration_restores_bird_forwarding(name):
    state = subprocess.run(
        ["lxc", "exec", "--project", PROJECT, name, "--", "systemctl", "is-active", "bird"],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert state.returncode == 0 and state.stdout.strip() == "active", (
        "BIRD admission is required for the native recovery check"
    )
    source = f"10.205.10.{20 + WORKERS.index(name)}"
    destination = "10.205.10.22" if name in WORKERS[:2] else "10.205.10.20"

    def routes():
        return json.loads(
            guest(name, "ip", "-j", "route", "show", "table", "4400", f"{destination}/32")
        )

    before = routes()
    assert len(before) == 1
    assert before[0]["protocol"] == "bird"
    assert len(before[0]["nexthops"]) == 2
    expected = {hop["dev"] for hop in before[0]["nexthops"]}
    guest(name, "networkctl", "reconfigure", sorted(expected)[0])
    deadline = time.monotonic() + 30
    while True:
        observed = routes()
        if (
            len(observed) == 1
            and {hop["dev"] for hop in observed[0].get("nexthops", [])} == expected
        ):
            break
        assert time.monotonic() < deadline, observed
        time.sleep(0.5)
    assert observed[0]["protocol"] == "bird"
    assert all(hop["via"]["family"] == "inet6" for hop in observed[0]["nexthops"])
    guest(name, "ping", "-n", "-c", "3", "-W", "2", "-I", source, destination)


@pytest.mark.parametrize("name", WORKERS)
def test_native_lldp_receive_only_parents(name):
    configuration = guest(name, "lldpcli", "-f", "keyvalue", "show", "configuration")
    links = json.loads(guest(name, "ip", "-j", "-d", "link", "show", "type", "vlan"))
    parents = {link["link"] for link in links if link["linkinfo"]["info_data"]["id"] == 3404}
    assert len(parents) == 2
    assert "configuration.config.rx-only=yes\n" in configuration
    pattern = next(
        line.split("=", 1)[1]
        for line in configuration.splitlines()
        if line.startswith("configuration.config.iface-pattern=")
    )
    assert len(pattern.split(",")) == 2
    assert set(pattern.split(",")) == parents


@pytest.mark.parametrize("name", WORKERS)
@pytest.mark.parametrize("pf", ("pf0", "pf1"))
def test_fast_parent_failure_disables_five_children(name, pf, ownership):
    links = json.loads(guest(name, "ip", "-j", "-d", "link", "show"))
    parents = {
        key: next(
            link
            for link in links
            if link.get("permaddr", link.get("address")) == ownership["devices"][key]["hwaddr"]
        )
        for key in ("pf0", "pf1")
    }
    children = {
        key: sorted(
            link["ifname"]
            for link in links
            if link.get("linkinfo", {}).get("info_kind") == "vlan"
            and (
                link.get("link") == parent["ifname"] or link.get("link_index") == parent["ifindex"]
            )
        )
        for key, parent in parents.items()
    }
    assert all(len(names) == 5 for names in children.values())
    assert all(
        "UP" in parent["flags"] and "LOWER_UP" in parent["flags"] for parent in parents.values()
    )
    survivor = "pf1" if pf == "pf0" else "pf0"
    receipt = {
        "name": name,
        "failed_parent": parents[pf]["ifname"],
        "children": children,
        "before": {},
        "failed": {},
        "surviving": {},
    }

    def probe(interface):
        return subprocess.run(
            [
                "lxc",
                "exec",
                "--project",
                PROJECT,
                name,
                "--",
                "ping",
                "-6",
                "-n",
                "-I",
                interface,
                "-c",
                "2",
                "-i",
                "0.2",
                "-W",
                "1",
                "fe80::1",
            ],
            text=True,
            capture_output=True,
            timeout=10,
        )

    def healthy(interface):
        result = probe(interface)
        assert result.returncode == 0 and "2 packets transmitted, 2 received" in result.stdout, (
            interface,
            result.stdout,
            result.stderr,
        )
        return result.stdout

    for names in children.values():
        for interface in names:
            receipt["before"][interface] = healthy(interface)
    try:
        receipt["fault_started"] = time.monotonic()
        guest(name, "ip", "link", "set", parents[pf]["ifname"], "down")
        observed = json.loads(guest(name, "ip", "-j", "link", "show"))
        for interface in children[pf]:
            child = next(link for link in observed if link["ifname"] == interface)
            assert "LOWER_UP" not in child["flags"]
            result = probe(interface)
            receipt["failed"][interface] = {
                "flags": child["flags"],
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
            assert result.returncode != 0, (interface, result.stdout)
        for interface in children[survivor]:
            receipt["surviving"][interface] = healthy(interface)
        receipt["management_during_fault"] = guest(
            name,
            "ping",
            "-n",
            "-c",
            "3",
            "-W",
            "1",
            "-I",
            "bond-mgmt",
            "10.203.241.10",
        )
        assert "3 packets transmitted, 3 received" in receipt["management_during_fault"]
        receipt["fault_assertions_passed"] = True
    finally:
        try:
            guest(name, "ip", "link", "set", parents[pf]["ifname"], "up")
            deadline = time.monotonic() + 20
            while True:
                try:
                    observed = json.loads(guest(name, "ip", "-j", "link", "show"))
                    restored = {}
                    for names in children.values():
                        for interface in names:
                            child = next(link for link in observed if link["ifname"] == interface)
                            assert "LOWER_UP" in child["flags"]
                            restored[interface] = healthy(interface)
                    receipt["restored"] = restored
                    break
                except (AssertionError, subprocess.CalledProcessError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.2)
            receipt["management_restored"] = guest(
                name,
                "ping",
                "-n",
                "-c",
                "3",
                "-W",
                "1",
                "-I",
                "bond-mgmt",
                "10.203.241.10",
            )
            assert "3 packets transmitted, 3 received" in receipt["management_restored"]
            receipt["restored_at"] = time.monotonic()
        finally:
            directory = ROOT / "runtime/terraform/acceptance/fast-parents"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_atomic(
                directory / f"{name}-{pf}-{time.time_ns()}.json",
                json.dumps(receipt, indent=2) + "\n",
                0o600,
            )


@pytest.mark.parametrize("name", WORKERS)
def test_native_scoped_fast_peer_paths(name):
    rows = json.loads(guest(name, "ip", "-j", "-d", "link", "show", "type", "vlan"))
    assert len(rows) == 10
    packets = {}
    for row in rows:
        assert "LOWER_UP" in row["flags"]
        output = guest(
            name,
            "ping",
            "-6",
            "-n",
            "-I",
            row["ifname"],
            "-c",
            "2",
            "-i",
            "0.2",
            "-W",
            "1",
            "fe80::1",
        )
        assert "2 packets transmitted, 2 received" in output
        packets[row["ifname"]] = output
    directory = ROOT / "runtime/terraform/acceptance/fast-peers"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt = {
        "worker": name,
        "boot_id": guest(name, "cat", "/proc/sys/kernel/random/boot_id").strip(),
        "packets": packets,
    }
    write_atomic(
        directory / f"{name}-{time.time_ns()}.json", json.dumps(receipt, indent=2) + "\n", 0o600
    )


@pytest.mark.parametrize("name", WORKERS)
def test_native_lldp_matches_both_authoritative_tors(name, ownership):
    networking = json.loads(
        (ROOT / "runtime/terraform/lab/40-networking/terraform.tfstate").read_text()
    )
    switches = json.loads((ROOT / "runtime/terraform/lab/10-frr/terraform.tfstate").read_text())
    worker = networking["outputs"]["workers"]["value"][name]
    chassis = switches["outputs"]["tor_chassis"]["value"]
    assert worker["system_uuid"] == ownership["config"]["volatile.uuid"]
    result = json.loads(guest(name, "lldpcli", "-f", "json", "show", "neighbors"))
    observed = {
        interface: peer for row in result["lldp"]["interface"] for interface, peer in row.items()
    }
    expected = {
        worker["interfaces"][f"pf{index}"]["name"]: f"r{worker['rack']}{side}"
        for index, side in enumerate(("a", "b"))
    }
    directory = ROOT / "runtime/terraform/acceptance/lldp"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_atomic(
        directory / f"{name}-{time.time_ns()}.json",
        json.dumps({"expected": expected, "observed": result}, indent=2) + "\n",
        0o600,
    )
    assert set(observed) == set(expected), observed
    for interface, tor in expected.items():
        peer = observed[interface]
        assert peer["via"] == "LLDP"
        assert set(peer["chassis"]) == {tor}, peer
        assert peer["chassis"][tor]["id"] == {"type": "mac", "value": chassis[tor]}
        assert peer["port"]["descr"] == "fast0"


@pytest.mark.parametrize("name", WORKERS)
def test_native_bird_sessions_and_source_bound_role_packets(name, ownership):
    workers = json.loads(
        (ROOT / "runtime/terraform/lab/40-networking/terraform.tfstate").read_text()
    )["outputs"]["workers"]["value"]
    worker = workers[name]
    assert worker["system_uuid"] == ownership["config"]["volatile.uuid"]
    roles = json.loads((ROOT / "maas/network-model/logical-roles.json").read_text())["roles"]
    protocols = guest(name, "birdc", "show", "protocols", "all")
    bfd = guest(name, "birdc", "show", "bfd", "sessions")
    blocks = re.split(r"(?m)^(?=\w+\s+(?:BGP|Pipe|Kernel|Direct|Device|Static)\s)", protocols)
    blocks = [block for block in blocks if re.match(r"\w+\s+BGP\s", block)]
    expected = {
        (
            f"{worker['interfaces'][f'pf{slot}']['name']}.{role['vlan']}",
            65101 + 2 * (worker["rack"] - 1) + slot,
        )
        for slot in (0, 1)
        for role in roles
    }
    receipt = {"worker": name, "protocols": protocols, "bfd": bfd, "roles": {}}
    try:
        assert len(blocks) == len(expected) == 10
        observed = set()
        bfd_rows = [line.split() for line in bfd.splitlines() if line.strip().startswith("fe80:")]
        assert len(bfd_rows) == 10
        for block in blocks:
            peers = re.findall(r"Neighbor address:\s+(\S+)", block)
            assert len(peers) == 1
            address, interface = peers[0].rsplit("%", 1)
            assert address == "fe80::1"
            remote_asn = int(re.findall(r"Neighbor AS:\s+(\d+)", block)[0])
            assert re.findall(r"Local AS:\s+(\d+)", block) == [
                str(4200000020 + WORKERS.index(name))
            ]
            observed.add((interface, remote_asn))
            matching = [row for row in bfd_rows if row[1] == interface]
            assert len(matching) == 1 and matching[0][:3] == ["fe80::1", interface, "Up"]
            assert accepted(re.sub(r"^\w+", "uplink_0", block), " ".join(matching[0]), 1)
        assert observed == expected
        remote = next(other for other in WORKERS if workers[other]["rack"] != worker["rack"])
        for role in roles:
            network = ip_network(role["address_block"])
            source = str(network.network_address + 20 + WORKERS.index(name))
            destination = str(network.network_address + 20 + WORKERS.index(remote))
            routes = json.loads(
                guest(
                    name,
                    "ip",
                    "-j",
                    "route",
                    "show",
                    "table",
                    str(role["table"]),
                    f"{destination}/32",
                )
            )
            assert len(routes) == 1 and routes[0]["protocol"] == "bird", routes
            hops = routes[0]["nexthops"]
            assert len(hops) == 2
            assert {hop["dev"] for hop in hops} == {
                f"{worker['interfaces'][f'pf{slot}']['name']}.{role['vlan']}" for slot in (0, 1)
            }
            assert all(hop["via"] == {"family": "inet6", "host": "fe80::1"} for hop in hops)
            packets = guest(
                name,
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
                source,
                destination,
            )
            receipt["roles"][role["id"]] = {
                "source": source,
                "destination": destination,
                "routes": routes,
                "packets": packets,
            }
            assert "3 packets transmitted, 3 received" in packets
        receipt["passed"] = True
    finally:
        directory = ROOT / "runtime/terraform/acceptance/host-routing"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_atomic(
            directory / f"{name}-{time.time_ns()}.json",
            json.dumps(receipt, indent=2) + "\n",
            0o600,
        )


@pytest.mark.parametrize("name", WORKERS)
@pytest.mark.parametrize("pf", (0, 1))
def test_native_pf_failure_preserves_role_and_overlay_forwarding(name, pf, ownership):
    _role_and_overlay_failure(name, pf, ownership, general_only=False)


@pytest.mark.parametrize("name", WORKERS)
@pytest.mark.parametrize("pf", (0, 1))
def test_native_general_vf_failure_preserves_storage_and_overlay(name, pf, ownership):
    _role_and_overlay_failure(name, pf, ownership, general_only=True)


def _role_and_overlay_failure(name, pf, ownership, *, general_only):
    workers = json.loads(
        (ROOT / "runtime/terraform/lab/40-networking/terraform.tfstate").read_text()
    )["outputs"]["workers"]["value"]
    worker = workers[name]
    assert worker["system_uuid"] == ownership["config"]["volatile.uuid"]
    roles = json.loads((ROOT / "maas/network-model/logical-roles.json").read_text())["roles"]
    remote = next(other for other in WORKERS if workers[other]["rack"] != worker["rack"])
    parent = worker["interfaces"][f"pf{pf}"]["name"]
    survivor = worker["interfaces"][f"pf{1 - pf}"]["name"]
    fault = f"{parent}.3404" if general_only else parent
    kind = "general-vf" if general_only else "pf"
    receipt = {
        "worker": name,
        "failed_parent": parent,
        "failed_interface": fault,
        "kind": kind,
        "observations": {},
    }

    def paths(count):
        observations = {}
        for role in roles:
            network = ip_network(role["address_block"])
            destination = str(network.network_address + 20 + WORKERS.index(remote))
            routes = json.loads(
                guest(
                    name,
                    "ip",
                    "-j",
                    "route",
                    "show",
                    "table",
                    str(role["table"]),
                    f"{destination}/32",
                )
            )
            assert len(routes) == 1 and routes[0]["protocol"] == "bird", routes
            route = routes[0]
            hops = route.get("nexthops", [route])
            expected_count = count if not general_only or role["id"] == "general" else 2
            assert len(hops) == expected_count, route
            expected = (
                {f"{survivor}.{role['vlan']}"}
                if expected_count == 1
                else {
                    f"{worker['interfaces'][f'pf{slot}']['name']}.{role['vlan']}"
                    for slot in (0, 1)
                }
            )
            assert {hop["dev"] for hop in hops} == expected, route
            assert all(hop["via"] == {"family": "inet6", "host": "fe80::1"} for hop in hops)
            observations[role["id"]] = routes
        return observations

    def await_paths(count):
        started = time.monotonic()
        while True:
            try:
                result = paths(count)
                return {"elapsed": time.monotonic() - started, "routes": result}
            except AssertionError:
                if time.monotonic() - started >= 30:
                    raise
                time.sleep(0.2)

    def packets():
        observed = {}
        for role in roles:
            network = ip_network(role["address_block"])
            source = str(network.network_address + 20 + WORKERS.index(name))
            destination = str(network.network_address + 20 + WORKERS.index(remote))
            result = guest(
                name,
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
                source,
                destination,
            )
            assert "3 packets transmitted, 3 received" in result
            observed[role["id"]] = result
        overlay = guest(
            name,
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
            "1372",
            "-I",
            "tenant0",
            f"172.30.10.{20 + WORKERS.index(remote)}",
        )
        assert "3 packets transmitted, 3 received" in overlay
        observed["overlay"] = overlay
        return observed

    receipt["observations"]["before"] = await_paths(2)
    receipt["observations"]["packets_before"] = packets()
    try:
        receipt["fault_started"] = time.monotonic()
        guest(name, "ip", "link", "set", fault, "down")
        receipt["observations"]["withdrawal"] = await_paths(1)
        links = json.loads(guest(name, "ip", "-j", "-d", "link", "show", "type", "vlan"))
        failed = (
            [link for link in links if link["ifname"] == fault]
            if general_only
            else [link for link in links if link.get("link") == parent]
        )
        assert len(failed) == (1 if general_only else 5)
        if general_only:
            unaffected = [link for link in links if link["ifname"] != fault]
            assert len(unaffected) == 9
            assert all(
                "UP" in link["flags"] and "LOWER_UP" in link["flags"] for link in unaffected
            )
            parent_state = json.loads(guest(name, "ip", "-j", "link", "show", parent))[0]
            assert {"UP", "LOWER_UP"} <= set(parent_state["flags"])
        assert all("LOWER_UP" not in link["flags"] for link in failed)
        receipt["observations"]["packets_during"] = packets()
        protocols = guest(name, "birdc", "show", "protocols", "all")
        bfd = guest(name, "birdc", "show", "bfd", "sessions")
        receipt["observations"]["protocols_during"] = protocols
        receipt["observations"]["bfd_during"] = bfd
        assert protocols.count("BGP state:          Established") == (9 if general_only else 5), (
            protocols
        )
        assert len(re.findall(r"(?m)^fe80:.*\sUp\s", bfd)) == (9 if general_only else 5), bfd
        receipt["observations"]["management"] = guest(
            name, "ping", "-n", "-c", "3", "-W", "1", "-I", "bond-mgmt", "10.203.241.10"
        )
    finally:
        try:
            guest(name, "ip", "link", "set", fault, "up")
            receipt["observations"]["recovery"] = await_paths(2)
            receipt["observations"]["packets_restored"] = packets()
        finally:
            directory = ROOT / "runtime/terraform/acceptance/host-role-failover"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_atomic(
                directory / f"{name}-{kind}{pf}-{time.time_ns()}.json",
                json.dumps(receipt, indent=2) + "\n",
                0o600,
            )


@pytest.mark.parametrize("name", WORKERS)
def test_native_geneve_blackhole_is_detected_with_bgp_up(name, ownership):
    values = json.loads((ROOT / "runtime/terraform/lab/90-ovn/terraform.tfstate").read_text())[
        "outputs"
    ]
    assert values["compute_enabled"]["value"] is True
    chassis = values["chassis"]["value"]
    assert chassis[name]["uuid"] == ownership["config"]["volatile.uuid"]
    remote = WORKERS[2] if name in WORKERS[:2] else WORKERS[0]
    source, destination = chassis[name]["vtep"], chassis[remote]["vtep"]
    tenant = f"172.30.10.{20 + WORKERS.index(remote)}"
    original = guest(name, "iptables", "-w", "5", "-S", "OUTPUT")
    marker = "nlab-geneve-" + uuid.uuid4().hex[:12]
    rule = [
        "-s",
        source,
        "-d",
        destination,
        "-p",
        "udp",
        "--dport",
        "6081",
        "-m",
        "comment",
        "--comment",
        marker,
        "-j",
        "DROP",
    ]
    receipt = {"worker": name, "remote": remote, "marker": marker, "rule": rule}

    def routing():
        protocols = guest(name, "birdc", "show", "protocols", "all")
        bfd = guest(name, "birdc", "show", "bfd", "sessions")
        assert protocols.count("BGP state:          Established") == 10, protocols
        assert len(re.findall(r"(?m)^fe80:.*\sUp\s", bfd)) == 10, bfd
        output = guest(
            name, "ping", "-n", "-c", "3", "-i", "0.2", "-W", "1", "-I", source, destination
        )
        assert "3 packets transmitted, 3 received" in output
        return {"bgp": protocols, "bfd": bfd, "underlay_packets": output}

    def overlay():
        return subprocess.run(
            [
                "lxc",
                "exec",
                "--project",
                PROJECT,
                name,
                "--",
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
                "1372",
                "-I",
                "tenant0",
                tenant,
            ],
            text=True,
            capture_output=True,
            timeout=10,
        )

    receipt["before"] = routing()
    baseline = overlay()
    assert baseline.returncode == 0 and "3 packets transmitted, 3 received" in baseline.stdout
    try:
        guest(name, "iptables", "-w", "5", "-I", "OUTPUT", "1", *rule)
        started = time.monotonic()
        broken = overlay()
        receipt["failure_detection_seconds"] = time.monotonic() - started
        receipt["failed_packets"] = {
            "returncode": broken.returncode,
            "stdout": broken.stdout,
            "stderr": broken.stderr,
        }
        assert broken.returncode != 0
        assert "3 packets transmitted, 0 received" in broken.stdout, broken.stdout
        counters = guest(name, "iptables", "-w", "5", "-L", "OUTPUT", "-n", "-v", "-x")
        matches = [line for line in counters.splitlines() if marker in line]
        assert len(matches) == 1 and int(matches[0].split()[0]) >= 3, counters
        receipt["drop_counter"] = matches[0]
        receipt["held_control_plane"] = routing()
    finally:
        try:
            present = subprocess.run(
                [
                    "lxc",
                    "exec",
                    "--project",
                    PROJECT,
                    name,
                    "--",
                    "iptables",
                    "-w",
                    "5",
                    "-C",
                    "OUTPUT",
                    *rule,
                ],
                text=True,
                capture_output=True,
                timeout=30,
            )
            assert present.returncode in (0, 1), present.stderr
            if present.returncode == 0:
                guest(name, "iptables", "-w", "5", "-D", "OUTPUT", *rule)
            assert guest(name, "iptables", "-w", "5", "-S", "OUTPUT") == original
            started = time.monotonic()
            while True:
                restored = overlay()
                if (
                    restored.returncode == 0
                    and "3 packets transmitted, 3 received" in restored.stdout
                ):
                    break
                assert time.monotonic() - started < 15, restored.stdout
                time.sleep(0.2)
            receipt["restored_packets"] = restored.stdout
            receipt["recovery_observation_seconds"] = time.monotonic() - started
            receipt["restored_control_plane"] = routing()
        finally:
            directory = ROOT / "runtime/terraform/acceptance/geneve-blackhole"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_atomic(
                directory / f"{name}-{time.time_ns()}.json",
                json.dumps(receipt, indent=2) + "\n",
                0o600,
            )
