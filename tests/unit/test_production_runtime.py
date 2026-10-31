"""Production daemon primitives: parser contracts and real filesystem failures.

CLI results are doubles here; these tests do not claim BIRD or systemd ran. File
replacement and permission checks use real disposable files, with failures at
specific durability boundaries rather than a mock of the whole writer.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest
from maas_bird import runtime


@pytest.fixture
def daemon():
    """Bind these assertions to the supported production implementation."""
    return runtime


def protocol():
    return """BIRD 2.14 ready.
uplink_0 BGP --- up 12:00:00 Established
  Neighbor capabilities
    Extended next hop
      IPv6 nexthop: ipv4
  Session: external
  Channel ipv4
    State: UP
"""


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("Established", "Active"),
        ("Neighbor capabilities", "Local capabilities"),
        ("Extended next hop", "Route refresh"),
        ("IPv6 nexthop: ipv4", "IPv6 nexthop: ipv6"),
        ("Channel ipv4", "Channel ipv6"),
        ("State: UP", "State: DOWN"),
    ],
)
def test_each_negotiation_requirement_is_required(daemon, old, new):
    healthy = protocol()
    bfd = "fe80::1 fabric1 Up 12:00:00 0.300 0.900"
    assert daemon.accepted(healthy, bfd, 1)
    assert not daemon.accepted(healthy.replace(old, new), bfd, 1)


@pytest.mark.parametrize("bfd", ["", "fe80::1 fabric1 Down 0.300", "fe80::1"])
def test_missing_down_or_truncated_bfd_does_not_authorize(daemon, bfd):
    assert not daemon.accepted(protocol(), bfd, 1)


def test_pipe_channel_cannot_supply_missing_bgp_capability(daemon):
    # The healthy-looking Pipe is a different protocol. Its channel/capability
    # output must not make the BGP block acceptable after BGP lost that evidence.
    text = protocol().replace("Channel ipv4\n    State: UP", "")
    text += """path_0 Pipe --- up 12:00:00
  Channel ipv4
    State: UP
"""
    assert not daemon.accepted(text, "fe80::1 fabric1 Up 0.300", 1)


def test_exact_bgp_and_bfd_counts_are_required(daemon):
    text = protocol()
    bfd = "fe80::1 fabric1 Up 0.300"
    assert not daemon.accepted(text, bfd, 2)
    assert not daemon.accepted(text + text.replace("uplink_0", "uplink_1"), bfd, 2)
    assert not daemon.accepted(text, bfd + "\nfe80::2 fabric2 Up 0.300", 1)


@pytest.mark.parametrize("checked", [False, True])
def test_success_returns_exact_native_result_with_bounded_capture(daemon, monkeypatch, checked):
    result = subprocess.CompletedProcess(["birdc"], 0, "native stdout", "native stderr")
    calls = []

    def execute(args, **options):
        calls.append((args, options))
        return result

    monkeypatch.setattr(daemon.subprocess, "run", execute)
    assert daemon.run(["birdc", "show", "status"], check=checked) is result
    assert calls == [
        (
            ["birdc", "show", "status"],
            dict(capture_output=True, text=True, timeout=30, check=False),
        )
    ]


def test_unchecked_nonzero_is_returned_but_checked_failure_is_sanitized(daemon, monkeypatch):
    result = subprocess.CompletedProcess(["birdc"], 7, "PRIVATE stdout", "PRIVATE stderr")
    monkeypatch.setattr(daemon.subprocess, "run", lambda *args, **kw: result)
    assert daemon.run(["birdc", "private-argument"], check=False) is result
    with pytest.raises(runtime.RuntimeErrorBase) as error:
        daemon.run(["birdc", "private-argument"])
    assert str(error.value) == "Workload command failed: birdc"


@pytest.mark.parametrize(
    "failure",
    [
        OSError("PRIVATE filesystem details"),
        subprocess.TimeoutExpired(["birdc", "PRIVATE argument"], 30, "PRIVATE stdout"),
    ],
)
def test_native_execution_errors_are_sanitized_and_preserve_cause(daemon, monkeypatch, failure):
    def execute(*args, **kw):
        raise failure

    monkeypatch.setattr(daemon.subprocess, "run", execute)
    with pytest.raises(runtime.RuntimeErrorBase) as error:
        daemon.run(["birdc", "PRIVATE argument"])
    assert str(error.value) == "Workload command failed: birdc"
    assert error.value.__cause__ is failure


@pytest.mark.parametrize("failure_point", ["file-fsync", "replace", "directory-fsync"])
def test_atomic_write_failure_leaves_one_complete_generation(tmp_path, monkeypatch, failure_point):
    path = tmp_path / "state.json"
    previous, proposed = '{"generation":"old"}\n', '{"generation":"new"}\n'
    path.write_text(previous)
    original_fsync, original_replace = runtime.os.fsync, Path.replace

    def sync(fd):
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if failure_point == ("directory-fsync" if directory else "file-fsync"):
            raise OSError("injected fsync failure")
        original_fsync(fd)

    def replace(source, target):
        if failure_point == "replace":
            raise OSError("injected replacement failure")
        return original_replace(source, target)

    monkeypatch.setattr(runtime.os, "fsync", sync)
    monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(OSError, match="injected"):
        runtime.write_atomic(path, proposed, 0o600)
    # A failed directory fsync happens after the rename: bytes are complete, but
    # the exception must propagate because crash durability was not established.
    assert path.read_text() == (proposed if failure_point == "directory-fsync" else previous)
    assert not path.with_name("state.json.tmp").exists()


def test_atomic_write_replaces_generation_and_applies_exact_permissions(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("previous")
    runtime.write_atomic(path, "complete replacement", 0o640)
    assert path.read_text() == "complete replacement"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert not path.with_name("state.json.tmp").exists()


def test_atomic_write_refuses_temporary_symlink_without_touching_victim(tmp_path):
    path = tmp_path / "state.json"
    victim = tmp_path / "unrelated-secret"
    path.write_text("accepted generation")
    victim.write_text("must remain unchanged")
    temporary = path.with_name("state.json.tmp")
    temporary.symlink_to(victim)
    with pytest.raises(OSError):
        runtime.write_atomic(path, "candidate", 0o600)
    assert victim.read_text() == "must remain unchanged"
    assert path.read_text() == "accepted generation"
    assert temporary.is_symlink()  # Opening failed before the writer acquired it.
