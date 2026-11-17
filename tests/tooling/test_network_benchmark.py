"""Offline CLI and fixture-agent checks; never contact hardware or generate traffic."""

import copy
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "scripts/network_benchmark.py"
AGENT = ROOT / "scripts/network_benchmark_agent.py"
EXAMPLE = ROOT / "examples/network-benchmark/manifest.json"


@pytest.fixture
def controller():
    return runpy.run_path(str(CLI))


@pytest.fixture
def manifest(tmp_path):
    config = json.loads(EXAMPLE.read_text())
    config.update(duration=10, warmup=1, idle=2, repeats=1)
    config["cases"] = config["cases"][:1]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(config))
    return path


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    """Substitute transport at the actual CLI boundary, including long-lived sessions."""
    binary = tmp_path / "ssh"
    binary.write_text("""#!/usr/bin/env python3
import json, os, select, sys, time
from pathlib import Path
request = json.loads(sys.stdin.readline())
op = request["operation"]
with open(os.environ["BENCH_TEST_CALLS"], "a") as log:
    log.write(json.dumps(request) + "\\n")
def emit(value):
    print(json.dumps(value), flush=True)
if op == "capture":
    emit({"uuid": request["uuid"], "commands": {}, "files": {}})
elif op == "check":
    if os.environ.get("BENCH_TEST_FAIL") == "check":
        emit({"error": "VLAN mismatch"})
        sys.exit(1)
    emit({"attachment": request["attachment"]})
elif op == "ports":
    emit({"returncode": 0, "stdout": "", "stderr": ""})
elif op == "clock":
    emit({"time": time.time()})
elif op == "sample":
    emit({"ready": True, "time": time.time()})
    epoch = time.time()
    for second in range(15):
        emit({"time": epoch + second, "monotonic": 100 + second, "clock_ticks": 100,
              "files": {"/proc/stat": {"value": f"cpu0 {second*10} 0 0 {second*90} 0 0 0 0\\n"}},
              "processes": {}})
    # Fill well beyond pipe capacity: controller must stream directly to evidence files.
    emit({"padding": "x" * 100000})
    sys.stdin.readline()
elif op == "server":
    if os.environ.get("BENCH_TEST_FAIL") == "server":
        emit({"error": "port occupied"})
        sys.exit(1)
    emit({"ready": True, "time": time.time()})
    marker = Path(os.environ["BENCH_TEST_DONE"])
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        if select.select([sys.stdin], [], [], .01)[0]:
            break
    emit({"returncode": 0, "stdout": '{"end": {}}', "stderr": ""})
elif op == "client":
    Path(os.environ["BENCH_TEST_DONE"]).touch()
    failed = os.environ.get("BENCH_TEST_FAIL") == "client"
    emit({"started": time.time(), "traffic": {
        "returncode": 1 if failed else 0,
        "stdout": json.dumps({"end": {"sum_received": {"bits_per_second": 1000000000}},
          "intervals": [{"streams": [
            {"sender": True, "end": 1, "bits_per_second": 1000000000}]}]}),
        "stderr": "fixture failure" if failed else ""},
        "latency": {"returncode": 0, "stdout": f"[{time.time()+2}] reply time=0.125 ms\\n"}})
else:
    raise ValueError(op)
""")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("BENCH_TEST_CALLS", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("BENCH_TEST_DONE", str(tmp_path / "done"))
    return tmp_path / "calls.jsonl"


def cli(*args):
    return subprocess.run(
        [sys.executable, str(CLI), *map(str, args)], text=True, capture_output=True, timeout=40
    )


def test_plan_is_offline_and_alternates(controller, manifest, fake_ssh):
    result = cli("plan", manifest)
    assert result.returncode == 0, result.stderr
    assert [j["mode"] for j in json.loads(result.stdout)["jobs"]] == ["pf", "vf"]
    assert not fake_ssh.exists()
    config = controller["load"](manifest)
    config["repeats"] = 2
    assert [j["mode"] for j in controller["jobs"](config)] == ["pf", "vf", "vf", "pf"]


def test_no_execute_has_no_ssh_or_output(manifest, fake_ssh, tmp_path):
    output = tmp_path / "evidence"
    result = cli("run", manifest, "--mode", "vf", "--output", output)
    assert result.returncode != 0
    assert "--execute required" in result.stderr
    assert not output.exists() and not fake_ssh.exists()


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda c: c["nodes"]["host-a"].update(ssh="-oProxyCommand=bad"), "SSH target"),
        (lambda c: c["modes"]["vf"]["a"].update(vlan=200), "identical"),
        (lambda c: c["modes"]["vf"]["a"].update(kind="bond"), "kind"),
        (lambda c: c["modes"]["vf"]["a"].update(parent="vf0;id"), "interface"),
        (lambda c: c["cases"][0].update(rate_bps=7), "offered rate"),
        (lambda c: c["endpoints"]["a"].update(arms=[]), "Arm"),
    ],
)
def test_reject_invalid_manifest(controller, manifest, mutation, message):
    config = json.loads(manifest.read_text())
    mutation(config)
    manifest.write_text(json.dumps(config))
    with pytest.raises(ValueError, match=message):
        controller["load"](manifest)


