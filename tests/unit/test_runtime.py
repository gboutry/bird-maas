import subprocess
from unittest.mock import Mock

import pytest
from maas_bird import runtime


def protocols(extended=True, established=True):
    state = "Established" if established else "Active"
    capability = "Extended next hop" if extended else ""
    return f"""BIRD 2.14 ready.
uplink_0 BGP --- up 12:00:00 {state}
  Neighbor capabilities
    {capability}
      IPv6 nexthop: ipv4
  Session: external
  Channel ipv4
    State: UP
"""


def test_acceptance_requires_negotiation_and_bfd():
    bfd = "fe80::1 fabric1 Up 12:00:00 0.300 0.900"
    assert runtime.accepted(protocols(), bfd, 1)
    assert not runtime.accepted(protocols(extended=False), bfd, 1)
    assert not runtime.accepted(protocols(established=False), bfd, 1)
    assert not runtime.accepted(protocols(), bfd.replace("Up", "Down"), 1)
    assert not runtime.accepted(protocols(), bfd, 2)


def test_ownership_guard_does_not_adopt_existing_directory(tmp_path):
    service = runtime.BirdService(tmp_path, owner="model:bird/0")
    service.directory.mkdir(parents=True)
    with pytest.raises(runtime.RuntimeErrorBase, match="adopt"):
        service._prepare(3100)
    assert not service.marker.exists()


def test_second_unit_cannot_control_first(tmp_path):
    first = runtime.BirdService(tmp_path, owner="model:bird/0")
    first.directory.mkdir(parents=True)
    first.marker.write_text("model:bird/0\n")
    second = runtime.BirdService(tmp_path, owner="model:bird/1")
    with pytest.raises(runtime.RuntimeErrorBase, match="ownership"):
        second._prepare(3100)
    second.stop()  # Must not stop the other unit's service.
    assert first.marker.read_text() == "model:bird/0\n"


def test_existing_table_cannot_be_repurposed(tmp_path):
    service = runtime.BirdService(tmp_path)
    service.directory.mkdir(parents=True)
    service.marker.write_text("development\n")
    (service.directory / "kernel-table").write_text("3100\n")
    with pytest.raises(runtime.RuntimeErrorBase, match="kernel table"):
        service._prepare(3101)


def test_reconfigure_is_native_confirmed_and_atomic(tmp_path, monkeypatch):
    service = runtime.BirdService(tmp_path)
    service.directory.mkdir(parents=True)
    service.config.write_text("previous")
    monkeypatch.setattr(service, "_prepare", lambda _: None)
    monkeypatch.setattr(runtime.os, "chown", lambda *args: None)
    monkeypatch.setattr(runtime.grp, "getgrnam", lambda _: Mock(gr_gid=100))
    control = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(service, "_control", control)
    monkeypatch.setattr(service, "established", lambda _: True)
    run = Mock()
    monkeypatch.setattr(runtime, "run", run)
    service.apply("candidate", 3100, 2)
    assert run.call_args.args[0][:3] == ["bird", "-p", "-c"]
    assert control.call_args_list[0].args == (
        "configure",
        '"' + str(service.directory / "candidate.conf") + '"',
        "timeout",
        "60",
    )
    assert control.call_args_list[1].args == ("configure", "confirm")
    assert service.config.read_text() == "candidate"


def test_failed_acceptance_undoes_without_replacing_config(tmp_path, monkeypatch):
    service = runtime.BirdService(tmp_path)
    service.directory.mkdir(parents=True)
    service.config.write_text("previous")
    monkeypatch.setattr(service, "_prepare", lambda _: None)
    monkeypatch.setattr(runtime.os, "chown", lambda *args: None)
    monkeypatch.setattr(runtime.grp, "getgrnam", lambda _: Mock(gr_gid=100))
    control = Mock()
    monkeypatch.setattr(service, "_control", control)
    monkeypatch.setattr(service, "established", lambda _: False)
    monkeypatch.setattr(runtime, "run", Mock())
    monkeypatch.setattr(runtime.time, "sleep", lambda _: None)
    with pytest.raises(runtime.SessionsPending, match="retained"):
        service.apply("candidate", 3100, 2)
    assert control.call_args.args == ("configure", "undo")
    assert service.config.read_text() == "previous"
    assert not (service.directory / "candidate.conf").exists()


def test_parse_failure_cannot_replace_previous_config(tmp_path, monkeypatch):
    service = runtime.BirdService(tmp_path)
    service.directory.mkdir(parents=True)
    service.config.write_text("previous")
    monkeypatch.setattr(service, "_prepare", lambda _: None)
    monkeypatch.setattr(runtime.os, "chown", lambda *args: None)
    monkeypatch.setattr(runtime.grp, "getgrnam", lambda _: Mock(gr_gid=100))
    monkeypatch.setattr(runtime, "run", Mock(side_effect=runtime.RuntimeErrorBase("parse failed")))
    with pytest.raises(runtime.RuntimeErrorBase):
        service.apply("invalid candidate", 3100, 2)
    assert service.config.read_text() == "previous"


def test_runtime_errors_do_not_include_output(monkeypatch):
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "password", "secret"),
    )
    with pytest.raises(runtime.RuntimeErrorBase) as exc:
        runtime.run(["bird", "-p"])
    assert "password" not in str(exc.value)
    assert "secret" not in str(exc.value)


def test_fresh_table_check_uses_all_tables(tmp_path, monkeypatch):
    service = runtime.BirdService(tmp_path)
    calls = []

    def run(args, check=True):
        calls.append(args)
        if args[0] == "ip":
            return subprocess.CompletedProcess(args, 0, '[{"table":3100}]', "")
        return subprocess.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(runtime, "run", run)
    with pytest.raises(runtime.RuntimeErrorBase, match="already populated"):
        service._prepare(3100)
    assert ["ip", "-j", "-N", "route", "show", "table", "all"] in calls
    assert not service.marker.exists()
