"""Ceph policy fixtures and actual no-network lease CLI cancellation checks."""

import copy
import io
import json
import os
import runpy
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts/network_benchmark_ceph.py"
LEASE = ROOT / "scripts/network_benchmark_lease.py"


@pytest.fixture
def module():
    return runpy.run_path(str(GUARD))


def snapshot(t=0, n=0, milliseconds=1):
    health = {"status": "HEALTH_OK", "checks": {}, "mutes": []}
    return {
        "started": t,
        "ended": t + 0.1,
        "health": health,
        "status": {
            "fsid": "test",
            "health": copy.deepcopy(health),
            "quorum_names": ["a", "b", "c"],
            "monmap": {"num_mons": 3},
            "osdmap": {"num_osds": 3, "num_up_osds": 2, "num_in_osds": 2, "num_remapped_pgs": 0},
            "pgmap": {
                "num_pgs": 100,
                "num_pools": 2,
                "pgs_by_state": [{"state_name": "active+clean", "count": 100}],
            },
        },
        "tree": {
            "nodes": [
                {"id": 52, "type": "osd", "status": "up", "reweight": 1},
                {"id": 95, "type": "osd", "status": "down", "reweight": 0},
            ]
        },
        "osds": {
            "52": {
                "process": [123, "456"],
                "perf": {
                    "op_wip": 0,
                    **{
                        metric: {"avgcount": n, "sum": n * milliseconds / 1000}
                        for metric in (
                            "op_latency",
                            "op_r_latency",
                            "op_w_latency",
                            "op_rw_latency",
                            "subop_latency",
                        )
                    },
                },
            }
        },
    }


def ready(module):
    guard = module["Guard"](snapshot()["health"], [52], minimum_seconds=10, minimum_intervals=2)
    for t, n in ((0, 0), (5, 10), (10, 20)):
        guard.check(snapshot(t, n), now=t + 0.2)
    guard.finish_baseline()
    return guard


def test_baseline_requires_time_activity_and_fresh_permission(module):
    guard = module["Guard"](snapshot()["health"], [52])
    guard.check(snapshot(), now=0.2)
    with pytest.raises(ValueError, match="completed healthy baseline"):
        guard.permit(now=0.2)
    with pytest.raises(ValueError, match="short"):
        guard.finish_baseline()
    # Idle intervals do not manufacture a latency baseline.
    for t in range(5, 305, 5):
        result = guard.check(snapshot(t), now=t + 0.2)
        assert result["interval_mean_ms"]["52/op_latency"] is None
    with pytest.raises(ValueError, match="insufficient"):
        guard.finish_baseline()
    guard = ready(module)
    guard.permit(now=10.2)
    with pytest.raises(ValueError, match="expired"):
        guard.permit(now=21)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda s: s["health"]["checks"].update(SLOW_OPS={"summary": {"count": 1}}),
        lambda s: s["status"]["quorum_names"].pop(),
        lambda s: s["tree"]["nodes"][0].update(status="down"),
        lambda s: s["tree"]["nodes"][0].update(reweight=0),
        lambda s: s["status"]["osdmap"].update(num_remapped_pgs=1),
        lambda s: s["status"]["pgmap"]["pgs_by_state"][0].update(state_name="active+degraded"),
        lambda s: s["status"]["pgmap"].update(num_pools=3),
        lambda s: s["status"]["osdmap"].update(num_up_osds=1),
        lambda s: s["osds"]["52"].update(process=[123, "999"]),
        lambda s: s["osds"].clear(),
        lambda s: s["osds"]["52"]["perf"]["op_latency"].update(avgcount=0),
        lambda s: s["osds"]["52"]["perf"]["op_latency"].update(sum=0),
        lambda s: s["osds"]["52"]["perf"]["op_latency"].update(sum=float("nan")),
        lambda s: s["osds"]["52"]["perf"]["op_latency"].update(avgcount=True),
        lambda s: s["osds"]["52"]["perf"].pop("op_w_latency"),
        lambda s: s.update(started=1),
        lambda s: s.update(ended=30),
    ],
)
def test_failures_latch_and_prohibit_subsequent_traffic(module, mutation):
    guard = ready(module)
    sample = snapshot(15, 30)
    mutation(sample)
    with pytest.raises(ValueError):
        guard.check(sample, now=15.2)
    with pytest.raises(ValueError):
        guard.check(snapshot(20, 40), now=20.2)
    with pytest.raises(ValueError):
        guard.permit(now=20.2)


def test_existing_warning_requires_exact_acceptance(module):
    sample = snapshot()
    health = {
        "status": "HEALTH_WARN",
        "checks": {
            "RECENT_CRASH": {
                "severity": "HEALTH_WARN",
                "summary": {"count": 1, "message": "old crash"},
                "detail": [{"message": "osd.95 old timestamp"}],
                "muted": False,
            }
        },
        "mutes": [],
    }
    sample["health"] = health
    sample["status"]["health"] = copy.deepcopy(health)
    sample["status"]["health"]["checks"]["RECENT_CRASH"].pop("detail")
    guard = module["Guard"](copy.deepcopy(health), [52])
    guard.check(sample, now=0.2)
    sample["started"], sample["ended"] = 5, 5.1
    sample["health"]["checks"]["RECENT_CRASH"]["detail"][0]["message"] = "new timestamp"
    with pytest.raises(ValueError, match="Unexpected Ceph health"):
        guard.check(sample, now=5.2)