def test_commands_use_aggregate_rate_and_explicit_bind(controller, manifest):
    config = controller["load"](manifest)
    job = controller["jobs"](config)[1]
    src, dst, argv = controller["iperf_argv"](config, job)
    assert (src, dst) == ("a", "b")
    assert argv[argv.index("--bind-dev") + 1] == "vf0.100"
    assert argv[argv.index("-b") + 1] == "1000000000"
    job["case"].update(transport="udp", payload_bytes=8972, direction="bidir")
    _, _, argv = controller["iperf_argv"](config, job)
    assert "--bidir" in argv and "--dont-fragment" in argv
    assert argv[argv.index("-l") + 1] == "8972"
    job["case"].update(transport="tcp", rate_bps="maximum", direction="b-a")
    src, dst, argv = controller["iperf_argv"](config, job)
    assert (src, dst) == ("b", "a") and "-b" not in argv


def test_cli_capture_keeps_nodes_and_refuses_overwrite(manifest, fake_ssh, tmp_path):
    output = tmp_path / "capture"
    result = cli("capture", manifest, "--execute", "--output", output)
    assert result.returncode == 0, result.stderr
    assert len(list(output.glob("state-*.json"))) == 4
    assert {json.loads(line)["operation"] for line in fake_ssh.read_text().splitlines()} == {
        "capture"
    }
    before = fake_ssh.read_bytes()
    assert cli("capture", manifest, "--execute", "--output", output).returncode != 0
    assert fake_ssh.read_bytes() == before


def test_cli_run_collects_both_hosts_and_arms(manifest, fake_ssh, tmp_path):
    output = tmp_path / "run"
    result = cli("run", manifest, "--mode", "vf", "--execute", "--output", output)
    assert result.returncode == 0, result.stderr
    trial = output / "r01-c01-vf"
    assert json.loads((trial / "result.json").read_text())["complete"]
    assert len(list(trial.glob("samples-*.jsonl"))) == 4
    assert all(p.stat().st_size > 100000 for p in trial.glob("samples-*.jsonl"))
    summary = json.loads((output / "summary.json").read_text())["trials"][0]
    assert summary["delivered_bps"] == 1000000000
    assert summary["icmp_rtt"]["p99_ms"] == 0.125
    assert {s["role"] for s in summary["resources"].values()} == {"host", "arm"}
    calls = [json.loads(line) for line in fake_ssh.read_text().splitlines()]
    assert {c["attachment"]["kind"] for c in calls if c["operation"] == "check"} == {"vf"}
    assert len([c for c in calls if c["operation"] == "client"]) == 1
    assert {c["port"] for c in calls if c["operation"] == "sample"} == {55201}
    assert json.loads((output / "report.json").read_text())["runs"][0]["charts"]


