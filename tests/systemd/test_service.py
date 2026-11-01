"""Real systemd restart/boot admission in a disposable, explicitly owned LXD guest.

This exercises the installed charm environment, root/bird permissions and the actual
BIRD process. It does not emulate a physical DPU or attest NIC offload.
"""

import json
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from maas_bird import external_policy
from maas_bird.rack_runtime import RackService
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
                LXC, "exec", guest, "--", "systemctl", "is-active", "bird", check=False
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
    execute(
        "env",
        "DEBIAN_FRONTEND=noninteractive",
        "apt-get",
        "install",
        "-y",
        "bird2",
        "python3-venv",
    )
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
    # Exercise the shared library as an installed package in a charm-like venv,
    # not a hand-copied import tree. No application Python lives under /etc.
    project = Path(__file__).resolve().parents[2]
    command(
        "uv", "build", "--wheel", "--out-dir", str(tmp_path / "wheels"), str(project / "shared")
    )
    wheel = next((tmp_path / "wheels").glob("*.whl"))
    execute("python3", "-m", "venv", "/opt/bird-charm/venv")
    command(LXC, "file", "push", str(wheel), guest + "/tmp/" + wheel.name)
    execute("/opt/bird-charm/venv/bin/pip", "install", "/tmp/" + wheel.name)
    execute("systemctl", "unmask", "bird.service")
    execute("systemctl", "enable", "--now", "bird.service")
    wait_active(guest, True)
    # The vendor daemon is running before explicit unit placement takes it over.
    execute("mkdir", "-p", "/var/lib/maas-bird")
    command(LXC, "file", "push", str(service.marker), guest + "/var/lib/maas-bird/owner")
    python = "/opt/bird-charm/venv/bin/python"
    install = (
        "from pathlib import Path; from maas_bird.system_service import SystemBird; "
        "from maas_bird.rack_runtime import _unit; import sys; "
        "SystemBird(Path('/'), Path('/var/lib/maas-bird')).install(_unit(sys.executable))"
    )
    execute(python, "-c", install)
    wait_active(guest, False)
    # Before an accepted bundle exists, the real system service must refuse boot.
    assert execute("systemctl", "start", "bird", check=False).returncode != 0
    execute(
        python,
        "-c",
        "from pathlib import Path; from maas_bird.system_service import SystemBird; "
        "SystemBird(Path('/'), Path('/var/lib/maas-bird')).rollback()",
    )
    wait_active(guest, True)
    assert "-m maas_bird.external_policy" not in execute("systemctl", "cat", "bird").stdout
    execute(python, "-c", install)
    wait_active(guest, False)
    command(LXC, "file", "push", str(service.manifest), guest + "/var/lib/maas-bird/state.json")
    execute("systemctl", "reset-failed", "bird")
    execute("systemctl", "enable", "--now", "bird")
    wait_active(guest, True)
    assert (
        execute("stat", "-c", "%U:%G:%a", "/run/maas-bird-config/bird.conf").stdout.strip()
        == "root:bird:640"
    )
    assert (
        execute(
            "sudo", "-u", "bird", "cat", "/var/lib/maas-bird/state.json", check=False
        ).returncode
        != 0
    )
    execute(
        python,
        "-c",
        "from pathlib import Path; from maas_bird.system_service import SystemBird; "
        "SystemBird(Path('/'), Path('/var/lib/maas-bird')).commit()",
    )
    pid = execute("systemctl", "show", "bird", "--property=MainPID", "--value").stdout
    execute("ln", "-s", "/opt/bird-charm", "/opt/bird-charm-refreshed")
    execute(
        python,
        "-c",
        "from pathlib import Path; from maas_bird.system_service import SystemBird; "
        "from maas_bird.rack_runtime import _unit; "
        "SystemBird(Path('/'), Path('/var/lib/maas-bird')).install("
        "_unit('/opt/bird-charm-refreshed/venv/bin/python'))",
    )
    assert execute("systemctl", "show", "bird", "--property=MainPID", "--value").stdout == pid
    assert (
        "/opt/bird-charm-refreshed/venv/bin/python" in execute("systemctl", "cat", "bird").stdout
    )
    assert execute("test", "-e", "/etc/maas-bird", check=False).returncode != 0
    execute("systemctl", "restart", "bird")
    wait_active(guest, True)
    assert (
        "mb_candidate_systemd_test"
        in execute(
            "birdc",
            "-s",
            "/run/bird/bird.ctl",
            "show",
            "symbols",
            "mb_candidate_systemd_test",
        ).stdout
    )
    execute("systemctl", "stop", "bird")
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
    assert execute("systemctl", "start", "bird", check=False).returncode != 0
    wait_active(guest, False)
    assert execute("test", "-S", "/run/bird/bird.ctl", check=False).returncode != 0

    # A real reboot also loses our deliberately nonpersistent test policy. The
    # enabled system service must fail closed, independently of Juju hooks.
    command(LXC, "restart", guest)
    for _ in range(120):
        if command(LXC, "exec", guest, "--", "true", check=False).returncode == 0:
            break
        time.sleep(1)
    else:
        pytest.fail("Owned VM did not return after reboot")
    execute("systemctl", "is-system-running", "--wait", check=False)
    wait_active(guest, False)
    assert execute("test", "-S", "/run/bird/bird.ctl", check=False).returncode != 0

    execute(
        python,
        "-c",
        "from maas_bird.rack_runtime import RackService; "
        "RackService(owner='systemd-test').remove()",
    )
    assert execute("systemctl", "is-enabled", "bird", check=False).stdout.strip() == "masked"
    assert execute("readlink", "/etc/systemd/system/bird.service").stdout.strip() == "/dev/null"
    assert execute("test", "-e", "/var/lib/maas-bird", check=False).returncode != 0
    assert execute("find", "/var/backups/maas-bird", "-name", "*.json").stdout.strip()
