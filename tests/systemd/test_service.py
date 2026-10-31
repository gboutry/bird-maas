"""Real systemd restart/boot admission in a disposable, explicitly owned LXD guest.

This exercises installed stdlib helpers, root/bird permissions and the actual
BIRD process. It does not emulate a physical DPU or attest NIC offload.
"""

import json
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from maas_bird import external_policy, policy_objects, runtime
from maas_bird.rack_runtime import RackService, _unit
from maas_bird.render import render_rack
from maas_bird.resolver import resolve_rack

from tests.factories import rack_inputs

pytestmark = pytest.mark.integration
LXC = "/snap/bin/lxc"


def command(*args, check=True):
    return subprocess.run(args, text=True, capture_output=True, check=check, timeout=600)


@pytest.fixture
def guest(tmp_path):
    name = "maas-bird-systemd-" + uuid.uuid4().hex[:10]
    owner = "maas-bird-systemd-test"
    command(
        LXC,
        "launch",
        "ubuntu:24.04",
        name,
        "--vm",
        "-c",
        "limits.cpu=2",
        "-c",
        "limits.memory=1GiB",
        "-d",
        "root,size=12GiB",
        "-c",
        "user.maas-bird-owner=" + owner,
    )
    (tmp_path / "owned-instance.json").write_text(json.dumps({"name": name, "owner": owner}))
    try:
        # Containers intentionally cannot prove physical DMI admission. Use a VM
        # with its own real firmware UUID and wait for the native LXD agent.
        for _ in range(120):
            if command(LXC, "exec", name, "--", "true", check=False).returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("Owned VM agent did not become ready")
        command(LXC, "exec", name, "--", "cloud-init", "status", "--wait")
        yield name
    finally:
        # Verify ownership immediately before the only destructive operation.
        observed = command(LXC, "config", "get", name, "user.maas-bird-owner").stdout.strip()
        assert observed == owner
        command(LXC, "delete", "--force", name)


def wait_active(guest, expected):
    for _ in range(100):
        active = (
            command(
                LXC, "exec", guest, "--", "systemctl", "is-active", "maas-bird", check=False
            ).returncode
            == 0
        )
        if active == expected:
            return
        time.sleep(0.1)
    pytest.fail(f"Owned service active state did not become {expected}")


def test_accepted_bundle_restart_and_missing_guard_boot_gate(guest, tmp_path):
    def execute(*args, check=True):
        return command(LXC, "exec", guest, "--", *map(str, args), check=check)

    execute("systemctl", "mask", "bird.service")
    execute("apt-get", "update", "-qq")
    execute("env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "bird2")
    version = execute("bird", "--version")
    assert (version.stdout + version.stderr).strip() == "BIRD version 2.14"
    uid = execute("cat", "/sys/class/dmi/id/product_uuid").stdout.strip().lower()
    plan = resolve_rack(**rack_inputs())
    plan = plan.model_copy(update={"host": plan.host.model_copy(update={"system_uuid": uid})})
    # A zero-export accepted baseline permits lifecycle tests without pretending
    # that a synthetic TOR session is established in this systemd-only boundary.
    plan = plan.model_copy(
        update={
            "domains": tuple(
                d.model_copy(
                    update={
                        "prefixes": (),
                        "contexts": tuple(
                            c.model_copy(update={"exports": ()}) for c in d.contexts
                        ),
                    }
                )
                for d in plan.domains
            )
        }
    )
    for session in plan.sessions:
        execute("ip", "link", "add", session.interface, "type", "dummy")
        execute("ip", "link", "set", session.interface, "up")
        execute(
            "ip", "-6", "address", "add", f"{session.local}/64", "dev", session.interface, "nodad"
        )
    recipe = external_policy.expected_policy(plan)
    for source in recipe["sources"]:
        execute("ip", "address", "add", source, "dev", "lo")
    for rule in recipe["rules"]:
        execute(
            "ip",
            "rule",
            "add",
            "priority",
            rule["priority"],
            "from",
            rule["src"],
            "table",
            rule["table"],
            "protocol",
            rule["protocol"],
        )
    for table in recipe["tables"]:
        execute(
            "ip",
            "route",
            "add",
            "unreachable",
            "default",
            "table",
            table,
            "proto",
            "196",
            "metric",
            "42760",
        )
    root = tmp_path / "root"
    service = RackService(root, owner="systemd-test")
    service.directory.mkdir(parents=True)
    service.marker.write_text("systemd-test\n")
    service.marker.chmod(0o600)
    service._producers = {}
    passwords = {s.id: "test-only" for s in plan.sessions}
    service._credentials = service._credential_hashes(plan, passwords)
    service._commit(plan, render_rack(plan, passwords), "systemd_test")
    package = service.directory / "maas_bird"
    package.mkdir()
    (package / "__init__.py").write_text("")
    for module in (external_policy, policy_objects, runtime):
        (package / Path(module.__file__).name).write_text(Path(module.__file__).read_text())
    (service.directory / "check_policy.py").write_text(
        "from maas_bird.external_policy import main\nmain()\n"
    )
    service.unit.parent.mkdir(parents=True)
    service.unit.write_text(_unit())
    command(LXC, "file", "push", "-r", str(service.directory), guest + "/etc/")
    command(
        LXC, "file", "push", str(service.unit), guest + "/etc/systemd/system/maas-bird.service"
    )
    execute("systemctl", "daemon-reload")
    execute("systemctl", "enable", "--now", "maas-bird")
    wait_active(guest, True)
    assert (
        execute("stat", "-c", "%U:%G:%a", "/run/maas-bird-config/bird.conf").stdout.strip()
        == "root:bird:640"
    )
    assert (
        execute("sudo", "-u", "bird", "cat", "/etc/maas-bird/state.json", check=False).returncode
        != 0
    )
    execute("systemctl", "restart", "maas-bird")
    wait_active(guest, True)
    assert (
        "mb_candidate_systemd_test"
        in execute(
            "birdc",
            "-s",
            "/run/maas-bird/bird.ctl",
            "show",
            "symbols",
            "mb_candidate_systemd_test",
        ).stdout
    )
    execute("systemctl", "stop", "maas-bird")
    execute(
        "ip",
        "route",
        "del",
        "unreachable",
        "default",
        "table",
        recipe["tables"][0],
        "proto",
        "196",
        "metric",
        "42760",
    )
    assert execute("systemctl", "start", "maas-bird", check=False).returncode != 0
    wait_active(guest, False)
    assert execute("test", "-S", "/run/maas-bird/bird.ctl", check=False).returncode != 0