@pytest.mark.parametrize("failure", ["check", "server", "client"])
def test_cli_failure_stops_without_claiming_success(
    manifest, fake_ssh, tmp_path, monkeypatch, failure
):
    monkeypatch.setenv("BENCH_TEST_FAIL", failure)
    output = tmp_path / "failed"
    result = cli("run", manifest, "--mode", "pf", "--execute", "--output", output)
    assert result.returncode != 0
    assert not (output / "summary.json").exists()
    calls = [json.loads(line) for line in fake_ssh.read_text().splitlines()]
    if failure in {"check", "server"}:
        assert not any(c["operation"] == "client" for c in calls)
    if failure != "check":
        assert not json.loads((output / "r01-c01-pf/result.json").read_text())["complete"]


def test_cpu_cost_excludes_idle_iowait_and_guest_double_count(controller):
    samples = [
        {
            "time": 0,
            "monotonic": 10,
            "clock_ticks": 100,
            "files": {"/proc/stat": {"value": "cpu0 100 0 100 100 100 0 0 0 50 0"}},
        },
        {
            "time": 2,
            "monotonic": 12,
            "clock_ticks": 100,
            "files": {"/proc/stat": {"value": "cpu0 150 0 150 150 150 0 0 0 75 0"}},
        },
    ]
    result = controller["cpu_window"](samples, 0, 2)
    assert result["busy_cores"] == 0.5
    assert result["per_core"]["cpu0"]["busy_seconds"] == 1
    assert "unavailable" in controller["cpu_window"](samples, 0, 1)


def test_udp_search_distinguishes_loss_from_sender_limit(
    controller, manifest, monkeypatch, tmp_path
):
    config = controller["load"](manifest)
    job = controller["jobs"](config)[0]
    job["case"].update(
        transport="udp",
        rate_bps="search",
        payload_bytes=18,
        search={"min_bps": 800, "max_bps": 8000, "steps": 4},
    )
    rates = []

    def trial(config, probe, path):
        rate = probe["case"]["rate_bps"]
        rates.append(rate)
        return {
            "client": {
                "traffic": {
                    "stdout": json.dumps(
                        {
                            "end": {
                                "sum_sent": {"bits_per_second": rate},
                                "sum_received": {"packets": 100, "lost_packets": int(rate > 4000)},
                            }
                        }
                    )
                }
            }
        }

    monkeypatch.setitem(controller["run_search"].__globals__, "run_job", trial)
    controller["run_search"](config, job, tmp_path)
    result = json.loads((tmp_path / (job["id"] + "-search.json")).read_text())
    assert rates == [8000, 4000, 6000, 5000]
    assert result["best_observed_zero_loss_bps_per_direction"] == 4000
    assert result["state"] == "step_limit"


@pytest.fixture
def agent(monkeypatch, tmp_path):
    values = runpy.run_path(str(AGENT))
    fs = tmp_path / "fs"
    fs.mkdir()
    monkeypatch.setitem(values["check"].__globals__, "Path", lambda p: fs / str(p).lstrip("/"))
    return values, fs


def test_agent_identity_fails_closed(agent):
    values, fs = agent
    identity = fs / "etc/machine-id"
    identity.parent.mkdir(parents=True)
    identity.write_text("1234\n")
    assert values["identity"]("1234", "machine-id") == "1234"
    with pytest.raises(ValueError, match="UUID mismatch"):
        values["identity"]("wrong", "machine-id")


def test_agent_preserves_missing_command_and_timeout(agent):
    values, _ = agent
    missing = values["command"](["/nonexistent-benchmark-command"])
    assert "unavailable" in missing and "returncode" not in missing
    timeout = values["command"]([sys.executable, "-c", "import time; time.sleep(1)"], timeout=0.01)
    assert "unavailable" in timeout