def test_interval_latency_floor_two_strikes_and_hard_stop(module):
    guard = ready(module)
    s = snapshot(15, 30)
    s["osds"]["52"]["perf"]["op_latency"]["sum"] = 0.32  # (0.32-0.02)/10 = 30ms
    assert guard.check(s, now=15.2)["interval_mean_ms"]["52/op_latency"] == 30
    s = snapshot(20, 40)
    s["osds"]["52"]["perf"]["op_latency"]["sum"] = 0.62
    with pytest.raises(ValueError, match="two interval means"):
        guard.check(s, now=20.2)
    guard = ready(module)
    s = snapshot(15, 30)
    s["osds"]["52"]["perf"]["op_latency"]["sum"] = 1.02
    with pytest.raises(ValueError, match=">=100"):
        guard.check(s, now=15.2)


def test_inflight_with_no_completions_stops_without_claiming_zero_latency(module):
    guard = ready(module)
    for t in (15, 20):
        s = snapshot(t, 20)
        s["osds"]["52"]["perf"]["op_wip"] = 1
        if t == 15:
            assert guard.check(s, now=t + 0.2)["interval_mean_ms"]["52/op_latency"] is None
        else:
            with pytest.raises(ValueError, match="without completions"):
                guard.check(s, now=t + 0.2)


def test_supervisor_cancels_and_never_renews_after_failed_read(module, monkeypatch):
    guard = ready(module)
    monkeypatch.setattr(time, "monotonic", lambda: 10.2)
    calls = []

    def failed():
        raise TimeoutError("monitor unavailable")

    with pytest.raises(TimeoutError):
        module["supervise"](
            guard,
            failed,
            lambda: calls.append("renew"),
            lambda: calls.append("cancel"),
            lambda: False,
            calls.append,
        )
    assert calls[-1] == "cancel" and "renew" not in calls
    assert guard.tripped == "monitor unavailable"


def test_collector_audits_before_exec_and_rejects_failed_ssh(module, monkeypatch):
    records = []

    def failed(argv, **kwargs):
        assert any(r.get("argv") == argv for r in records)
        return subprocess.CompletedProcess(argv, 255, "", "unreachable")

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(ValueError, match="read failed"):
        module["collect"]({"monitor": "ubuntu@monitor", "hosts": []}, records.append)
    assert len(records) == 6


def test_sparse_osd_needs_explicit_fixed_floor_exception(module):
    guard = module["Guard"](
        snapshot()["health"], [52], minimum_seconds=10, minimum_intervals=2, fixed_floor_osds=[52]
    )
    for t in (0, 5, 10):
        guard.check(snapshot(t), now=t + 0.2)
    baseline = guard.finish_baseline()
    assert baseline["explicit_fixed_floor_osds"] == ["52"]
    assert set(baseline["threshold_ms"].values()) == {20}
    # The first new completion is guarded immediately; no baseline latency was invented.
    with pytest.raises(ValueError, match=">=100"):
        guard.check(snapshot(15, 1, milliseconds=101), now=15.2)
    guard = module["Guard"](snapshot()["health"], [52], minimum_seconds=10, minimum_intervals=2)
    for t, n in ((0, 0), (5, 0), (10, 1)):
        guard.check(snapshot(t, n), now=t + 0.2)
    with pytest.raises(ValueError, match="insufficient"):
        guard.finish_baseline()


def test_baseline_relative_limit_uses_p95_of_interval_means(module):
    guard = module["Guard"](snapshot()["health"], [52], minimum_seconds=10, minimum_intervals=2)
    for t, n in ((0, 0), (5, 10), (10, 20)):
        guard.check(snapshot(t, n, milliseconds=10), now=t + 0.2)
    result = guard.finish_baseline()
    assert set(result["threshold_ms"].values()) == {30}


def test_collector_rejects_ssh_option_target_before_any_command(module):
    records = []
    with pytest.raises(ValueError, match="Invalid SSH target"):
        module["collect"]({"monitor": "-oProxyCommand=touch bad", "hosts": []}, records.append)
    assert not records


def read_event(process, seconds=3):
    # Unbuffered binary streams prevent a second JSON event hiding in TextIO's buffer.
    assert select.select([process.stdout], [], [], seconds)[0], "missing lease event"
    line = b""
    while not line.endswith(b"\n"):
        line += os.read(process.stdout.fileno(), 1)
    return json.loads(line)


