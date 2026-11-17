#!/usr/bin/env python3
"""Capture physical machines and benchmark existing Linux PF/VF VLAN attachments.

Planning is offline. Capture and run require --execute; neither configures
networking. Each run owns only its bounded iperf3 processes and SSH sessions.
"""

import argparse
import concurrent.futures
import copy
import hashlib
import ipaddress
import itertools
import json
import math
import os
import re
import select
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

AGENT = Path(__file__).with_name("network_benchmark_agent.py")
IFNAME = re.compile(r"[a-zA-Z0-9_.:-]{1,15}\Z")
SSH_TARGET = re.compile(r"(?:[a-zA-Z0-9_.-]+@)?[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z")


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer in {low}..{high}")
    return value


def name(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", value):
        raise ValueError(f"Invalid {label}")
    return value


def load(path, inventory=False):
    config = json.loads(Path(path).read_text())
    if config["version"] != 1:
        raise ValueError("Unsupported manifest version")
    nodes = config["nodes"]
    if not isinstance(nodes, dict) or not nodes:
        raise ValueError("nodes must be a nonempty object")
    identities = set()
    for node_id, node in nodes.items():
        name(node_id, "node name")
        if not SSH_TARGET.fullmatch(node["ssh"]):
            raise ValueError("SSH target must be a hostname, optionally with a username")
        kind = node.get("identity_kind", "dmi")
        pattern = r"[0-9a-fA-F-]{36}" if kind == "dmi" else r"[0-9a-fA-F]{32}"
        if kind not in {"dmi", "machine-id"} or not re.fullmatch(pattern, node["uuid"]):
            raise ValueError("Invalid machine identity")
        identity_key = (kind, node["uuid"].lower())
        if identity_key in identities:
            raise ValueError("Each node must have a distinct identity")
        identities.add(identity_key)
        if node["role"] not in {"host", "arm"}:
            raise ValueError("Node role must be host or arm")
        node.setdefault("interfaces", [])
        if (not inventory and not node["interfaces"]) or any(
            not IFNAME.fullmatch(i) for i in node["interfaces"]
        ):
            raise ValueError("Invalid observed interface list")
    if inventory:
        return config
    integer(config["mtu"], 576, 9000, "mtu")
    integer(config["duration"], 10, 3600, "duration")
    integer(config["warmup"], 1, 300, "warmup")
    integer(config["idle"], 2, 300, "idle")
    integer(config["repeats"], 1, 20, "repeats")
    integer(config["port"], 1024, 65535, "port")
    if set(config["endpoints"]) != {"a", "b"}:
        raise ValueError("Exactly endpoints a and b are required")
    endpoint_nodes = set()
    for endpoint in config["endpoints"].values():
        host = endpoint["node"]
        if host not in nodes or nodes[host]["role"] != "host":
            raise ValueError("Endpoint must reference a host node")
        endpoint_nodes.add(host)
        if not endpoint["arms"] or any(
            n not in nodes or nodes[n]["role"] != "arm" for n in endpoint["arms"]
        ):
            raise ValueError("Endpoint must list its observed DPU Arm nodes")
    if len(endpoint_nodes) != 2:
        raise ValueError("Endpoints must be on distinct hosts")
    if set(config["modes"]) != {"pf", "vf"}:
        raise ValueError("Supply pf and vf modes")
    for mode, attachments in config["modes"].items():
        if set(attachments) != {"a", "b"}:
            raise ValueError("Each mode must supply a and b attachments")
        for end, attachment in attachments.items():
            if attachment["kind"] != mode:
                raise ValueError("Attachment kind must match mode")
            if not all(IFNAME.fullmatch(attachment[k]) for k in ("interface", "parent")):
                raise ValueError("Invalid attachment interface")
            if attachment["interface"] == attachment["parent"]:
                raise ValueError("VLAN interface must differ from its parent")
            if not re.fullmatch(
                r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", attachment["pci"]
            ):
                raise ValueError("Explicit PCI BDF required")
            integer(attachment["vlan"], 1, 4094, "vlan")
            integer(attachment["numa_node"], 0, 255, "interface NUMA node")
            if ipaddress.ip_address(attachment["address"]).version != 4:
                raise ValueError("Initial suite supports IPv4 only")
            host = nodes[config["endpoints"][end]["node"]]
            if not {attachment["interface"], attachment["parent"]} <= set(host["interfaces"]):
                raise ValueError("Observed interfaces must include VLAN and parent")
    for end in ("a", "b"):
        pf, vf = (config["modes"][m][end] for m in ("pf", "vf"))
        if any(pf[k] != vf[k] for k in ("address", "vlan", "numa_node")):
            raise ValueError("PF/VF must use identical address, VLAN and interface NUMA node")
    if config["modes"]["pf"]["a"]["address"] == config["modes"]["pf"]["b"]["address"]:
        raise ValueError("Endpoint addresses must differ")
    for label, placements in config["placements"].items():
        name(label, "placement name")
        if set(placements) != {"a", "b"}:
            raise ValueError("Placement must describe both endpoints")
        for placement in placements.values():
            integer(placement["memory_node"], 0, 255, "memory_node")
            if not placement["cpus"] or len(set(placement["cpus"])) != len(placement["cpus"]):
                raise ValueError("Supply distinct CPU IDs")
            for cpu in placement["cpus"]:
                integer(cpu, 0, 65535, "CPU ID")
    if not config["placements"] or not config["cases"]:
        raise ValueError("Supply placements and cases")
    for case in config["cases"]:
        name(case["name"], "case name")
        if case["transport"] not in {"tcp", "udp"}:
            raise ValueError("Unsupported transport")
        integer(case["flows"], 1, 128, "flows")
        if case["direction"] not in {"a-b", "b-a", "bidir"}:
            raise ValueError("Unsupported direction")
        if case["placement"] not in config["placements"]:
            raise ValueError("Unknown placement")
        rate = case["rate_bps"]
        if rate == "search":
            if case["transport"] != "udp":
                raise ValueError("Rate search is for UDP; use maximum for TCP")
            search = case["search"]
            integer(search["min_bps"], case["flows"], 10**13, "search minimum")
            integer(search["max_bps"], search["min_bps"], 10**13, "search maximum")
            integer(search["steps"], 2, 20, "search steps")
            if any(search[k] % case["flows"] for k in ("min_bps", "max_bps")):
                raise ValueError("Search bounds must divide evenly across flows")
        elif rate == "maximum":
            if case["transport"] != "tcp":
                raise ValueError("UDP requires an explicit offered rate, not an unbounded flood")
        else:
            integer(rate, case["flows"], 10**13, "aggregate offered rate")
            if rate % case["flows"]:
                raise ValueError("Aggregate offered rate must divide evenly across flows")
        if case["transport"] == "udp":
            integer(case["payload_bytes"], 18, config["mtu"] - 28, "UDP payload")
    if len({case["name"] for case in config["cases"]}) != len(config["cases"]):
        raise ValueError("Case names must be unique")
    return config


def jobs(config):
    result = []
    for repeat, (index, case) in itertools.product(
        range(config["repeats"]), enumerate(config["cases"])
    ):
        # Alternate which mode runs first to expose drift rather than favour one mode.
        for mode in ("pf", "vf") if repeat % 2 == 0 else ("vf", "pf"):
            result.append(
                {
                    "id": f"r{repeat + 1:02d}-c{index + 1:02d}-{mode}",
                    "mode": mode,
                    "repeat": repeat + 1,
                    "case": case,
                }
            )
    return result


def request(config, node, operation, **kwargs):
    n = config["nodes"][node]
    return {
        "operation": operation,
        "uuid": n["uuid"],
        "identity_kind": n.get("identity_kind", "dmi"),
        "interfaces": n["interfaces"],
        **kwargs,
    }


def remote(node):
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        node["ssh"],
        shlex.join(["python3", "-u", "-c", AGENT.read_text()]),
    ]


