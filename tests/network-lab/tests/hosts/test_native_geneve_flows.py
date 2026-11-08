"""Bounded direct/Geneve TCP egress experiment on the current Noble HWE hosts.

Sixteen 8-KiB streams vary both TCP ports while retaining one VTEP pair.
Hash policy is temporary and restored exactly. No packet marks, routing rules,
TC filters or OVN configuration are changed. TCP payload integrity is checked;
TCP delivery is not used to infer packet loss or receive ordering.
"""

import hashlib
import importlib
import json
import re
import secrets
import subprocess
import tempfile
import time
import uuid
from contextlib import ExitStack
from typing import Any

import pytest
from maas_bird.runtime import write_atomic
from test_native_storage_transport import _read, _start, _stop, _wait_socket
from test_native_workers import PROJECT, ROOT, guest

SOURCE, TARGET = "probe", "probe-r2a"
FLOW_COUNT, PAYLOAD_BYTES = 16, 8192
pytestmark = pytest.mark.integration


def configuration():
    workers = json.loads(
        (ROOT / "runtime/terraform/lab/40-networking/terraform.tfstate").read_text()
    )["outputs"]["workers"]["value"]
    ovn = {
        key: value["value"]
        for key, value in json.loads(
            (ROOT / "runtime/terraform/lab/90-ovn/terraform.tfstate").read_text()
        )["outputs"].items()
    }
    assert ovn["compute_enabled"] is True
    kernels = {}
    for name in (SOURCE, TARGET):
        instance = json.loads(
            subprocess.check_output(
                ["lxc", "query", f"/1.0/instances/{name}?project={PROJECT}"], text=True, timeout=30
            )
        )
        assert instance["type"] == "virtual-machine" and instance["status"] == "Running"
        assert instance["config"]["user.maas-bird-owner"] == "network-lab"
        assert (
            instance["config"]["limits.cpu"] == "1"
            and instance["config"]["limits.memory"] == "4GiB"
        )
        assert (
            instance["config"]["volatile.uuid"]
            == workers[name]["system_uuid"]
            == ovn["chassis"][name]["uuid"]
        )
        assert "br-provider" not in guest(name, "ovs-vsctl", "list-br").split()
        kernel = guest(name, "uname", "-r").strip()
        depends = guest(name, "dpkg-query", "-W", "-f=${Depends}", "linux-image-generic-hwe-24.04")
        assert f"linux-image-{kernel}" in depends and not kernel.startswith("6.8.")
        kernels[name] = kernel
    paths = {
        vlan["name"] for vlan in workers[SOURCE]["vlans"].values() if vlan["role"] == "general"
    }
    route = json.loads(
        guest(
            SOURCE,
            "ip",
            "-j",
            "route",
            "show",
            "table",
            "4400",
            f"{ovn['chassis'][TARGET]['vtep']}/32",
        )
    )
    assert len(route) == 1 and route[0]["protocol"] == "bird"
    assert len(route[0]["nexthops"]) == 2 and {hop["dev"] for hop in route[0]["nexthops"]} == paths
    assert all(
        hop["via"] == {"family": "inet6", "host": "fe80::1"} for hop in route[0]["nexthops"]
    )
    return ovn["chassis"], paths, kernels


def observations(captures, flows, mode, source_vtep, target_vtep):
    expected = {(flow["source_port"], flow["target_port"]): flow for flow in flows}
    result: dict[str, Any] = {
        str(index): {
            "devices": set(),
            "outer_udp_source_ports": set(),
            "packets": 0,
            "payload_packets": 0,
        }
        for index in range(len(flows))
    }
    for device, text in captures.items():
        packets = re.findall(r"(?ms)^\d+\.\d+ IP .*?(?=^\d+\.\d+ IP |\Z)", text)
        for packet in packets:
            for (source_port, target_port), flow in expected.items():
                inner = re.search(
                    rf"(?m)^\s*{re.escape(flow['source'])}\.{source_port} > "
                    rf"{re.escape(flow['target'])}\.{target_port}: (.*)$",
                    packet,
                )
                if inner is None or "Flags [" not in inner[1]:
                    continue
                row = result[str(flow["index"])]
                row["devices"].add(device)
                row["packets"] += 1
                length = re.search(r"\blength (\d+)", inner[1])
                row["payload_packets"] += int(length is not None and int(length[1]) > 0)
                if mode == "geneve":
                    outer = re.search(
                        r"(?m)^\s*(\d+\.\d+\.\d+\.\d+)\.(\d+) > "
                        r"(\d+\.\d+\.\d+\.\d+)\.6081:.*Geneve",
                        packet,
                    )
                    assert outer is not None, packet
                    assert (outer[1], outer[3]) == (source_vtep, target_vtep), packet
                    row["outer_udp_source_ports"].add(int(outer[2]))
                else:
                    assert "Geneve" not in packet
    return {
        key: {
            **row,
            "devices": sorted(row["devices"]),
            "outer_udp_source_ports": sorted(row["outer_udp_source_ports"]),
        }
        for key, row in result.items()
    }


