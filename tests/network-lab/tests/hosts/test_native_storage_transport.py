"""Source-bound native storage TCP/UDP records; no Ceph performance claim."""

import hashlib
import json
import os
import secrets
import signal
import subprocess
import tempfile
import time
import uuid
from contextlib import ExitStack
from ipaddress import ip_network
from typing import Any

import pytest
from maas_bird.runtime import write_atomic
from test_native_workers import PROJECT, ROOT, WORKERS, guest
from test_native_workers import ownership as ownership

STORAGE_ROLES = ("access0", "access1", "replication0", "replication1")
UDP_RECORD_BYTES = 1200
UDP_RECORDS = 20


def _socket_rows(node, transport, port):
    return guest(
        node,
        "ss",
        "-H",
        "-n",
        "-4",
        "-a",
        "-t" if transport == "tcp" else "-u",
        f"sport = :{port}",
    )


def _start(node, arguments, output, errors, *, sender=False):
    return subprocess.Popen(
        [
            "lxc",
            "exec",
            "--project",
            PROJECT,
            node,
            "--",
            "timeout",
            "--signal=INT",
            "--kill-after=2",
            "25",
            "nc",
            *arguments,
        ],
        stdin=subprocess.PIPE if sender else subprocess.DEVNULL,
        stdout=output,
        stderr=errors,
    )


def _stop(process):
    if process is None:
        return
    if process.stdin is not None:
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        finally:
            process.stdin = None
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.communicate(timeout=25)


def _wait_socket(node, transport, port, addresses, process):
    deadline = time.monotonic() + 5
    while True:
        output = _socket_rows(node, transport, port)
        if any(set(addresses) <= set(line.split()) for line in output.splitlines()):
            return output
        assert process.poll() is None, output
        assert time.monotonic() < deadline, output
        time.sleep(0.05)


def _read(output):
    return os.pread(output.fileno(), 1_000_000, 0)


def _wait_bytes(output, size, *, timeout):
    deadline = time.monotonic() + timeout
    while True:
        received = _read(output)
        if len(received) >= size or time.monotonic() >= deadline:
            return received
        time.sleep(0.02)


def _route(name, worker, role, source, destination):
    rules = json.loads(guest(name, "ip", "-j", "rule", "show"))
    assert any(
        rule.get("src") == source
        and str(rule.get("table")) == str(role["table"])
        and rule.get("priority") == role["rule_priority"]
        for rule in rules
    )
    routes = json.loads(
        guest(name, "ip", "-j", "route", "show", "table", str(role["table"]), f"{destination}/32")
    )
    assert len(routes) == 1 and routes[0]["protocol"] == "bird", routes
    hops = routes[0]["nexthops"]
    assert len(hops) == 2
    assert {hop["dev"] for hop in hops} == {
        f"{worker['interfaces'][f'pf{pf}']['name']}.{role['vlan']}" for pf in (0, 1)
    }
    assert all(hop["via"] == {"family": "inet6", "host": "fe80::1"} for hop in hops)
    return {"rules": rules, "routes": routes}


def _udp_records(token):
    result = []
    for sequence in range(UDP_RECORDS):
        header = f"{token}:{sequence:04d}:".encode()
        result.append(header + b"x" * (UDP_RECORD_BYTES - len(header) - 1) + b"\n")
    return result


def _udp_observations(received, expected):
    lookup = {record: sequence for sequence, record in enumerate(expected)}
    records = received.splitlines(keepends=True)
    sequences = [lookup[record] for record in records if record in lookup]
    highest = -1
    out_of_order = 0
    for sequence in sequences:
        if sequence < highest:
            out_of_order += 1
        highest = max(highest, sequence)
    return {
        "unit": "application records; not independently captured UDP datagrams",
        "expected_records": len(expected),
        "received_complete_records": len(sequences),
        "missing_records": len(expected) - len(set(sequences)),
        "duplicate_records": len(sequences) - len(set(sequences)),
        "out_of_order_records": out_of_order,
        "invalid_records_hex": [record.hex() for record in records if record not in lookup],
        "received_sequences": sequences,
    }


