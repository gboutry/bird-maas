"""Real worker BIRD and Juju-machine-agent process failure and recovery."""

import json
import subprocess
import time
from typing import Any

import pytest
from maas_bird.runtime import write_atomic
from test_native_reboot import juju_boot_id
from test_native_workers import PROJECT, ROOT, WORKERS, guest
from test_native_workers import agents as agents
from test_native_workers import inventory as inventory
from test_native_workers import ownership as ownership
from test_native_workers import (
    test_native_bird_sessions_and_source_bound_role_packets as assert_host_forwarding,
)


@pytest.mark.parametrize("name", WORKERS)
@pytest.mark.parametrize("process", ("bird", "juju-agent"))
def test_native_worker_process_stop_and_recovery(name, process, ownership, inventory, agents):
    machine = agents[inventory[name]["system_id"]]["id"]
    service = "bird" if process == "bird" else f"jujud-machine-{machine}"
    before_pid = guest(name, "systemctl", "show", service, "--property=MainPID", "--value").strip()
    assert before_pid.isdigit() and int(before_pid) > 0
    boot_id = guest(name, "cat", "/proc/sys/kernel/random/boot_id").strip()
    assert juju_boot_id(machine) == boot_id
    assert_host_forwarding(name, ownership)
    roles = json.loads((ROOT / "maas/network-model/logical-roles.json").read_text())["roles"]
    rules = guest(name, "ip", "-j", "rule", "show")
    addresses = guest(name, "ip", "-j", "address", "show", "lo")
    configuration = guest(name, "sha256sum", "/etc/bird/bird.conf")
    receipt: dict[str, Any] = {
        "worker": name,
        "process": process,
        "service": service,
        "before_pid": before_pid,
        "boot_id": boot_id,
        "configuration_sha256": configuration,
    }
    unit_state = guest(
        name, "systemctl", "show", service, "--property=UnitFileState", "--value"
    ).strip()
    assert unit_state not in {"masked", "masked-runtime"}
    try:
        receipt["stop_started"] = time.monotonic()
        guest(name, "systemctl", "stop", service)
        state = subprocess.run(
            ["lxc", "exec", "--project", PROJECT, name, "--", "systemctl", "is-active", service],
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert state.returncode != 0 and state.stdout.strip() == "inactive", state.stdout
        if process == "bird":
            receipt["withdrawn_tables"] = {}
            for role in roles:
                table = json.loads(
                    guest(name, "ip", "-j", "route", "show", "table", str(role["table"]))
                )
                assert all(route.get("protocol") != "bird" for route in table), table
                assert any(
                    route.get("dst") == "default"
                    and route.get("type") == "unreachable"
                    and route.get("metric") == 42760
                    for route in table
                ), table
                receipt["withdrawn_tables"][role["id"]] = table
            source = f"10.205.10.{20 + WORKERS.index(name)}"
            destination = "10.205.10.22" if name in WORKERS[:2] else "10.205.10.20"
            denied = subprocess.run(
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
                    "2",
                    "-W",
                    "1",
                    "-I",
                    source,
                    destination,
                ],
                text=True,
                capture_output=True,
                timeout=10,
            )
            assert denied.returncode != 0
            receipt["guarded_packets"] = {
                "returncode": denied.returncode,
                "stdout": denied.stdout,
                "stderr": denied.stderr,
            }
        else:
            assert_host_forwarding(name, ownership)
            receipt["forwarding_while_agent_stopped"] = True
        assert guest(name, "ip", "-j", "rule", "show") == rules
        assert guest(name, "ip", "-j", "address", "show", "lo") == addresses
        receipt["management_while_stopped"] = guest(
            name,
            "ping",
            "-n",
            "-c",
            "3",
            "-i",
            "0.2",
            "-W",
            "1",
            "-I",
            "bond-mgmt",
            "10.203.241.10",
        )
        assert "3 packets transmitted, 3 received" in receipt["management_while_stopped"]
        state = subprocess.run(
            ["lxc", "exec", "--project", PROJECT, name, "--", "systemctl", "is-active", service],
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert state.returncode != 0 and state.stdout.strip() == "inactive", state.stdout
        receipt["held_fault_verified"] = True
    except BaseException as error:
        receipt["failure"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        try:
            receipt["restore_started"] = time.monotonic()
            guest(name, "systemctl", "start", service)
            assert (
                guest(
                    name, "systemctl", "show", service, "--property=UnitFileState", "--value"
                ).strip()
                == unit_state
            )
            deadline = time.monotonic() + 30
            while True:
                try:
                    assert_host_forwarding(name, ownership)
                    break
                except (AssertionError, subprocess.CalledProcessError):
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.2)
            receipt["forwarding_restored_at"] = time.monotonic()
            assert guest(name, "systemctl", "is-active", service).strip() == "active"
            after_pid = guest(
                name, "systemctl", "show", service, "--property=MainPID", "--value"
            ).strip()
            assert after_pid.isdigit() and int(after_pid) > 0 and after_pid != before_pid
            assert guest(name, "sha256sum", "/etc/bird/bird.conf") == configuration
            assert guest(name, "ip", "-j", "rule", "show") == rules
            assert guest(name, "ip", "-j", "address", "show", "lo") == addresses
            assert juju_boot_id(machine) == boot_id
            receipt["after_pid"] = after_pid
            receipt["native_juju_exec_completed"] = True
        finally:
            directory = ROOT / "runtime/terraform/acceptance/worker-processes"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_atomic(
                directory / f"{name}-{process}-{time.time_ns()}.json",
                json.dumps(receipt, indent=2) + "\n",
                0o600,
            )
