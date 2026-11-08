"""Native broadcasts across repeated VIDs stay in their rack/PF fabric.

Each burst temporarily binds one otherwise absent 198.18/15 IPv4 endpoint
on a role child. Its address and connected route are removed before the next
burst. Captures cover all forty role children; management and OVN interfaces
are never reconfigured. This exercises L2 isolation, not workload capacity.
"""

import importlib
import json
import re
import stat
import subprocess
import time
import uuid
from collections import Counter
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any

import pytest
from maas_bird.runtime import write_atomic

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "maas-bird-network-lab-tf"
WORKERS = ("probe", "probe-alt", "probe-r2a", "probe-r2b")
pytestmark = pytest.mark.integration


def guest(node, *arguments, check=True, timeout=30):
    return subprocess.run(
        ["lxc", "exec", "--project", PROJECT, node, "--", *arguments],
        text=True,
        capture_output=True,
        check=check,
        timeout=timeout,
    )


def ipv4_addresses(node, interface):
    """ip's IPv4 filter omits an existing interface with no IPv4 addresses."""
    rows = json.loads(guest(node, "ip", "-j", "-4", "address", "show", "dev", interface).stdout)
    assert len(rows) <= 1
    assert all(row["ifname"] == interface for row in rows)
    return [address for row in rows for address in row.get("addr_info", [])]


def state(stage):
    path = ROOT / f"runtime/terraform/lab/{stage}/terraform.tfstate"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    return {
        key: output["value"] for key, output in json.loads(path.read_text())["outputs"].items()
    }


def inventory():
    workers = state("40-networking")["workers"]
    authority = state("25-maas-config")
    fabrics = authority["fast_fabrics"]
    assert set(workers) == set(WORKERS)
    assert set(fabrics) == {f"r{rack}-pf{pf}" for rack in (1, 2) for pf in (0, 1)}
    assert len(set(fabrics.values())) == 4
    machines = {
        node["system_id"]: node
        for node in json.loads(guest("maas", "maas", "lab", "machines", "read").stdout)
    }
    endpoints = {}
    for name in WORKERS:
        worker = workers[name]
        instance = json.loads(
            subprocess.check_output(
                ["lxc", "query", f"/1.0/instances/{name}?project={PROJECT}"],
                text=True,
                timeout=30,
            )
        )
        assert instance["type"] == "virtual-machine" and instance["status"] == "Running"
        assert instance["config"]["user.maas-bird-owner"] == "network-lab"
        assert instance["config"]["limits.cpu"] == "1"
        assert instance["config"]["limits.memory"] == "4GiB"
        assert instance["config"]["volatile.uuid"] == worker["system_uuid"]
        machine = machines[worker["system_id"]]
        assert machine["hostname"] == name and machine["status_name"] == "Deployed"
        assert machine["hardware_uuid"] == worker["system_uuid"]
        maas_links = {link["name"]: link for link in machine["interface_set"]}
        native_links = {
            link["ifname"]: link
            for link in json.loads(guest(name, "ip", "-j", "-d", "link", "show").stdout)
        }
        assert guest(name, "sh", "-c", "command -v tcpdump").stdout.strip()
        all_addresses = json.loads(guest(name, "ip", "-j", "-4", "address", "show").stdout)
        assert not any(
            IPv4Address(address["local"]) in IPv4Network("198.18.0.0/15")
            for link in all_addresses
            for address in link["addr_info"]
        ), (name, "Benchmark address space is already in use")
        assert len(worker["vlans"]) == 10
        for vlan in worker["vlans"].values():
            interface, pf, role, vid = vlan["name"], vlan["nic"], vlan["role"], vlan["vid"]
            assert pf in {"pf0", "pf1"}
            assert vid in range(3400, 3405)
            parent = worker["interfaces"][pf]
            assert instance["devices"][pf]["hwaddr"] == parent["mac"]
            assert maas_links[parent["name"]]["mac_address"] == parent["mac"]
            carrier = f"r{worker['rack']}-{pf}"
            expected_vlan = authority["role_vlans"][f"{carrier}-{role}"]
            maas_link = maas_links[interface]
            assert maas_link["parents"] == [parent["name"]]
            assert str(maas_link["vlan"]["fabric_id"]) == fabrics[carrier]
            assert maas_link["vlan"]["fabric"] == carrier
            assert str(maas_link["vlan"]["id"]) == expected_vlan
            assert maas_link["vlan"]["vid"] == vid
            link = native_links[interface]
            assert link["linkinfo"]["info_kind"] == "vlan"
            assert link["linkinfo"]["info_data"]["id"] == vid
            assert (
                link.get("link") == parent["name"]
                or link.get("link_index") == native_links[parent["name"]]["ifindex"]
            )
            assert {"UP", "LOWER_UP"} <= set(link["flags"])
            addresses = ipv4_addresses(name, interface)
            assert addresses == [], (name, interface, addresses)
            endpoints[f"{name}/{interface}"] = {
                "worker": name,
                "interface": interface,
                "carrier": carrier,
                "fabric_id": fabrics[carrier],
                "vid": vid,
                "role": role,
            }
    assert len(endpoints) == 40
    assert Counter((row["carrier"], row["vid"]) for row in endpoints.values()) == Counter(
        {(carrier, vid): 2 for carrier in fabrics for vid in range(3400, 3405)}
    )
    return workers, endpoints


