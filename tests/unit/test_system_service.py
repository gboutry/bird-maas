"""Takeover uses real private files; command doubles model only systemd state."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from maas_bird import system_service
from maas_bird.rack_runtime import _unit
from maas_bird.runtime import RuntimeErrorBase
from maas_bird.system_service import SystemBird


@pytest.fixture
def original(tmp_path, monkeypatch):
    directory = tmp_path / "var/lib/maas-bird"
    directory.mkdir(parents=True)
    (directory / "owner").write_text("model:bird/0\n")
    service = SystemBird(tmp_path, directory)
    service.unit.parent.mkdir(parents=True)
    service.unit.write_text("[Service]\nExecStart=/usr/sbin/bird -f\n")
    dropin = service.unit.with_name("bird.service.d") / "override.conf"
    dropin.parent.mkdir()
    dropin.write_text("[Service]\nRestart=no\n")
    calls = []

    def run(args, check=True):
        calls.append(args)
        out = {"is-enabled": "enabled\n", "is-active": "active\n"}.get(args[1], "")
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(system_service, "run", run)
    return service, dropin, calls


def test_takeover_snapshots_before_stopping_and_restores_on_first_failure(original):
    service, dropin, calls = original
    original_unit, original_dropin = service.unit.read_bytes(), dropin.read_bytes()
    service.install(_unit(sys.executable))
    assert service.pending()
    assert not dropin.exists()
    assert "-m maas_bird.external_policy /var/lib/maas-bird" in service.unit.read_text()
    assert service.journal.stat().st_mode & 0o777 == 0o600
    assert ["systemctl", "stop", "bird.service"] in calls
    service.rollback()
    assert service.unit.read_bytes() == original_unit
    assert dropin.read_bytes() == original_dropin
    assert calls[-2:] == [
        ["systemctl", "enable", "bird.service"],
        ["systemctl", "start", "bird.service"],
    ]
    assert not service.journal.exists()


def test_interrupted_takeover_never_overwrites_original_snapshot(original):
    service, _, _ = original
    service.install(_unit(sys.executable))
    before = service.journal.read_bytes()
    service.install(_unit(sys.executable))
    assert service.journal.read_bytes() == before
    service.rollback()
    assert "ExecStart=/usr/sbin/bird -f" in service.unit.read_text()


def test_committed_refresh_changes_entrypoint_without_stopping_routes(original):
    service, _, calls = original
    service.install(_unit("/opt/charm/venv/bin/python"))
    service.commit()
    calls.clear()
    service.install(_unit("/opt/new-charm/venv/bin/python"))
    assert calls == [["systemctl", "daemon-reload"]]
    assert "/opt/new-charm/venv/bin/python" in service.unit.read_text()
    assert not list((service.root / "etc").rglob("*.py"))
    with pytest.raises(RuntimeErrorBase, match="completed"):
        service.rollback()


def test_decommission_masks_vendor_without_resurrecting_old_exports(original):
    service, _, calls = original
    service.install(_unit(sys.executable))
    service.commit()
    before = service.journal.read_bytes()
    calls.clear()
    service.decommission()
    assert service.unit.readlink() == Path("/dev/null")
    archives = list((service.root / "var/backups/maas-bird").glob("*.json"))
    assert len(archives) == 1 and archives[0].read_bytes() == before
    assert archives[0].stat().st_mode & 0o777 == 0o600
    assert not service.journal.exists()
    assert calls == [
        ["systemctl", "disable", "--now", "bird.service"],
        ["systemctl", "daemon-reload"],
    ]


@pytest.mark.parametrize(
    "fault", ["runtime-unit", "runtime-dropin", "linked-directory", "vendor-dropin"]
)
def test_unsupported_overrides_block_before_stopping_existing_daemon(original, fault):
    service, dropin, calls = original
    if fault.startswith("runtime-"):
        path = (
            service.root
            / "run/systemd/system"
            / ("bird.service" if fault == "runtime-unit" else "bird.service.d")
        )
        path.parent.mkdir(parents=True)
        path.touch()
    elif fault == "vendor-dropin":
        path = service.root / "usr/lib/systemd/system/bird.service.d/extra.conf"
        path.parent.mkdir(parents=True)
        path.touch()
    else:
        dropin.unlink()
        dropin.parent.rmdir()
        dropin.parent.symlink_to(service.directory, target_is_directory=True)
    with pytest.raises(RuntimeErrorBase):
        service.install(_unit(sys.executable))
    assert not calls
    assert not service.journal.exists()


@pytest.mark.parametrize("fault", ["owner", "permissions", "path"])
def test_corrupt_snapshot_never_restores_other_units_or_paths(original, fault):
    service, _, calls = original
    service.install(_unit(sys.executable))
    record = json.loads(service.journal.read_text())
    if fault == "owner":
        record["owner"] = "other-unit"
    elif fault == "path":
        record["files"][0]["path"] = "etc/systemd/system/unrelated.service"
    service.journal.write_text(json.dumps(record))
    if fault == "permissions":
        service.journal.chmod(0o644)
    with pytest.raises(RuntimeErrorBase):
        service.rollback()
    assert not (service.root / "etc/systemd/system/unrelated.service").exists()
    assert ["systemctl", "start", "bird.service"] not in calls


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=24))
def test_startup_entrypoint_remains_in_supplied_charm_environment(segment):
    # Vary the deployment path: the service must not depend on a developer's
    # checkout, import search path, or a hand-maintained copy under /etc.
    python = f"/var/lib/juju/agents/unit-{segment}/charm/venv/bin/python"
    text = _unit(python)
    assert f'ExecStartPre=+"{python}" -B -m maas_bird.external_policy' in text
    assert "/etc/maas-bird" not in text
    assert "PYTHONPATH" not in text


def test_failed_reload_is_retried_even_when_unit_bytes_already_match(original, monkeypatch):
    service, _, _ = original
    service.install(_unit(sys.executable))
    service.commit()
    execute = system_service.run
    reloads = []

    def fail_once(args, check=True):
        if args == ["systemctl", "daemon-reload"]:
            reloads.append(True)
            if len(reloads) == 1:
                raise RuntimeErrorBase("reload failed")
        return execute(args, check)

    monkeypatch.setattr(system_service, "run", fail_once)
    with pytest.raises(RuntimeErrorBase, match="reload failed"):
        service.install(_unit("/opt/refreshed/venv/bin/python"))
    assert service.reload_pending.exists()
    service.install(_unit("/opt/refreshed/venv/bin/python"))
    assert reloads == [True, True]
    assert not service.reload_pending.exists()


def test_no_installed_vendor_unit_is_a_supported_first_install(original, monkeypatch):
    service, dropin, _ = original
    service.unit.unlink()
    dropin.unlink()

    def run(args, check=True):
        if args[1] == "is-enabled":
            return subprocess.CompletedProcess(args, 4, "", "Unit not found")
        if args[1] == "is-active":
            return subprocess.CompletedProcess(args, 4, "unknown\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(system_service, "run", run)
    service.install(_unit(sys.executable))
    assert json.loads(service.journal.read_text())["enabled"] == "not-found"
    assert service.unit.exists()