@pytest.mark.parametrize(
    "failure", [None, "route", "vlan", "pci", "type", "numa", "cpu", "parent-conflict"]
)
@pytest.mark.parametrize("parent_format", ["index", "name"])
def test_agent_live_preflight_against_fixture_sysfs(
    agent, manifest, monkeypatch, failure, parent_format
):
    values, fs = agent
    config = json.loads(manifest.read_text())
    attachment = copy.deepcopy(config["modes"]["vf"]["a"])
    device = fs / "sys/class/net/vf0/device"
    device.parent.mkdir(parents=True)
    pci = fs / "sys/devices/0000:03:00.2"
    pci.mkdir(parents=True)
    (pci / "physfn").mkdir()
    (pci / "numa_node").write_text("0")
    device.symlink_to(pci)
    node = fs / "sys/devices/system/node/node0"
    node.mkdir(parents=True)
    (node / "cpulist").write_text("0-3")
    vlan = {
        "ifname": "vf0.100",
        "link_index": 5,
        "mtu": 9000,
        "flags": ["UP"],
        "linkinfo": {"info_kind": "vlan", "info_data": {"id": 100}},
    }
    if parent_format == "name":
        vlan.pop("link_index")
        vlan["link"] = "vf0"
    if failure == "parent-conflict":
        vlan["link_index"] = 5
        vlan["link"] = "different-parent"
    if failure == "vlan":
        vlan["linkinfo"]["info_data"]["id"] = 200
    if failure == "pci":
        attachment["pci"] = "0000:04:00.2"
    if failure == "type":
        (pci / "physfn").rmdir()
    if failure == "numa":
        (pci / "numa_node").write_text("1")
    monkeypatch.setitem(values["check"].__globals__, "identity", lambda *args: "fixture")
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0, 1, 2, 3, 4})

    def command(argv, timeout=15):
        if "link" in argv:
            out = [vlan, {"ifname": "vf0", "ifindex": 5, "mtu": 9000}]
        elif "address" in argv:
            out = [{"addr_info": [{"local": "192.0.2.10"}]}]
        elif "route" in argv:
            out = [{"dev": "management0" if failure == "route" else "vf0.100"}]
        else:
            return {"returncode": 0, "stdout": "fixture version"}
        return {"returncode": 0, "stdout": json.dumps(out)}

    monkeypatch.setitem(values["check"].__globals__, "command", command)
    request = {
        "uuid": "fixture",
        "attachment": attachment,
        "mtu": 9000,
        "peer": "192.0.2.20",
        "cpus": [4] if failure == "cpu" else [2, 3],
        "memory_node": 0,
    }
    if failure:
        with pytest.raises(ValueError):
            values["check"](request)
    else:
        assert values["check"](request)["attachment"] == attachment


@pytest.mark.parametrize("reason", ["missing", "sender"])
def test_udp_search_keeps_inconclusive_distinct(
    controller, manifest, monkeypatch, tmp_path, reason
):
    config = controller["load"](manifest)
    job = controller["jobs"](config)[0]
    job["case"].update(
        transport="udp",
        rate_bps="search",
        payload_bytes=18,
        search={"min_bps": 800, "max_bps": 8000, "steps": 4},
    )
    received = {} if reason == "missing" else {"packets": 100, "lost_packets": 0}

    def trial(config, probe, path):
        return {
            "client": {
                "traffic": {
                    "stdout": json.dumps(
                        {
                            "end": {
                                "sum_sent": {"bits_per_second": 100},
                                "sum_received": received,
                            }
                        }
                    )
                }
            }
        }

    monkeypatch.setitem(controller["run_search"].__globals__, "run_job", trial)
    controller["run_search"](config, job, tmp_path)
    result = json.loads((tmp_path / (job["id"] + "-search.json")).read_text())
    assert result["best_observed_zero_loss_bps_per_direction"] is None
    assert result["state"] == (
        "inconclusive_missing_counters"
        if reason == "missing"
        else "inconclusive_sender_rate_limited"
    )


def test_memory_summary_retains_units_and_process_groups(controller):
    samples = [
        {
            "time": 2,
            "processes": {
                "1": {"comm": {"value": "iperf3\n"}, "status": {"value": "VmRSS:  100 kB\n"}},
                "2": {"comm": {"value": "iperf3\n"}, "status": {"value": "VmRSS:  200 kB\n"}},
            },
        }
    ]
    result = controller["memory_window"](samples, 1, 3)
    assert result["peak_observed_rss_bytes_by_process_name"] == {"iperf3": 307200}