def start_lease(code="import time;time.sleep(30)", lease=0.4, maximum=3):
    process = subprocess.Popen(
        [sys.executable, str(LEASE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    request = {
        "machine_id": Path("/etc/machine-id").read_text().strip(),
        "lease_seconds": lease,
        "maximum_seconds": maximum,
        "argv": [sys.executable, "-c", code],
    }
    assert process.stdin is not None
    process.stdin.write((json.dumps(request) + "\n").encode())
    assert read_event(process)["event"] == "command"
    event = read_event(process)
    assert event["event"] == "started"
    return process, event["pid"]


@pytest.mark.parametrize(
    "action,expected",
    [
        ("expiry", "lease expired"),
        ("eof", "lease input closed"),
        ("cancel", "cancelled"),
        ("bad", "invalid lease"),
    ],
)
def test_real_cli_cancels_owned_process_without_network(action, expected):
    process, child = start_lease()
    try:
        if action == "eof":
            process.stdin.close()
        elif action in {"cancel", "bad"}:
            process.stdin.write((json.dumps({"action": action}) + "\n").encode())
        result = read_event(process)
        assert result["reason"] == expected
        assert process.wait(timeout=3) == 1
        assert not Path(f"/proc/{child}").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_real_cli_renews_then_cancels_sigterm_resistant_process():
    process, child = start_lease(
        "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)"
    )
    try:
        for _ in range(4):
            time.sleep(0.15)
            process.stdin.write(b'{"action":"renew"}\n')
        assert Path(f"/proc/{child}").exists()
        process.stdin.write(b'{"action":"cancel"}\n')
        result = read_event(process)
        assert result["returncode"] == -signal.SIGKILL
        assert process.wait(timeout=3) == 1
        assert not Path(f"/proc/{child}").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_real_cli_watchdog_cannot_be_extended_by_renewals():
    process, child = start_lease(lease=1, maximum=0.4)
    try:
        process.stdin.write(b'{"action":"renew"}\n')
        result = read_event(process)
        assert result["reason"] == "watchdog expired"
        assert process.wait(timeout=3) == 1
        assert not Path(f"/proc/{child}").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_real_cli_cleans_descendants_after_group_leader_exits(tmp_path):
    child_file = tmp_path / "child.pid"
    code = (
        "import subprocess,sys;from pathlib import Path;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
        f"Path({str(child_file)!r}).write_text(str(p.pid))"
    )
    process, _ = start_lease(code)
    try:
        result = read_event(process)
        assert result["reason"] == "completed"
        assert process.wait(timeout=3) == 0
        path = Path("/proc") / child_file.read_text() / "stat"
        # A killed orphan can remain a zombie until init reaps it; it cannot run.
        assert not path.exists() or path.read_text().rsplit(") ", 1)[1].split()[0] == "Z"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_actual_guard_cli_refuses_reads_without_explicit_flag(tmp_path):
    config = tmp_path / "config.json"
    config.write_text("{}")
    result = subprocess.run(
        [sys.executable, str(GUARD), str(config)], capture_output=True, text=True, timeout=5
    )
    assert result.returncode != 0 and "--execute-read-only required" in result.stderr


def test_supervisor_failure_cancels_real_leased_child(module, monkeypatch):
    process, child = start_lease(lease=1)
    guard = ready(module)
    monkeypatch.setattr(time, "monotonic", lambda: 15.2)
    calls = []

    def sample():
        if calls:
            raise TimeoutError("lost Ceph read after renewal")
        return snapshot(15, 30)

    def renew():
        process.stdin.write(b'{"action":"renew"}\n')
        calls.append("renewed")

    def cancel():
        process.stdin.write(b'{"action":"cancel"}\n')
        assert read_event(process)["reason"] == "cancelled"
        assert process.wait(timeout=3) == 1
        calls.append("cancelled")

    try:
        with pytest.raises(TimeoutError):
            module["supervise"](
                guard, sample, renew, cancel, lambda: False, lambda r: None, period=0
            )
        assert calls == ["renewed", "cancelled"]
        assert not Path(f"/proc/{child}").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_actual_readonly_collector_cli_with_fixture_ssh(tmp_path, monkeypatch):
    data = snapshot()
    fake = tmp_path / "ssh"
    fake.write_text(
        f"#!{sys.executable}\nimport json,sys\n"
        f"data={data!r}\n"
        "cmd=sys.argv[-1]\n"
        "key='health' if 'health detail' in cmd else 'tree' if 'osd tree' in cmd else 'status'\n"
        "print(json.dumps(data['osds'] if 'ubuntu@osd' in sys.argv else data[key]))\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "monitor": "ubuntu@monitor",
                "hosts": [{"ssh": "ubuntu@osd", "machine_id": "a" * 32, "osds": [52]}],
            }
        )
    )
    result = subprocess.run(
        [sys.executable, str(GUARD), str(config), "--execute-read-only"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)
    assert record["snapshot"]["osds"] == data["osds"]
    assert len(record["commands"]) == 8


def test_inbox_preserves_prefetched_lines():
    module = runpy.run_path(str(LEASE))
    # Both messages arrive in one read; the second is available without another select.
    read_fd, write_fd = os.pipe()
    with io.FileIO(read_fd, "r") as stream, io.FileIO(write_fd, "w") as writer:
        writer.write(b'{"action":"renew"}\n{"action":"cancel"}\n')
        inbox = module["Inbox"](stream.fileno())
        assert inbox.read(0) == {"action": "renew"}
        assert inbox.read(0) == {"action": "cancel"}
