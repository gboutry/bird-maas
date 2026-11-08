"""Representative continuous GP and management failover observations."""

import json
import os
import re
import signal
import subprocess
import tempfile
import time
import uuid
from contextlib import ExitStack
from typing import Any

import pytest
from maas_bird.runtime import write_atomic
from test_native_tor_failover import instance
from test_native_workers import PROJECT, ROOT, guest
from test_native_workers import ownership as ownership

READ_LIMIT = 10_000_000


def _text(output):
    raw = os.pread(output.fileno(), READ_LIMIT, 0)
    assert len(raw) < READ_LIMIT, "Observation output reached its explicit read limit"
    return raw.decode()


def _replies(output):
    return [
        (float(stamp), int(sequence))
        for stamp, sequence in re.findall(
            r"(?m)^\[(\d+\.\d+)\].* bytes from .*: icmp_seq=(\d+)", _text(output)
        )
    ]


def _stop(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    try:
        process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.communicate(timeout=25)


def _flow(process, output, errors):
    rows, text = _replies(output), _text(output)
    counts = re.search(r"(\d+) packets transmitted, (\d+) received", text)
    return {
        "stdout": text,
        "stderr": _text(errors),
        "returncode": process.returncode,
        "transmitted": int(counts[1]) if counts else None,
        "received": int(counts[2]) if counts else None,
        "max_inter_reply_gap_seconds": max(
            (b[0] - a[0] for a, b in zip(rows, rows[1:], strict=False)), default=None
        ),
        "duplicate_replies": len(rows) - len({sequence for _, sequence in rows}),
        "reordered_replies": sum(b[1] < a[1] for a, b in zip(rows, rows[1:], strict=False)),
    }


def _packets(trace, interface, source, destination, tenant_source, tenant_destination, identifier):
    result = []
    for block in re.split(r"(?m)^(?=\d+\.\d+\s)", trace):
        outer = re.search(
            r"(?m)^\s*(\d+\.\d+\.\d+\.\d+)\.\d+ > (\d+\.\d+\.\d+\.\d+)\.6081:", block
        )
        inner = re.search(
            rf"{re.escape(tenant_source)} > {re.escape(tenant_destination)}: "
            r"ICMP echo request, id (\d+), seq (\d+)",
            block,
        )
        if outer and inner and "Geneve" in block and int(inner[1]) == identifier:
            assert (outer[1], outer[2]) == (source, destination), block
            result.append(
                {
                    "interface": interface,
                    "identifier": int(inner[1]),
                    "sequence": int(inner[2]),
                    "outer_source": outer[1],
                    "outer_destination": outer[2],
                }
            )
    return result


@pytest.mark.parametrize("name", ("probe",))
def test_native_continuous_gp_and_management_path_measurements(name, ownership):
    workers = json.loads(
        (ROOT / "runtime/terraform/lab/40-networking/terraform.tfstate").read_text()
    )["outputs"]["workers"]["value"]
    ovn = {
        key: value["value"]
        for key, value in json.loads(
            (ROOT / "runtime/terraform/lab/90-ovn/terraform.tfstate").read_text()
        )["outputs"].items()
    }
    remote = "probe-r2a"
    assert ovn["compute_enabled"] is True and workers[name]["rack"] != workers[remote]["rack"]
    for node in (name, remote):
        row = ownership if node == name else instance(node)
        assert row["type"] == "virtual-machine" and row["status"] == "Running"
        assert row["config"]["user.maas-bird-owner"] == "network-lab"
        assert (
            row["config"]["volatile.uuid"]
            == workers[node]["system_uuid"]
            == ovn["chassis"][node]["uuid"]
        )
    before_links = json.loads(guest(name, "ip", "-j", "-d", "link", "show"))
    admin_before = {link["ifname"]: "UP" in link["flags"] for link in before_links}
    parents = [workers[name]["interfaces"][f"pf{pf}"]["name"] for pf in (0, 1)]
    children = [f"{parent}.3404" for parent in parents]
    for pf, parent in enumerate(parents):
        native = next(link for link in before_links if link["ifname"] == parent)
        assert (
            native.get("permaddr", native.get("address"))
            == workers[name]["interfaces"][f"pf{pf}"]["mac"]
            == ownership["devices"][f"pf{pf}"]["hwaddr"]
        )
        child = next(link for link in before_links if link["ifname"] == children[pf])
        assert child["linkinfo"]["info_data"]["id"] == 3404
        assert child.get("link") == parent or child.get("link_index") == native["ifindex"]
    slaves = guest(name, "cat", "/sys/class/net/bond-mgmt/bonding/slaves").split()
    assert len(slaves) == 2 and not set(slaves) & set(parents)
    original_active = guest(name, "cat", "/sys/class/net/bond-mgmt/bonding/active_slave").strip()
    assert original_active in slaves and all(
        admin_before[interface] for interface in (*parents, *children, *slaves)
    )
    before_addresses = json.loads(guest(name, "ip", "-j", "-4", "address", "show", "bond-mgmt"))
    source, destination = ovn["chassis"][name]["vtep"], ovn["chassis"][remote]["vtep"]
    tenant_source, tenant_destination = "172.30.10.20", "172.30.10.22"
    identifier = 1 + uuid.uuid4().int % 65534
    receipt: dict[str, Any] = {
        "worker": name,
        "remote": remote,
        "identifier": identifier,
        "original_active_slave": original_active,
        "management_addresses_before": before_addresses,
        "restore_observation_limit_seconds": 90,
        "management_failure_domain": "two bond NICs, one carrier; no access-switch failure claim",
        "phases": [],
        "captures": {},
        "flows": {},
    }
    streams, captures = {}, {}
    command = ["lxc", "exec", "--project", PROJECT, name, "--"]

    def routes(expected, limit):
        started = time.monotonic()
        while True:
            observed = json.loads(
                guest(name, "ip", "-j", "route", "show", "table", "4400", f"{destination}/32")
            )
            if len(observed) == 1 and observed[0].get("protocol") == "bird":
                hops = observed[0].get("nexthops", [observed[0]])
                if (
                    len(hops) == len(expected)
                    and {hop["dev"] for hop in hops} == set(expected)
                    and all(hop["via"] == {"family": "inet6", "host": "fe80::1"} for hop in hops)
                ):
                    return {"elapsed": time.monotonic() - started, "routes": observed}
            assert time.monotonic() - started < limit, observed
            time.sleep(0.2)

    def sequences():
        return {
            kind: _replies(output)[-1][1] if _replies(output) else 0
            for kind, (_, output, _) in streams.items()
        }

    def later(after):
        deadline = time.monotonic() + 10
        while True:
            if all(
                sum(sequence > after[kind] for _, sequence in _replies(output)) >= 3
                for kind, (_, output, _) in streams.items()
            ):
                return sequences()
            assert all(process.poll() is None for process, _, _ in streams.values())
            assert time.monotonic() < deadline
            time.sleep(0.1)

    def active(expected):
        started = time.monotonic()
        while True:
            observed = guest(name, "cat", "/sys/class/net/bond-mgmt/bonding/active_slave").strip()
            if observed == expected:
                return {"elapsed": time.monotonic() - started, "active": observed}
            assert time.monotonic() - started < 10, observed
            time.sleep(0.1)

    def select(expected):
        deadline = time.monotonic() + 5
        while True:
            try:
                guest(
                    name,
                    "ip",
                    "link",
                    "set",
                    "bond-mgmt",
                    "type",
                    "bond",
                    "active_slave",
                    expected,
                )
                return active(expected)
            except subprocess.CalledProcessError:
                assert time.monotonic() < deadline
                time.sleep(0.1)

    receipt["initial_routes"] = routes(children, 30)
    with ExitStack() as stack:
        try:
            for interface in children:
                output, errors = (stack.enter_context(tempfile.TemporaryFile()) for _ in range(2))
                expression = "udp port 6081"
                arguments = (
                    "timeout --signal=INT --kill-after=2 330 "
                    "tcpdump -p --immediate-mode -tt -n -l -vv"
                ).split()
                process = subprocess.Popen(
                    [*command, *arguments, "-i", interface, expression],
                    stdout=output,
                    stderr=errors,
                    text=True,
                )
                captures[interface] = (process, output, errors)
                deadline = time.monotonic() + 10
                while "listening on" not in _text(errors):
                    assert process.poll() is None and time.monotonic() < deadline, _text(errors)
                    time.sleep(0.05)
            for kind, interface, target, size in (
                ("data", "tenant0", tenant_destination, 1372),
                ("management", "bond-mgmt", ovn["central_address"], 1472),
            ):
                output, errors = (stack.enter_context(tempfile.TemporaryFile()) for _ in range(2))
                arguments = [
                    *command,
                    *"ping -n -D -O -i 0.2 -w 300 -W 1 -M do".split(),
                    "-s",
                    str(size),
                    "-I",
                    interface,
                ]
                if kind == "data":
                    arguments.extend(("-e", str(identifier)))
                process = subprocess.Popen(
                    [*arguments, target], stdout=output, stderr=errors, text=True
                )
                streams[kind] = (process, output, errors)
            receipt["baseline_window"] = {
                "after": dict.fromkeys(streams, 0),
                "through": later(dict.fromkeys(streams, 0)),
            }
            for pf, child in enumerate(children):
                phase = {
                    "kind": "general-vf",
                    "interface": child,
                    "before": sequences(),
                    "fault_started": time.monotonic(),
                }
                receipt["phases"].append(phase)
                guest(name, "ip", "link", "set", child, "down")
                phase["withdrawal"] = routes([children[1 - pf]], 30)
                phase["held_window"] = {"after": sequences()}
                phase["held_window"]["through"] = later(phase["held_window"]["after"])
                phase["restore_started"] = time.monotonic()
                guest(name, "ip", "link", "set", child, "up")
                phase["restored_routes"] = routes(children, 90)
                phase["restored_window"] = {"after": sequences()}
                phase["restored_window"]["through"] = later(phase["restored_window"]["after"])
            for selected in slaves:
                select(selected)
                phase = {
                    "kind": "management-nic",
                    "interface": selected,
                    "before": sequences(),
                    "fault_started": time.monotonic(),
                }
                receipt["phases"].append(phase)
                guest(name, "ip", "link", "set", selected, "down")
                phase["survivor"] = active(next(slave for slave in slaves if slave != selected))
                phase["held_window"] = {"after": sequences()}
                phase["held_window"]["through"] = later(phase["held_window"]["after"])
                guest(name, "ip", "link", "set", selected, "up")
                assert (
                    "UP"
                    in json.loads(guest(name, "ip", "-j", "link", "show", selected))[0]["flags"]
                )
                phase["restored_window"] = {"after": sequences()}
                phase["restored_window"]["through"] = later(phase["restored_window"]["after"])
        except BaseException as error:
            receipt["failure"] = {"type": type(error).__name__, "message": str(error)}
            raise
        finally:
            try:
                errors = []
                for interface in (*children, *slaves):
                    try:
                        guest(
                            name,
                            "ip",
                            "link",
                            "set",
                            interface,
                            "up" if admin_before[interface] else "down",
                        )
                    except subprocess.CalledProcessError as error:
                        errors.append(str(error))
                assert not errors, errors
                select(original_active)
                after_links = json.loads(guest(name, "ip", "-j", "link", "show"))
                assert {
                    link["ifname"]: "UP" in link["flags"] for link in after_links
                } == admin_before
                receipt["management_addresses_after"] = json.loads(
                    guest(name, "ip", "-j", "-4", "address", "show", "bond-mgmt")
                )
                assert receipt["management_addresses_after"] == before_addresses
                receipt["final_routes"] = routes(children, 90)
                for node in (name, remote):
                    assert (
                        guest(node, "birdc", "show", "protocols", "all").count(
                            "BGP state:          Established"
                        )
                        == 10
                    )
                    bfd = guest(node, "birdc", "show", "bfd", "sessions")
                    assert len(re.findall(r"(?m)^fe80:.*\sUp\s", bfd)) == 10
                if streams:
                    receipt["final_window"] = {"after": sequences()}
                    receipt["final_window"]["through"] = later(receipt["final_window"]["after"])
            finally:
                cleanup_errors = []
                for kind, (process, output, errors) in streams.items():
                    try:
                        _stop(process)
                        receipt["flows"][kind] = _flow(process, output, errors)
                    except (OSError, AssertionError, subprocess.TimeoutExpired) as error:
                        cleanup_errors.append(str(error))
                for interface, (process, output, errors) in captures.items():
                    try:
                        _stop(process)
                        receipt["captures"][interface] = {
                            "stdout": _text(output),
                            "stderr": _text(errors),
                            "returncode": process.returncode,
                        }
                    except (OSError, AssertionError, subprocess.TimeoutExpired) as error:
                        cleanup_errors.append(str(error))
                receipt["cleanup_errors"] = cleanup_errors
                for phase in receipt["phases"]:
                    phase["flows"] = {}
                    for kind, (_, output, _) in streams.items():
                        through = (
                            phase.get("restored_window", phase.get("held_window", {}))
                            .get("through", sequences())
                            .get(kind, 0)
                        )
                        rows = [
                            (stamp, seq)
                            for stamp, seq in _replies(output)
                            if phase["before"][kind] <= seq <= through
                        ]
                        received = {seq for _, seq in rows if seq > phase["before"][kind]}
                        phase["flows"][kind] = {
                            "reply_sequence_after": phase["before"][kind],
                            "reply_sequence_through": through,
                            "missing_sequences_in_window": through
                            - phase["before"][kind]
                            - len(received),
                            "max_inter_reply_gap_seconds": max(
                                (b[0] - a[0] for a, b in zip(rows, rows[1:], strict=False)),
                                default=None,
                            ),
                        }
                directory = ROOT / "runtime/terraform/acceptance/path-measurements"
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                write_atomic(
                    directory / f"{name}-{time.time_ns()}.json",
                    json.dumps(receipt, indent=2) + "\n",
                    0o600,
                )
    assert not receipt["cleanup_errors"], receipt["cleanup_errors"]
    packets = []
    for interface, capture in receipt["captures"].items():
        assert "0 packets dropped by kernel" in capture["stderr"]
        packets.extend(
            _packets(
                capture["stdout"],
                interface,
                source,
                destination,
                tenant_source,
                tenant_destination,
                identifier,
            )
        )

    def in_window(window):
        return [
            packet
            for packet in packets
            if window["after"]["data"] < packet["sequence"] <= window["through"]["data"]
        ]

    windows = [receipt["baseline_window"], receipt["final_window"]]
    for phase in receipt["phases"]:
        windows.extend((phase["held_window"], phase["restored_window"]))
        if phase["kind"] == "general-vf":
            window = phase["held_window"]
            observed = in_window(window)
            assert len({packet["sequence"] for packet in observed}) >= 3
            assert {packet["interface"] for packet in observed} == set(children) - {
                phase["interface"]
            }
    for window in windows:
        assert len({packet["sequence"] for packet in in_window(window)}) >= 3
    for flow in receipt["flows"].values():
        assert flow["returncode"] == 0
        assert flow["transmitted"] >= flow["received"] >= 15