def test_report_combines_only_identical_manifests(manifest, tmp_path, fake_ssh):
    directories = [tmp_path / "pf", tmp_path / "vf"]
    for directory in directories:
        directory.mkdir()
        (directory / "manifest.json").write_bytes(manifest.read_bytes())
    output = tmp_path / "combined"
    result = cli("report", *directories, "--output", output)
    assert result.returncode == 0, result.stderr
    assert json.loads((output / "summary.json").read_text())["trials"] == []
    assert not fake_ssh.exists()
    config = json.loads(manifest.read_text())
    config["duration"] = 60
    (directories[1] / "manifest.json").write_text(json.dumps(config))
    rejected = tmp_path / "rejected"
    result = cli("report", *directories, "--output", rejected)
    assert result.returncode != 0 and "identical manifests" in result.stderr
    assert not rejected.exists()


def test_agent_server_owns_and_reaps_its_child(agent, tmp_path, monkeypatch, capsys):
    """Execute the agent server path with fixture executables, without opening sockets."""
    values, _ = agent
    pidfile = tmp_path / "pid"
    iperf = tmp_path / "iperf3"
    iperf.write_text(
        f"#!{sys.executable}\nimport os, time\nfrom pathlib import Path\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(.3)\nprint('{\"end\": {}}')\n"
    )
    iperf.chmod(0o755)
    numactl = tmp_path / "numactl"
    numactl.write_text(
        f"#!{sys.executable}\nimport os,sys\nos.execvp(sys.argv[3], sys.argv[3:])\n"
    )
    numactl.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setitem(values["server"].__globals__, "identity", lambda *args: "fixture")

    def command(argv, timeout=15):
        out = f'users:(("iperf3",pid={pidfile.read_text()},fd=3))' if pidfile.exists() else ""
        return {"returncode": 0, "stdout": out}

    monkeypatch.setitem(values["server"].__globals__, "command", command)
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd) as reader, os.fdopen(write_fd, "w"):
        monkeypatch.setattr(sys, "stdin", reader)
        values["server"](
            {
                "uuid": "fixture",
                "cpus": [2, 3],
                "memory_node": 0,
                "address": "192.0.2.20",
                "interface": "vf0.100",
                "port": 55201,
                "seconds": 3,
            }
        )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["ready"]
    assert events[1]["returncode"] == 0
    assert json.loads(events[1]["stdout"]) == {"end": {}}
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_inventory_capture_does_not_require_benchmark_configuration(tmp_path, fake_ssh):
    inventory = ROOT / "examples/network-benchmark/inventory.json"
    output = tmp_path / "inventory"
    result = cli("capture", inventory, "--execute", "--output", output)
    assert result.returncode == 0, result.stderr
    assert {p.stem for p in output.glob("state-*.json")} == {
        "state-host-a",
        "state-host-b",
        "state-arm-a0",
        "state-arm-a1",
        "state-arm-b0",
        "state-arm-b1",
    }
    calls = [json.loads(line) for line in fake_ssh.read_text().splitlines()]
    assert all(call["interfaces"] == [] and call["operation"] == "capture" for call in calls)
    assert output.stat().st_mode & 0o777 == 0o700


def test_capture_discovers_only_interfaces_and_queries_eswitch_per_device(agent, monkeypatch):
    values, fs = agent
    net = fs / "sys/class/net"
    (net / "eth0").mkdir(parents=True)
    (net / "bonding_masters").write_text("bond0\n")
    monkeypatch.setitem(values["capture"].__globals__, "identity", lambda *args: "fixture")
    calls = []

    def command(argv, timeout=15):
        calls.append(argv)
        if argv == ["devlink", "dev", "info"]:
            return {
                "returncode": 0,
                "stdout": "pci/0000:03:00.0:\n  driver mlx5_core\n"
                "auxiliary/mlx5_core.eth.0:\n  driver mlx5_core.eth\n",
            }
        return {"returncode": 0, "stdout": ""}

    monkeypatch.setitem(values["capture"].__globals__, "command", command)
    snapshot = values["capture"]({"uuid": "fixture", "interfaces": []})
    assert not any("bonding_masters" in argument for call in calls for argument in call)
    assert ["ethtool", "-i", "eth0"] in calls
    assert ["devlink", "dev", "eswitch", "show", "pci/0000:03:00.0"] in calls
    assert ["devlink", "dev", "eswitch", "show"] not in calls
    assert "eswitch/pci/0000:03:00.0" in snapshot["commands"]