@pytest.mark.parametrize("name", WORKERS)
@pytest.mark.parametrize("role_id", STORAGE_ROLES)
@pytest.mark.parametrize("transport", ("tcp", "udp"))
def test_native_storage_source_bound_transport(name, role_id, transport, ownership):
    workers = json.loads(
        (ROOT / "runtime/terraform/lab/40-networking/terraform.tfstate").read_text()
    )["outputs"]["workers"]["value"]
    assert workers[name]["system_uuid"] == ownership["config"]["volatile.uuid"]
    remote = WORKERS[(WORKERS.index(name) + 2) % len(WORKERS)]
    assert workers[name]["rack"] != workers[remote]["rack"]
    target = json.loads(
        subprocess.check_output(
            ["lxc", "query", f"/1.0/instances/{remote}?project={PROJECT}"], text=True, timeout=30
        )
    )
    assert target["type"] == "virtual-machine" and target["status"] == "Running"
    assert target["config"]["user.maas-bird-owner"] == "network-lab"
    assert target["config"]["volatile.uuid"] == workers[remote]["system_uuid"]
    assert target["config"]["limits.cpu"] == "1" and target["config"]["limits.memory"] == "4GiB"
    roles = json.loads((ROOT / "maas/network-model/logical-roles.json").read_text())["roles"]
    role = next(item for item in roles if item["id"] == role_id)
    assert role["domain"] in {"ceph-access", "ceph-replication"}
    network = ip_network(role["address_block"])
    source = str(network.network_address + 20 + WORKERS.index(name))
    destination = str(network.network_address + 20 + WORKERS.index(remote))
    token = uuid.uuid4().hex[:16]
    records = _udp_records(token)
    payload = token.encode() * 8192 if transport == "tcp" else b"".join(records)
    receipt: dict[str, Any] = {
        "worker": name,
        "remote": remote,
        "role": role_id,
        "transport": transport,
        "source": source,
        "destination": destination,
        "token": token,
        "expected_bytes": len(payload),
        "expected_sha256": hashlib.sha256(payload).hexdigest(),
        "route_before": _route(name, workers[name], role, source, destination),
    }
    for _ in range(5):
        port = 40000 + secrets.randbelow(20000)
        if (
            not _socket_rows(name, transport, port).strip()
            and not _socket_rows(remote, transport, port).strip()
        ):
            break
    else:
        pytest.fail("No unused source/listener port found in the bounded selection")
    receipt["port"] = port
    receiver = sender = None
    with ExitStack() as stack:
        receive_output = stack.enter_context(tempfile.TemporaryFile())
        receive_errors = stack.enter_context(tempfile.TemporaryFile())
        send_output = stack.enter_context(tempfile.TemporaryFile())
        send_errors = stack.enter_context(tempfile.TemporaryFile())
        try:
            listen = ["-4", "-n", "-d", "-l"]
            if transport == "udp":
                listen.append("-u")
            receiver = _start(
                remote, [*listen, destination, str(port)], receive_output, receive_errors
            )
            receipt["listening_socket"] = _wait_socket(
                remote, transport, port, [f"{destination}:{port}"], receiver
            )
            arguments = ["-4", "-n", "-s", source, "-p", str(port), "-w", "10"]
            arguments += ["-N"] if transport == "tcp" else ["-u", "-q", "1"]
            sender = _start(
                name, [*arguments, destination, str(port)], send_output, send_errors, sender=True
            )
            assert sender.stdin is not None
            if transport == "tcp":
                receipt["connected_socket"] = _wait_socket(
                    remote,
                    transport,
                    port,
                    [f"{destination}:{port}", f"{source}:{port}"],
                    receiver,
                )
                assert sender.stdin.write(payload) == len(payload)
                sender.stdin.flush()
            else:
                receipt["submitted_records"] = 0
                for sequence, record in enumerate(records):
                    assert sender.stdin.write(record) == len(record)
                    sender.stdin.flush()
                    receipt["submitted_records"] += 1
                    if sequence == 0:
                        assert len(_wait_bytes(receive_output, len(record), timeout=5)) >= len(
                            record
                        )
                        receipt["connected_socket"] = _wait_socket(
                            remote,
                            transport,
                            port,
                            [f"{destination}:{port}", f"{source}:{port}"],
                            receiver,
                        )
                    time.sleep(0.05)
            sender.stdin.close()
            sender.stdin = None
            sender.communicate(timeout=15)
            receipt["sender_returncode"] = sender.returncode
            assert sender.returncode == 0, _read(send_errors).decode(errors="replace")
            received = _wait_bytes(receive_output, len(payload), timeout=2)
            receipt["received_bytes"] = len(received)
            receipt["received_sha256"] = hashlib.sha256(received).hexdigest()
            if transport == "udp":
                receipt["delivery"] = _udp_observations(received, records)
                assert receipt["delivery"]["missing_records"] == 0, receipt["delivery"]
                assert receipt["delivery"]["duplicate_records"] == 0, receipt["delivery"]
                assert receipt["delivery"]["out_of_order_records"] == 0, receipt["delivery"]
                assert receipt["delivery"]["invalid_records_hex"] == [], receipt["delivery"]
            else:
                receipt["delivery"] = {
                    "unit": "TCP application byte stream",
                    "packet_loss": "not inferred from TCP delivery",
                    "packet_order": "not inferred from TCP delivery",
                }
            assert received == payload
            assert receipt["received_sha256"] == receipt["expected_sha256"]
            receipt["route_after"] = _route(name, workers[name], role, source, destination)
            receipt["transport_assertions_passed"] = True
        except BaseException as error:
            receipt["failure"] = {"type": type(error).__name__, "message": str(error)}
            raise
        finally:
            try:
                try:
                    _stop(sender)
                finally:
                    _stop(receiver)
                received = _read(receive_output)
                receipt["received_bytes"] = len(received)
                receipt["received_sha256"] = hashlib.sha256(received).hexdigest()
                if transport == "udp":
                    receipt["delivery"] = _udp_observations(
                        received, records[: receipt.get("submitted_records", 0)]
                    )
                receipt["sender_stderr"] = _read(send_errors).decode(errors="replace")
                receipt["receiver_stderr"] = _read(receive_errors).decode(errors="replace")
                receipt["sender_final_returncode"] = sender.returncode if sender else None
                receipt["receiver_final_returncode"] = receiver.returncode if receiver else None
                deadline = time.monotonic() + 5
                while True:
                    receipt["source_sockets_after"] = _socket_rows(name, transport, port)
                    receipt["destination_sockets_after"] = _socket_rows(remote, transport, port)
                    rows = (
                        receipt["source_sockets_after"].splitlines()
                        + receipt["destination_sockets_after"].splitlines()
                    )
                    if all(row.split()[0] == "TIME-WAIT" for row in rows):
                        break
                    assert time.monotonic() < deadline, "A temporary endpoint remains active"
                    time.sleep(0.05)
            finally:
                directory = ROOT / "runtime/terraform/acceptance/storage-transport"
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                write_atomic(
                    directory / f"{name}-{role_id}-{transport}-{time.time_ns()}.json",
                    json.dumps(receipt, indent=2) + "\n",
                    0o600,
                )