def call(config, node, payload, timeout=180):
    result = subprocess.run(
        remote(config["nodes"][node]),
        input=json.dumps(payload) + "\n",
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(f"{node}: agent failed: {result.stdout} {result.stderr}")
    value = json.loads(result.stdout)
    if "error" in value:
        raise RuntimeError(f"{node}: {value['error']}")
    return value


def save(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def capture_all(config, directory, label):
    def one(item):
        node, _ = item
        value = call(config, node, request(config, node, "capture"))
        save(directory / f"{label}-{node}.json", value)
        return node

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, config["nodes"].items()))


def iperf_argv(config, job):
    case = job["case"]
    source, target = ("b", "a") if case["direction"] == "b-a" else ("a", "b")
    attachments = config["modes"][job["mode"]]
    argv = [
        "iperf3",
        "-c",
        attachments[target]["address"],
        "-B",
        attachments[source]["address"],
        "--bind-dev",
        attachments[source]["interface"],
        "-p",
        str(config["port"]),
        "-J",
        "--get-server-output",
        "-P",
        str(case["flows"]),
        "-t",
        str(config["duration"]),
        "-O",
        str(config["warmup"]),
        "--connect-timeout",
        "5000",
    ]
    if case["direction"] == "bidir":
        argv.append("--bidir")
    if case["rate_bps"] != "maximum":
        argv.extend(["-b", str(case["rate_bps"] // case["flows"])])
    if case["transport"] == "udp":
        argv.extend(["-u", "-l", str(case["payload_bytes"]), "--dont-fragment"])
    return source, target, argv


class Session:
    """Bounded remote agent; stdin EOF requests cleanup of owned children."""

    def __init__(self, config, node, payload, directory, label):
        self.path = directory / f"{label}.jsonl"
        self.output = self.path.open("x")
        self.error = (directory / f"{label}.stderr").open("x")
        self.streaming = payload["operation"] == "sample"
        self.process = subprocess.Popen(
            remote(config["nodes"][node]),
            stdin=subprocess.PIPE,
            stdout=self.output if self.streaming else subprocess.PIPE,
            stderr=self.error,
            text=True,
        )
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()
        self.ready = None

    def await_ready(self):
        if self.streaming:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                with self.path.open() as stream:
                    line = stream.readline()
                if line.endswith("\n"):
                    event = json.loads(line)
                    if event.get("ready"):
                        return
                    raise RuntimeError(f"Sampler failed: {line}")
                if self.process.poll() is not None:
                    raise RuntimeError("Sampler exited before readiness")
                time.sleep(0.05)
            raise RuntimeError("Sampler readiness timed out")
        assert self.process.stdout is not None
        if not select.select([self.process.stdout], [], [], 30)[0]:
            raise RuntimeError("Remote service readiness timed out")
        line = self.process.stdout.readline()
        self.output.write(line)
        self.output.flush()
        self.ready = json.loads(line)
        if not self.ready.get("ready"):
            raise RuntimeError(f"Remote service failed: {line}")

    def stop(self):
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
            self.process.stdin = None
        timed_out = False
        try:
            stdout, _ = self.process.communicate(timeout=25)
        except subprocess.TimeoutExpired:
            self.process.kill()
            stdout, _ = self.process.communicate()
            timed_out = True
        finally:
            self.error.close()
        if stdout:
            self.output.write(stdout)
        self.output.close()
        if timed_out:
            raise RuntimeError("Remote agent stop timed out; remote watchdog remains bounded")
        if self.process.returncode:
            raise RuntimeError("Remote session failed; see its JSONL and stderr")


def preflight(config, job, directory):
    attachments = config["modes"][job["mode"]]
    placement = config["placements"][job["case"]["placement"]]
    evidence = {}
    for end in ("a", "b"):
        node = config["endpoints"][end]["node"]
        evidence[end] = call(
            config,
            node,
            request(
                config,
                node,
                "check",
                attachment=attachments[end],
                mtu=config["mtu"],
                peer=attachments["b" if end == "a" else "a"]["address"],
                **placement[end],
            ),
        )
        ports = call(config, node, request(config, node, "ports", port=config["port"]))
        if ports.get("returncode") != 0 or ports["stdout"].strip():
            raise RuntimeError(f"{node}: benchmark TCP port unavailable")
    clocks = {}
    for node in config["nodes"]:
        before = time.time()
        remote_clock = call(config, node, request(config, node, "clock"))["time"]
        after = time.time()
        clocks[node] = {
            "offset_seconds": remote_clock - (before + after) / 2,
            "uncertainty_seconds": (after - before) / 2,
        }
        if after - before > 2:
            raise RuntimeError("Clock-offset uncertainty exceeds one second; retry preflight")
    save(directory / "clock.json", clocks)
    save(directory / "preflight.json", evidence)


def run_job(config, job, directory):
    directory.mkdir()
    save(directory / "job.json", job)
    seconds = config["idle"] + config["warmup"] + config["duration"] + 60
    sessions = []
    failures = []
    result = {"complete": False, "job": job}
    prepared = False
    try:
        preflight(config, job, directory)
        capture_all(config, directory, "before")
        prepared = True
        for node in config["nodes"]:
            sessions.append(
                Session(
                    config,
                    node,
                    request(config, node, "sample", seconds=seconds, port=config["port"]),
                    directory,
                    f"samples-{node}",
                )
            )
        for session in sessions:
            session.await_ready()
        result["idle_started"] = time.time()
        time.sleep(config["idle"])
        source, target, argv = iperf_argv(config, job)
        src = config["endpoints"][source]["node"]
        dst = config["endpoints"][target]["node"]
        addresses = config["modes"][job["mode"]]
        placement = config["placements"][job["case"]["placement"]]
        server = Session(
            config,
            dst,
            request(
                config,
                dst,
                "server",
                address=addresses[target]["address"],
                interface=addresses[target]["interface"],
                port=config["port"],
                seconds=seconds,
                **placement[target],
            ),
            directory,
            "server",
        )
        sessions.append(server)
        server.await_ready()
        result["client"] = call(
            config,
            src,
            request(
                config,
                src,
                "client",
                argv=argv,
                address=addresses[source]["address"],
                peer=addresses[target]["address"],
                seconds=config["warmup"] + config["duration"],
                **placement[source],
            ),
            timeout=seconds,
        )
        traffic = result["client"]["traffic"]
        if traffic.get("returncode") != 0:
            raise RuntimeError("iperf3 client failed")
        payload = json.loads(traffic["stdout"])
        if "error" in payload or "end" not in payload:
            raise RuntimeError("iperf3 returned incomplete results")
        # Allow the one-shot server to flush its final JSON before requesting cleanup.
        try:
            server.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            raise RuntimeError("iperf3 server did not finish after its client") from None
        result["complete"] = True
    except Exception as error:
        result["error"] = str(error)
    finally:
        for session in reversed(sessions):
            try:
                session.stop()
            except Exception as error:
                failures.append(str(error))
        if result["complete"]:
            try:
                records = [
                    json.loads(line)
                    for line in (directory / "server.jsonl").read_text().splitlines()
                ]
                final = records[-1]
                payload = json.loads(final["stdout"])
                if final["returncode"] != 0 or "error" in payload or "end" not in payload:
                    raise RuntimeError("iperf3 receiver returned incomplete results")
            except (ValueError, KeyError, RuntimeError) as error:
                failures.append(str(error))
        if prepared:
            try:
                capture_all(config, directory, "after")
            except Exception as error:
                failures.append("After-state capture failed: " + str(error))
        if failures:
            result["complete"] = False
            result["cleanup_errors"] = failures
        result["ended"] = time.time()
        save(directory / "result.json", result)
    if not result["complete"]:
        raise RuntimeError(f"Trial failed: {directory}; {result.get('error', failures)}")
    return result


def received_summaries(traffic, case):
    keys = ["sum_received"]
    if case["direction"] == "bidir":
        keys.append("sum_received_bidir_reverse")
    return [traffic.get("end", {}).get(key, {}) for key in keys]


def run_search(config, job, directory):
    """Find an observed zero-loss UDP rate; retain every probe as an ordinary trial."""
    search = job["case"]["search"]
    low, high = 0, search["max_bps"]
    rate = high
    attempts = []
    best = None
    state = "step_limit"
    for step in range(search["steps"]):
        probe = copy.deepcopy(job)
        probe["id"] += f"-s{step + 1:02d}"
        probe["case"]["rate_bps"] = rate
        result = run_job(config, probe, directory / probe["id"])
        traffic = json.loads(result["client"]["traffic"]["stdout"])
        received = received_summaries(traffic, probe["case"])
        sent_keys = ["sum_sent"]
        if probe["case"]["direction"] == "bidir":
            sent_keys.append("sum_sent_bidir_reverse")
        sent = [traffic.get("end", {}).get(key, {}) for key in sent_keys]
        if not all("bits_per_second" in item for item in sent) or not all(
            "packets" in item and "lost_packets" in item for item in received
        ):
            state = "inconclusive_missing_counters"
            break
        if any(item["bits_per_second"] < rate * 0.99 for item in sent):
            state = "inconclusive_sender_rate_limited"
            break
        passed = all(item["packets"] > 0 and item["lost_packets"] == 0 for item in received)
        attempts.append(
            {"trial": probe["id"], "rate_bps_per_direction": rate, "observed_zero_loss": passed}
        )
        if passed:
            best = rate
            low = rate
            if rate == search["max_bps"]:
                state = "configured_ceiling_reached"
                break
        else:
            high = rate
            if rate == search["min_bps"]:
                state = "no_passing_rate_in_range"
                break
        rate = max(
            search["min_bps"], ((low + high) // (2 * job["case"]["flows"])) * job["case"]["flows"]
        )
        if rate <= low or rate >= high:
            state = "rate_resolution_reached"
            break
    save(
        directory / (job["id"] + "-search.json"),
        {
            "job": job,
            "state": state,
            "best_observed_zero_loss_bps_per_direction": best,
            "attempts": attempts,
            "boundary": "Finite-duration observed zero-loss UDP search. "
            "This is not a reliability guarantee. "
            "Latency is reported separately; no latency acceptance ceiling is imposed.",
        },
    )


def percentiles(values):
    values = sorted(values)
    if not values:
        return {"samples": 0}
    return {
        "samples": len(values),
        **{
            label: values[math.ceil(q * len(values)) - 1]
            for label, q in (("p50_ms", 0.5), ("p99_ms", 0.99), ("p999_ms", 0.999))
        },
    }


def cpu_values(sample):
    values = {}
    for line in sample["files"].get("/proc/stat", {}).get("value", "").splitlines():
        fields = line.split()
        if fields and re.fullmatch(r"cpu[0-9]+", fields[0]):
            ticks = list(map(int, fields[1:9]))
            if len(ticks) == 8:
                values[fields[0]] = ticks
    return values


def cpu_window(samples, start, end):
    selected = [s for s in samples if start <= s["time"] <= end]
    if len(selected) < 2:
        return {"unavailable": "Fewer than two telemetry samples in phase"}
    first, last = selected[0], selected[-1]
    elapsed = last["monotonic"] - first["monotonic"]
    if elapsed <= 0:
        return {"unavailable": "Non-increasing monotonic clock"}
    before, after = cpu_values(first), cpu_values(last)
    cores = {}
    for core in before.keys() & after.keys():
        delta = [b - a for a, b in zip(before[core], after[core], strict=True)]
        if any(d < 0 for d in delta):
            continue
        # /proc/stat user/nice already include guest ticks. Idle and iowait are not busy CPU.
        busy = sum(delta[i] for i in (0, 1, 2, 5, 6, 7)) / first["clock_ticks"]
        cores[core] = {
            "busy_seconds": busy,
            "busy_cores": busy / elapsed,
            "irq_seconds": delta[5] / first["clock_ticks"],
            "softirq_seconds": delta[6] / first["clock_ticks"],
        }
    if not cores:
        return {"unavailable": "No comparable per-core CPU counters"}
    return {
        "observed_seconds": elapsed,
        "per_core": dict(sorted(cores.items())),
        "busy_cores": sum(c["busy_cores"] for c in cores.values()),
    }


def memory_window(samples, start, end):
    peaks = {}
    for sample in samples:
        if not start <= sample["time"] <= end:
            continue
        totals = {}
        for process in sample.get("processes", {}).values():
            name = process["comm"].get("value", "").strip()
            match = re.search(r"^VmRSS:\s+(\d+) kB$", process["status"].get("value", ""), re.M)
            if match:
                totals[name] = totals.get(name, 0) + int(match[1]) * 1024
        for name, size in totals.items():
            peaks[name] = max(peaks.get(name, 0), size)
    return {
        "peak_observed_rss_bytes_by_process_name": peaks,
        "boundary": "Sampled process RSS; shared pages may be counted more than once. "
        "Kernel memory, hugepages and NUMA counters are in raw samples.",
    }


def network_window(samples, start, end):
    selected = [sample for sample in samples if start <= sample["time"] <= end]
    if len(selected) < 2:
        return {"unavailable": "Fewer than two network telemetry samples in phase"}
    if not any("network_counter_deltas" in sample for sample in selected):
        return {"unavailable": "Archive has no extended network telemetry"}
    sources = {}
    failures = []
    for sample in selected[1:]:
        for name, delta in sample.get("network_counter_deltas", {}).items():
            totals = sources.setdefault(name, {"values": {}, "discontinuities": []})
            for counter, value in delta["values"].items():
                totals["values"][counter] = totals["values"].get(counter, 0) + value
            for kind in ("new", "decreased_or_reset", "missing"):
                if delta.get(kind):
                    totals["discontinuities"].append(
                        {"time": sample["time"], "kind": kind, "fields": delta[kind]}
                    )
        for name, result in sample.get("network", {}).items():
            if (
                ("argv" in result and result.get("returncode") != 0)
                or result.get("unavailable")
                or result.get("errors")
            ):
                failures.append(
                    {
                        "time": sample["time"],
                        "source": name,
                        "returncode": result.get("returncode"),
                        "unavailable": result.get("unavailable"),
                        "parse_errors": result.get("errors"),
                        "stderr": result.get("stderr"),
                    }
                )
    return {
        "counter_changes": sources,
        "read_failures": failures,
        "samples": len(selected),
        "max_collection_seconds": max(s.get("collection_seconds", 0) for s in selected),
        "overrun_samples": sum(s.get("overrun_seconds", 0) > 0 for s in selected),
        "boundary": "Sums of comparable adjacent-sample deltas within the phase; gaps and "
        "decreases are retained. Socket and qdisc snapshots remain in raw JSONL. "
        "Driver/interface/kernel counters overlap and must not be added as packet loss.",
    }


def resource_summary(path, client, config, job, delivered_bps, delivered_pps):
    summaries = {}
    clocks = json.loads((path / "clock.json").read_text())
    source = "b" if job["case"]["direction"] == "b-a" else "a"
    source_node = config["endpoints"][source]["node"]
    client_start = client["started"] - clocks[source_node]["offset_seconds"]
    # Preserve per-node evidence; loaded windows require synchronized Unix clocks.
    for node, details in config["nodes"].items():
        file = path / f"samples-{node}.jsonl"
        if not file.exists():
            summaries[node] = {"unavailable": "Telemetry file missing"}
            continue
        samples = [json.loads(line) for line in file.read_text().splitlines()]
        samples = [s for s in samples if "files" in s]
        if not samples:
            summaries[node] = {"unavailable": "Telemetry empty"}
            continue
        for sample in samples:
            sample["time"] -= clocks[node]["offset_seconds"]
        # Sampling starts before the idle wait. These windows are deliberately conservative:
        # exclude one second at each boundary to avoid attributing startup to steady load.
        first = samples[0]["time"]
        idle = cpu_window(samples, first, first + config["idle"] - 0.25)
        # Controller/client timestamps are Unix times; record clock skew as a limitation.
        load_start = client_start + config["warmup"] + 1
        loaded = cpu_window(samples, load_start, load_start + config["duration"] - 2)
        value = {
            "role": details["role"],
            "idle": idle,
            "loaded": loaded,
            "boundary": (
                "Whole-machine CPU including background work; sampled clock offset correction"
            ),
            "clock": clocks[node],
            "loaded_memory": memory_window(
                samples, load_start, load_start + config["duration"] - 2
            ),
            "idle_network": network_window(samples, first, first + config["idle"] - 0.25),
            "loaded_network": network_window(
                samples, load_start, load_start + config["duration"] - 2
            ),
        }
        if "busy_cores" in idle and "busy_cores" in loaded:
            delta = loaded["busy_cores"] - idle["busy_cores"]
            value["incremental_busy_cores"] = delta
            if delivered_bps and delivered_bps > 0:
                value["total_cpu_seconds_per_gbit"] = loaded["busy_cores"] * 1e9 / delivered_bps
                value["incremental_cpu_seconds_per_gbit"] = delta * 1e9 / delivered_bps
            if delivered_pps and delivered_pps > 0:
                value["total_cpu_seconds_per_million_packets"] = (
                    loaded["busy_cores"] * 1e6 / delivered_pps
                )
                value["incremental_cpu_seconds_per_million_packets"] = delta * 1e6 / delivered_pps
        summaries[node] = value
    return summaries


def report(directory):
    config = json.loads((directory / "manifest.json").read_text())
    summaries, charts = [], []
    for path in sorted(directory.glob("r*/result.json")):
        result = json.loads(path.read_text())
        row = {"id": path.parent.name, "complete": result["complete"], "job": result["job"]}
        series: list[dict[str, Any]] = []
        if result.get("client"):
            client = result["client"]
            raw = client["traffic"]
            if raw.get("returncode") == 0:
                traffic = json.loads(raw["stdout"])
                row["iperf_end"] = traffic.get("end", {})
                keys = ["sum_received"]
                if result["job"]["case"]["direction"] == "bidir":
                    keys.append("sum_received_bidir_reverse")
                received = [row["iperf_end"].get(key, {}) for key in keys]
                delivered = (
                    sum(r["bits_per_second"] for r in received)
                    if all("bits_per_second" in r for r in received)
                    else None
                )
                row["delivered_bps"] = delivered
                pps = None
                if result["job"]["case"]["transport"] == "udp" and all(
                    all(key in item for key in ("packets", "lost_packets", "seconds"))
                    and item["seconds"] > 0
                    for item in received
                ):
                    pps = sum(
                        (item["packets"] - item["lost_packets"]) / item["seconds"]
                        for item in received
                    )
                row["delivered_udp_pps"] = pps
                row["resources"] = resource_summary(
                    path.parent, client, config, result["job"], delivered, pps
                )
                # Preserve direction-specific streams; bidirectional sums vary by iperf version.
                for index, interval in enumerate(traffic.get("intervals", [])):
                    streams = interval.get("streams", [])
                    for n, stream in enumerate(streams):
                        if index == 0:
                            series.append(
                                {"name": f"stream {n} sender={stream.get('sender')}", "points": []}
                            )
                        if not stream.get("omitted"):
                            series[n]["points"].append(
                                [stream["end"], stream["bits_per_second"] / 1e9]
                            )
            probe = client["latency"]
            cutoff = client["started"] + config["warmup"]
            samples = []
            for line in probe.get("stdout", "").splitlines():
                match = re.search(r"\[([0-9.]+)\].*time=([0-9.]+) ms", line)
                if match and float(match[1]) >= cutoff:
                    samples.append(float(match[2]))
            row["icmp_rtt"] = percentiles(samples) | {
                "returncode": probe.get("returncode"),
                "boundary": "Sampled ICMP RTT, excludes warmup",
            }
            row["memory_bandwidth"] = {"unavailable": "Platform-specific PMU collection required"}
        summaries.append(row)
        charts.append(
            {
                "title": path.parent.name,
                "charts": [
                    {
                        "title": "Per-stream throughput",
                        "xlabel": "iperf seconds (includes omitted warmup)",
                        "ylabel": "Gbit/s",
                        "series": series,
                    }
                ],
            }
        )
    return {
        "version": 1,
        "trials": summaries,
        "searches": [json.loads(p.read_text()) for p in sorted(directory.glob("*-search.json"))],
        "boundary": "Kernel iperf3 and sampled ICMP RTT; no Ceph/OVN/TRex acceptance claim",
    }, {"version": 1, "title": "PF/VF Linux VLAN benchmark", "runs": charts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    for operation in ("validate", "plan", "capture", "run"):
        command = sub.add_parser(operation)
        command.add_argument("manifest", type=Path)
        if operation not in {"plan", "validate"}:
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--execute", action="store_true")
        if operation == "run":
            command.add_argument(
                "--mode",
                choices=("pf", "vf"),
                required=True,
                help="Run only the currently configured attachment mode",
            )
    summary = sub.add_parser("report")
    summary.add_argument("directories", type=Path, nargs="+")
    summary.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.operation == "report":
        manifests = [json.loads((p / "manifest.json").read_text()) for p in args.directories]
        if any(m != manifests[0] for m in manifests[1:]):
            raise ValueError("Comparison requires identical manifests")
        combined = [report(p) for p in args.directories]
        os.umask(0o077)
        args.output.mkdir(parents=True, exist_ok=False)
        values = {
            "version": 1,
            "sources": [str(p.resolve()) for p in args.directories],
            "trials": [trial for value, _ in combined for trial in value["trials"]],
            "searches": [search for value, _ in combined for search in value["searches"]],
            "boundary": combined[0][0]["boundary"],
        }
        charts = {
            "version": 1,
            "title": "PF/VF Linux VLAN comparison",
            "runs": [run for _, chart in combined for run in chart["runs"]],
        }
        save(args.output / "summary.json", values)
        save(args.output / "report.json", charts)
        return
    config = load(args.manifest, inventory=args.operation in {"capture", "validate"})
    if args.operation == "validate":
        print(f"Inventory valid: {len(config['nodes'])} nodes; no SSH or traffic executed")
        return
    plan = jobs(config) if args.operation != "capture" else []
    if args.operation == "plan":
        print(
            json.dumps(
                {
                    "jobs": plan,
                    "nodes": config["nodes"],
                    "boundary": "Offline plan; no SSH, configuration or traffic",
                },
                indent=2,
            )
        )
        return
    if not args.execute:
        parser.error("--execute required for SSH capture or benchmark traffic")
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    save(args.output / "manifest.json", config)
    save(
        args.output / "provenance.json",
        {
            "started": time.time(),
            "agent_sha256": hashlib.sha256(AGENT.read_bytes()).hexdigest(),
            "controller_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
    )
    if args.operation == "capture":
        capture_all(config, args.output, "state")
    else:
        for job in plan:
            if job["mode"] == args.mode:
                if job["case"]["rate_bps"] == "search":
                    run_search(config, job, args.output)
                else:
                    run_job(config, job, args.output / job["id"])
        values, charts = report(args.output)
        save(args.output / "summary.json", values)
        save(args.output / "report.json", charts)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"network-benchmark: {error}", file=sys.stderr)
        sys.exit(1)