def test_native_reused_vid_broadcasts_stay_in_exact_fabric_and_role(monkeypatch):
    workers, endpoints = inventory()
    monkeypatch.syspath_prepend(str(ROOT / "tests/lxd"))
    capture = importlib.import_module("test_native_gateways")
    identifier = 1 + int(uuid.uuid4().hex[:4], 16) % 65535
    cases = []
    for rack in (1, 2):
        sender = next(name for name in WORKERS if workers[name]["rack"] == rack)
        for pf in ("pf0", "pf1"):
            for vid in range(3400, 3405):
                endpoint = next(
                    row
                    for row in endpoints.values()
                    if row["worker"] == sender
                    and row["carrier"] == f"r{rack}-{pf}"
                    and row["vid"] == vid
                )
                index = len(cases)
                cases.append(
                    {
                        **endpoint,
                        "source": f"198.18.{index}.10",
                        "broadcast": f"198.18.{index}.255",
                        "prefix": f"198.18.{index}.0/24",
                    }
                )
    assert len(cases) == 20
    receipt: dict[str, Any] = {
        "project": PROJECT,
        "identifier": identifier,
        "worker_uuids": {name: row["system_uuid"] for name, row in workers.items()},
        "endpoints": endpoints,
        "cases": cases,
        "captures": {},
    }
    processes = {}
    expression = "ether broadcast and icmp and src net 198.18.0.0/15"
    try:
        for key, endpoint in endpoints.items():
            process = capture.provider_capture(
                endpoint["worker"], endpoint["interface"], expression, 120
            )
            processes[key] = process
            startup = capture.wait_capture_ready(process)
            receipt["captures"][key] = {"startup": startup}
        receipt["all_captures_ready_at"] = time.time()
        for case in cases:
            assert all(process.poll() is None for process in processes.values()), (
                "A native capture ended before the broadcast sweep completed"
            )
            node, interface = case["worker"], case["interface"]
            before = ipv4_addresses(node, interface)
            assert before == []
            assert (
                json.loads(
                    guest(
                        node, "ip", "-j", "-4", "route", "show", "table", "all", case["prefix"]
                    ).stdout
                )
                == []
            )
            try:
                guest(
                    node,
                    "ip",
                    "address",
                    "add",
                    f"{case['source']}/24",
                    "broadcast",
                    case["broadcast"],
                    "dev",
                    interface,
                )
                result = guest(
                    node,
                    "ping",
                    "-b",
                    "-n",
                    "-c",
                    "3",
                    "-i",
                    "0.2",
                    "-W",
                    "1",
                    "-s",
                    "24",
                    "-e",
                    str(identifier),
                    "-I",
                    interface,
                    case["broadcast"],
                    check=False,
                    timeout=10,
                )
                case["ping"] = {
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
                assert result.returncode in (0, 1), result.stderr
                assert "3 packets transmitted" in result.stdout, result.stdout
            finally:
                current = ipv4_addresses(node, interface)
                owned = [address for address in current if address["local"] == case["source"]]
                if owned:
                    assert len(owned) == 1 and owned[0]["prefixlen"] == 24
                    assert owned[0]["broadcast"] == case["broadcast"]
                    guest(node, "ip", "address", "del", f"{case['source']}/24", "dev", interface)
                after = ipv4_addresses(node, interface)
                assert after == before
                assert (
                    json.loads(
                        guest(
                            node, "ip", "-j", "-4", "route", "show", "table", "all", case["prefix"]
                        ).stdout
                    )
                    == []
                )
                case["address_and_route_restored"] = True
        for key, process in processes.items():
            output, errors = process.communicate(timeout=150)
            row = receipt["captures"][key]
            row.update(stdout=output, stderr=errors, returncode=process.returncode)
            assert process.returncode == 124, (key, errors)
            assert "0 packets dropped by kernel" in errors, (key, errors)
            observed = re.findall(
                r"(\d+\.\d+\.\d+\.\d+) > (\d+\.\d+\.\d+\.\d+): "
                r"ICMP echo request, id (\d+), seq (\d+)",
                output,
            )
            actual = Counter(
                (source, destination, int(ping_id), int(sequence))
                for source, destination, ping_id, sequence in observed
            )
            endpoint = endpoints[key]
            expected = Counter(
                (case["source"], case["broadcast"], identifier, sequence)
                for case in cases
                if case["carrier"] == endpoint["carrier"] and case["vid"] == endpoint["vid"]
                for sequence in (1, 2, 3)
            )
            row["observed_requests"] = observed
            assert actual == expected, {
                "endpoint": endpoint,
                "actual": actual,
                "expected": expected,
            }
            captured = re.search(r"(\d+) packets captured", errors)
            assert captured is not None and int(captured[1]) == sum(expected.values()) == 3, (
                key,
                output,
                errors,
            )
        receipt["all_twenty_cases_passed"] = True
    except BaseException as error:
        receipt["failure"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        for key, process in processes.items():
            if process.poll() is None:
                process.terminate()
            try:
                output, errors = process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                output, errors = process.communicate(timeout=10)
            receipt["captures"].setdefault(key, {}).update(
                stdout=output, stderr=errors, returncode=process.returncode
            )
        directory = ROOT / "runtime/terraform/acceptance/broadcast-isolation"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_atomic(
            directory / f"native-{time.time_ns()}.json",
            json.dumps(receipt, indent=2) + "\n",
            0o600,
        )