def test_protocol_counters_keep_buffers_retransmits_and_gauges_separate(agent):
    values, _ = agent
    result = values["protocol_counters"](
        "Tcp: CurrEstab MaxConn RetransSegs\nTcp: 2 -1 7\n"
        "Udp: InDatagrams RcvbufErrors SndbufErrors\nUdp: 100 3 0\n"
        "TcpExt: TCPBacklogDrop TCPMemoryPressures\nTcpExt: 5 1\n"
    )
    assert result == {
        "values": {
            "Tcp.RetransSegs": 7,
            "Udp.InDatagrams": 100,
            "Udp.RcvbufErrors": 3,
            "Udp.SndbufErrors": 0,
            "TcpExt.TCPBacklogDrop": 5,
            "TcpExt.TCPMemoryPressures": 1,
        },
        "gauges": {"Tcp.CurrEstab": 2, "Tcp.MaxConn": -1},
        "errors": [],
    }


@pytest.mark.parametrize(
    "text",
    ["Udp: RcvbufErrors\n", "Udp: A B\nUdp: 1\n", "Udp: A\nTcp: 1\n", "Udp: A\nUdp: unknown\n"],
)
def test_malformed_protocol_counters_are_unavailable_not_zero(agent, text):
    values, _ = agent
    result = values["protocol_counters"](text)
    assert result["errors"] and result["values"] == {}


def test_driver_counters_and_discontinuities_preserve_zero(agent):
    values, _ = agent
    counters = values["numeric_counters"](
        "NIC statistics:\n rx0_packets: 100\n rx0_buff_alloc_err: 0\n"
        " rx_out_of_buffer: 3\n rx1_packets: unavailable\n"
    )
    assert counters == {"rx0_packets": 100, "rx0_buff_alloc_err": 0, "rx_out_of_buffer": 3}
    delta = values["counter_delta"](
        {"rx0_packets": 80, "rx0_buff_alloc_err": 0, "rx_out_of_buffer": 5, "gone": 1}, counters
    )
    assert delta == {
        "values": {"rx0_packets": 20, "rx0_buff_alloc_err": 0},
        "new": [],
        "decreased_or_reset": ["rx_out_of_buffer"],
        "missing": ["gone"],
    }


def test_interrupt_encoding_retains_all_counts_and_global_lines(agent):
    values, _ = agent
    result = values["interrupt_counters"](
        {
            "value": " CPU0 CPU1 CPU2\n 42: 0 17 0 PCI-MSI mlx5_comp0\n"
            " NMI: 0 0 0 Non-maskable\n ERR: 3\n"
        }
    )
    assert result["cpus"] == ["CPU0", "CPU1", "CPU2"]
    assert result["rows"]["42"] == {"counts": {"CPU1": 17}, "description": "PCI-MSI mlx5_comp0"}
    assert result["rows"]["NMI"]["counts"] == {}
    assert [result["rows"]["42"]["counts"].get(cpu, 0) for cpu in result["cpus"]] == [0, 17, 0]
    assert result["other_lines"] == [" ERR: 3"]
    assert values["interrupt_counters"]({"unavailable": "permission"}) == {
        "unavailable": "permission"
    }