@pytest.mark.parametrize("mode", ("direct", "geneve"))
@pytest.mark.parametrize("policy", (0, 1))
def test_native_hwe_tcp_egress_distribution(mode, policy, monkeypatch):
    chassis, paths, kernels = configuration()
    source_vtep, target_vtep = chassis[SOURCE]["vtep"], chassis[TARGET]["vtep"]
    source = source_vtep if mode == "direct" else chassis[SOURCE]["endpoint"]["address"]
    target = target_vtep if mode == "direct" else chassis[TARGET]["endpoint"]["address"]
    original = guest(SOURCE, "cat", "/proc/sys/net/ipv4/fib_multipath_hash_policy").strip()
    assert original in {"0", "1", "2", "3"}
    occupied = set()
    for name in (SOURCE, TARGET):
        occupied.update(
            int(port)
            for port in re.findall(r":(\d+)\b", guest(name, "ss", "-H", "-n", "-4", "-a", "-t"))
        )
    for _ in range(5):
        source_base, target_base = 42000 + secrets.randbelow(5000), 50000 + secrets.randbelow(5000)
        if not occupied.intersection(
            {source_base + index for index in range(FLOW_COUNT)}
            | {target_base + index for index in range(FLOW_COUNT)}
        ):
            break
    else:
        pytest.fail("No unused TCP port ranges found in the bounded selection")
    token = uuid.uuid4().hex
    flows = [
        {
            "index": index,
            "source": source,
            "target": target,
            "source_port": source_base + index,
            "target_port": target_base + index,
        }
        for index in range(FLOW_COUNT)
    ]
    receipt: dict[str, Any] = {
        "mode": mode,
        "policy": policy,
        "original_policy": original,
        "kernels": kernels,
        "source_vtep": source_vtep,
        "target_vtep": target_vtep,
        "payload_bytes_per_flow": PAYLOAD_BYTES,
        "flows": flows,
        "captures": {},
        "tcp_packet_loss_and_order": "not inferred from TCP byte-stream delivery",
    }
    monkeypatch.syspath_prepend(str(ROOT / "tests/lxd"))
    wait_capture_ready = importlib.import_module("test_native_gateways").wait_capture_ready
    processes = {}
    with ExitStack() as files:
        try:
            guest(SOURCE, "sysctl", "-w", f"net.ipv4.fib_multipath_hash_policy={policy}")
            assert guest(
                SOURCE, "cat", "/proc/sys/net/ipv4/fib_multipath_hash_policy"
            ).strip() == str(policy)
            expression = (
                "udp dst port 6081"
                if mode == "geneve"
                else f"tcp and src host {source} and dst host {target}"
            )
            for device in sorted(paths):
                output = files.enter_context(tempfile.TemporaryFile())
                args = ["timeout", "--signal=INT", "--kill-after=2", "120"]
                args += [
                    "tcpdump",
                    "-tt",
                    "-n",
                    "-l",
                    "-vv",
                    "-Q",
                    "out",
                    "-i",
                    device,
                    expression,
                ]
                command = ["lxc", "exec", "--project", PROJECT, SOURCE, "--", *args]
                process = subprocess.Popen(
                    command, stdout=output, stderr=subprocess.PIPE, text=True
                )
                processes[device] = (process, output)
                receipt["captures"][device] = {"startup": wait_capture_ready(process)}
            for flow in flows:
                payload = hashlib.sha256(f"{token}:{flow['index']}".encode()).digest() * 256
                flow["expected_sha256"] = hashlib.sha256(payload).hexdigest()
                receiver = sender = None
                receive_output = files.enter_context(tempfile.TemporaryFile())
                receive_errors = files.enter_context(tempfile.TemporaryFile())
                send_output = files.enter_context(tempfile.TemporaryFile())
                send_errors = files.enter_context(tempfile.TemporaryFile())
                try:
                    receiver = _start(
                        TARGET,
                        ["-4", "-n", "-d", "-l", target, str(flow["target_port"])],
                        receive_output,
                        receive_errors,
                    )
                    flow["listening_socket"] = _wait_socket(
                        TARGET,
                        "tcp",
                        flow["target_port"],
                        [f"{target}:{flow['target_port']}"],
                        receiver,
                    )
                    args = ["-4", "-n", "-N", "-w", "5", "-s", source]
                    args += ["-p", str(flow["source_port"]), target, str(flow["target_port"])]
                    sender = _start(SOURCE, args, send_output, send_errors, sender=True)
                    flow["connected_socket"] = _wait_socket(
                        TARGET,
                        "tcp",
                        flow["target_port"],
                        [f"{target}:{flow['target_port']}", f"{source}:{flow['source_port']}"],
                        receiver,
                    )
                    assert sender.stdin is not None
                    assert sender.stdin.write(payload) == PAYLOAD_BYTES
                    sender.stdin.close()
                    sender.stdin = None
                    sender.communicate(timeout=10)
                    receiver.communicate(timeout=10)
                    assert sender.returncode == receiver.returncode == 0
                    received = _read(receive_output)
                    flow["received_bytes"] = len(received)
                    flow["received_sha256"] = hashlib.sha256(received).hexdigest()
                    assert (
                        received == payload and flow["received_sha256"] == flow["expected_sha256"]
                    )
                finally:
                    try:
                        _stop(sender)
                    finally:
                        _stop(receiver)
                    flow["sender_stderr"] = _read(send_errors).decode(errors="replace")
                    flow["receiver_stderr"] = _read(receive_errors).decode(errors="replace")
            deadline = time.monotonic() + 5
            while True:
                observed = observations(
                    {device: _read(output).decode() for device, (_, output) in processes.items()},
                    flows,
                    mode,
                    source_vtep,
                    target_vtep,
                )
                if all(row["payload_packets"] > 0 for row in observed.values()):
                    break
                assert time.monotonic() < deadline, observed
                assert all(process.poll() is None for process, _ in processes.values())
                time.sleep(0.05)
            for device, (process, output) in processes.items():
                _stop(process)
                _, errors = process.communicate(timeout=5)
                receipt["captures"][device].update(
                    stdout=_read(output).decode(), stderr=errors, returncode=process.returncode
                )
                assert process.returncode in {0, 130}, errors
                assert "0 packets dropped by kernel" in errors, errors
            receipt["paths"] = observed
            receipt["devices"] = sorted(
                {device for row in observed.values() for device in row["devices"]}
            )
            receipt["outer_udp_source_ports"] = sorted(
                {port for row in observed.values() for port in row["outer_udp_source_ports"]}
            )
            assert all(len(row["devices"]) == 1 for row in observed.values()), observed
            if mode == "geneve":
                assert all(len(row["outer_udp_source_ports"]) == 1 for row in observed.values()), (
                    observed
                )
            assert len(receipt["devices"]) == (1 if policy == 0 else 2), receipt["devices"]
            receipt["distribution_passed"] = True
        except BaseException as error:
            receipt["failure"] = {"type": type(error).__name__, "message": str(error)}
            raise
        finally:
            try:
                for device, (process, output) in processes.items():
                    _stop(process)
                    _, errors = process.communicate(timeout=5)
                    receipt["captures"].setdefault(device, {}).update(
                        stdout=_read(output).decode(), stderr=errors, returncode=process.returncode
                    )
            finally:
                guest(SOURCE, "sysctl", "-w", f"net.ipv4.fib_multipath_hash_policy={original}")
                restored = guest(
                    SOURCE, "cat", "/proc/sys/net/ipv4/fib_multipath_hash_policy"
                ).strip()
                receipt["restored_policy"] = restored
                directory = ROOT / "runtime/terraform/acceptance/geneve-flows"
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                write_atomic(
                    directory / f"{mode}-policy{policy}-{time.time_ns()}.json",
                    json.dumps(receipt, indent=2) + "\n",
                    0o600,
                )
                assert restored == original
