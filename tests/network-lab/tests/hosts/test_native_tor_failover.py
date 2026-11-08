"""Real worker forwarding across ToR routing-service and container failures."""

import json
import os
import re
import signal
import subprocess
import tempfile
import time
from contextlib import ExitStack
from ipaddress import ip_network
from typing import Any

import pytest
from maas_bird.runtime import write_atomic
from test_native_workers import PROJECT, ROOT, WORKERS, guest

TORS = ("r1a", "r1b", "r2a", "r2b")


def instance(node, *, state=False):
    suffix = "/state" if state else ""
    return json.loads(
        subprocess.check_output(
            ["lxc", "query", f"/1.0/instances/{node}{suffix}?project={PROJECT}"],
            text=True,
            timeout=30,
        )
    )


@pytest.mark.parametrize("tor", TORS)
@pytest.mark.parametrize("fault", ("frr", "container"))
def test_native_tor_failure_preserves_worker_roles_and_geneve(tor, fault):
    identity = instance(tor)
    assert identity["type"] == "container" and identity["status"] == "Running"
    assert identity["config"]["user.maas-bird-owner"] == "network-lab"
    assert identity["config"]["limits.cpu"] == "1"
    assert identity["config"]["limits.memory"] == "768MiB"
    assert identity["expanded_config"].get("security.privileged", "false") == "false"
    workers = json.loads(
        (ROOT / "runtime/terraform/lab/40-networking/terraform.tfstate").read_text()
    )["outputs"]["workers"]["value"]
    roles = json.loads((ROOT / "maas/network-model/logical-roles.json").read_text())["roles"]
    rack, slot = int(tor[1]), int(tor.endswith("b"))
    affected = [name for name in WORKERS if workers[name]["rack"] == rack]
    assert len(affected) == 2
    remote = next(name for name in WORKERS if workers[name]["rack"] != rack)
    for name in affected:
        row = instance(name)
        assert row["type"] == "virtual-machine" and row["status"] == "Running"
        assert row["config"]["user.maas-bird-owner"] == "network-lab"
        assert row["config"]["volatile.uuid"] == workers[name]["system_uuid"]
        assert row["config"]["limits.cpu"] == "1" and row["config"]["limits.memory"] == "4GiB"
    configuration = guest(tor, "sha256sum", "/etc/frr/frr.conf", "/etc/frr/daemons")
    receipt: dict[str, Any] = {
        "tor": tor,
        "fault": fault,
        "affected": affected,
        "configuration_sha256": configuration,
        "before_pid": guest(tor, "pidof", "bgpd").strip(),
        "before_container_pid": instance(tor, state=True)["pid"],
        "stop_observed": False,
    }

    def health(disabled):
        result = {}
        for name in affected:
            protocols = guest(name, "birdc", "show", "protocols", "all")
            bfd = guest(name, "birdc", "show", "bfd", "sessions")
            count = 5 if disabled else 10
            assert protocols.count("BGP state:          Established") == count, protocols
            assert len(re.findall(r"(?m)^fe80:.*\sUp\s", bfd)) == count, bfd
            routes = {}
            for role in roles:
                destination = str(
                    ip_network(role["address_block"]).network_address + 20 + WORKERS.index(remote)
                )
                observed = json.loads(
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
                assert len(observed) == 1 and observed[0]["protocol"] == "bird", observed
                hops = observed[0].get("nexthops", [observed[0]])
                expected = {
                    f"{workers[name]['interfaces'][f'pf{pf}']['name']}.{role['vlan']}"
                    for pf in (0, 1)
                    if not disabled or pf != slot
                }
                assert len(hops) == len(expected)
                assert {hop["dev"] for hop in hops} == expected, observed
                assert all(hop["via"] == {"family": "inet6", "host": "fe80::1"} for hop in hops)
                routes[role["id"]] = observed
            result[name] = {"bgp": protocols, "bfd": bfd, "routes": routes}
        return result

    def await_health(disabled):
        started = time.monotonic()
        limit = 90 if fault == "container" and not disabled else 30
        while True:
            try:
                result = health(disabled)
                return {"elapsed": time.monotonic() - started, "workers": result}
            except (AssertionError, subprocess.CalledProcessError):
                if time.monotonic() - started >= limit:
                    raise
                time.sleep(0.2)

    def role_packets():
        result = {}
        for name in affected:
            result[name] = {}
            for role in roles:
                network = ip_network(role["address_block"])
                source = str(network.network_address + 20 + WORKERS.index(name))
                destination = str(network.network_address + 20 + WORKERS.index(remote))
                output = guest(
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
                assert "3 packets transmitted, 3 received" in output
                result[name][role["id"]] = output
            result[name]["management"] = guest(
                name, "ping", "-n", "-c", "3", "-W", "1", "-I", "bond-mgmt", "10.203.241.10"
            )
        return result

    receipt["before"] = await_health(False)
    receipt["before_packets"] = role_packets()
    streams = {}

    def replies(output):
        text = os.pread(output.fileno(), 2_000_000, 0).decode()
        return [
            (float(stamp), int(sequence))
            for stamp, sequence in re.findall(
                r"(?m)^\[(\d+\.\d+)\].* bytes from .*: icmp_seq=(\d+)", text
            )
        ]

    def await_replies(after):
        deadline = time.monotonic() + 10
        while True:
            observed = {name: replies(output) for name, (_, output, _) in streams.items()}
            if all(
                sum(sequence > after[name] for _, sequence in rows) >= 3
                for name, rows in observed.items()
            ):
                return {name: rows[-1][1] for name, rows in observed.items()}
            assert all(process.poll() is None for process, _, _ in streams.values())
            assert time.monotonic() < deadline, observed
            time.sleep(0.1)

    with ExitStack() as stack:
        try:
            for name in affected:
                output = stack.enter_context(tempfile.TemporaryFile())
                errors = stack.enter_context(tempfile.TemporaryFile())
                process = subprocess.Popen(
                    [
                        "lxc",
                        "exec",
                        "--project",
                        PROJECT,
                        name,
                        "--",
                        "ping",
                        "-n",
                        "-D",
                        "-O",
                        "-i",
                        "0.2",
                        "-w",
                        "120",
                        "-W",
                        "1",
                        "-M",
                        "do",
                        "-s",
                        "1372",
                        "-I",
                        "tenant0",
                        f"172.30.10.{20 + WORKERS.index(remote)}",
                    ],
                    stdout=output,
                    stderr=errors,
                    text=True,
                )
                streams[name] = (process, output, errors)
            receipt["baseline_last_sequences"] = await_replies(dict.fromkeys(affected, 0))
            receipt["fault_started"] = time.monotonic()
            if fault == "frr":
                guest(tor, "systemctl", "stop", "frr")
                state = subprocess.run(
                    [
                        "lxc",
                        "exec",
                        "--project",
                        PROJECT,
                        tor,
                        "--",
                        "systemctl",
                        "is-active",
                        "frr",
                    ],
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                assert state.returncode != 0 and state.stdout.strip() == "inactive"
            else:
                subprocess.run(
                    ["lxc", "stop", "--project", PROJECT, tor, "--timeout=30"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
                assert instance(tor)["status"] == "Stopped"
            receipt["stop_observed"] = True
            receipt["held_state"] = await_health(True)
            receipt["withdrawal_observed"] = time.monotonic()
            receipt["held_packets"] = role_packets()
            cutoff = {name: replies(output)[-1][1] for name, (_, output, _) in streams.items()}
            receipt["held_last_sequences"] = await_replies(cutoff)
        except BaseException as error:
            receipt["failure"] = {"type": type(error).__name__, "message": str(error)}
            raise
        finally:
            try:
                receipt["restore_started"] = time.monotonic()
                if fault == "container":
                    if instance(tor)["status"] != "Running":
                        subprocess.run(
                            ["lxc", "start", "--project", PROJECT, tor],
                            check=True,
                            capture_output=True,
                            text=True,
                            timeout=45,
                        )
                else:
                    guest(tor, "systemctl", "start", "frr")
                receipt["restored_state"] = await_health(False)
                receipt["routing_restored_at"] = time.monotonic()
                receipt["restored_packets"] = role_packets()
                assert (
                    instance(tor)["config"]["volatile.uuid"] == identity["config"]["volatile.uuid"]
                )
                assert (
                    guest(tor, "sha256sum", "/etc/frr/frr.conf", "/etc/frr/daemons")
                    == configuration
                )
                receipt["after_pid"] = guest(tor, "pidof", "bgpd").strip()
                receipt["after_container_pid"] = instance(tor, state=True)["pid"]
                if receipt.get("stop_observed", False):
                    if fault == "frr":
                        assert receipt["after_pid"] != receipt["before_pid"]
                    else:
                        assert receipt["after_container_pid"] != receipt["before_container_pid"]
                    cutoff = {
                        name: replies(output)[-1][1] for name, (_, output, _) in streams.items()
                    }
                    receipt["restored_last_sequences"] = await_replies(cutoff)
            finally:
                receipt["flows"] = {}
                for name, (process, output, errors) in streams.items():
                    if process.poll() is None:
                        process.send_signal(signal.SIGINT)
                    process.communicate(timeout=15)
                    text = os.pread(output.fileno(), 2_000_000, 0).decode()
                    rows = replies(output)
                    counts = re.search(r"(\d+) packets transmitted, (\d+) received", text)
                    receipt["flows"][name] = {
                        "stdout": text,
                        "stderr": os.pread(errors.fileno(), 2_000_000, 0).decode(),
                        "returncode": process.returncode,
                        "transmitted": int(counts[1]) if counts else None,
                        "received": int(counts[2]) if counts else None,
                        "max_inter_reply_gap_seconds": max(
                            (b[0] - a[0] for a, b in zip(rows, rows[1:], strict=False)),
                            default=None,
                        ),
                        "duplicate_replies": len(rows) - len({sequence for _, sequence in rows}),
                        "reordered_replies": sum(
                            b[1] < a[1] for a, b in zip(rows, rows[1:], strict=False)
                        ),
                    }
                directory = ROOT / "runtime/terraform/acceptance/tor-failures"
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                write_atomic(
                    directory / f"{tor}-{fault}-{time.time_ns()}.json",
                    json.dumps(receipt, indent=2) + "\n",
                    0o600,
                )
    for flow in receipt["flows"].values():
        assert flow["returncode"] == 0
        assert flow["received"] >= 10
        assert flow["transmitted"] >= flow["received"]
        assert flow["duplicate_replies"] == flow["reordered_replies"] == 0