def test_network_reads_are_bounded_and_socket_filter_is_scoped(agent, monkeypatch):
    values, _ = agent
    calls = []

    def command(argv, timeout=15):
        calls.append((argv, timeout))
        if argv[:2] == ["ethtool", "-S"]:
            return {"argv": argv, "returncode": 0, "stdout": " rx0_packets: 15\n"}
        if argv[0] == "tc":
            return {"argv": argv, "returncode": 0, "stdout": '[{"backlog":12,"drops":2}]'}
        return {"argv": argv, "unavailable": "fixture socket read timeout", "stdout": "partial"}

    monkeypatch.setitem(values["network_observations"].__globals__, "command", command)
    result = values["network_observations"]({"interfaces": ["pf0"], "port": 55201})
    assert result["pf0/statistics"]["counters"] == {"rx0_packets": 15}
    assert result["tcp_sockets"]["unavailable"] == "fixture socket read timeout"
    assert result["tcp_sockets"]["stdout"] == "partial"
    assert all(timeout == 2 for _, timeout in calls)
    assert (["ss", "-tinmH", "( sport = :55201 or dport = :55201 )"], 2) in calls
    assert (["tc", "-s", "-j", "qdisc", "show", "dev", "pf0"], 2) in calls
    with pytest.raises(ValueError, match="socket port"):
        values["network_observations"]({"interfaces": [], "port": "55201;bad"})


def test_sampler_reports_overhead_and_breaks_counter_continuity(agent, monkeypatch):
    values, fs = agent
    (fs / "proc").mkdir()
    output = []
    state = {"clock": 0.0, "reads": 0}
    globals_ = values["sample"].__globals__
    monkeypatch.setitem(globals_, "identity", lambda *args: "fixture")
    monkeypatch.setitem(globals_, "emit", output.append)
    monkeypatch.setattr(globals_["time"], "monotonic", lambda: state["clock"])
    monkeypatch.setattr(
        globals_["select"], "select", lambda *args: ([1] if state["reads"] == 3 else [], [], [])
    )

    def observations(request):
        state["reads"] += 1
        state["clock"] += 1.5
        if state["reads"] == 2:
            return {
                "pf0/statistics": {
                    "argv": ["ethtool", "-S", "pf0"],
                    "unavailable": "fixture timeout",
                }
            }
        return {
            "pf0/statistics": {
                "returncode": 0,
                "counters": {"rx0_packets": 10 if state["reads"] == 1 else 15},
            }
        }

    monkeypatch.setitem(globals_, "network_observations", observations)
    values["sample"]({"uuid": "fixture", "interfaces": [], "seconds": 10})
    assert output[0]["ready"]
    assert len(output) == 4
    assert all(s["collection_seconds"] == 1.5 and s["overrun_seconds"] == 0.5 for s in output[1:])
    assert output[2]["network"]["pf0/statistics"]["unavailable"] == "fixture timeout"
    assert output[3]["network_counter_deltas"]["pf0/statistics"] == {
        "values": {},
        "new": ["rx0_packets"],
        "decreased_or_reset": [],
        "missing": [],
    }
    assert "/proc/net/snmp" in output[1]["files"]
    assert output[1]["hardware_queue_occupancy"]["unavailable"]


def test_report_network_window_excludes_boundary_delta_and_keeps_failures(controller):
    samples = [
        {
            "time": t,
            "collection_seconds": 0.2,
            "overrun_seconds": 0,
            "network_counter_deltas": {
                "pf0/statistics": {
                    "values": {"rx0_packets": count},
                    "new": [],
                    "decreased_or_reset": [],
                    "missing": [],
                }
            },
            "network": {},
        }
        for t, count in [(0, 1000), (1, 3), (2, 4)]
    ]
    samples[-1]["network"] = {"tcp_sockets": {"argv": ["ss"], "unavailable": "fixture failure"}}
    result = controller["network_window"](samples, 0, 2)
    assert result["counter_changes"]["pf0/statistics"]["values"] == {"rx0_packets": 7}
    assert result["read_failures"][0]["unavailable"] == "fixture failure"
    assert result["max_collection_seconds"] == 0.2
    assert "unavailable" in controller["network_window"]([{"time": 0}, {"time": 1}], 0, 1)
