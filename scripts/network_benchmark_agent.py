"""Standalone stdlib agent, sent over SSH without installing files remotely.

Read requests on stdin. Observations retain stdout, stderr and exit status,
following inspect_guest.py. No network configuration commands are issued.
"""

import concurrent.futures
import json
import os
import re
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def read(path):
    try:
        return {"value": Path(path).read_text()}
    except OSError as exc:
        return {"unavailable": str(exc)}


def command(argv, timeout=15):
    started = time.time()
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {
            "argv": argv,
            "started": started,
            "ended": time.time(),
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        return {
            "argv": argv,
            "started": started,
            "ended": time.time(),
            "unavailable": str(exc),
            "stdout": stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout,
            "stderr": stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr,
        }
    except OSError as exc:
        return {"argv": argv, "started": started, "ended": time.time(), "unavailable": str(exc)}


def identity(expected, kind="dmi"):
    path = "/sys/class/dmi/id/product_uuid" if kind == "dmi" else "/etc/machine-id"
    actual = Path(path).read_text().strip().lower()
    if actual != expected.lower():
        raise ValueError("Machine UUID mismatch")
    return actual


def capture(request):
    identity(request["uuid"], request.get("identity_kind", "dmi"))
    commands = {
        "kernel": ["uname", "-a"],
        "cpu": ["lscpu", "-J"],
        "pci": ["lspci", "-Dnnk"],
        "numa": ["numactl", "--hardware"],
        "links": ["ip", "-j", "-d", "link", "show"],
        "addresses": ["ip", "-j", "address", "show"],
        "routes4": ["ip", "-j", "-4", "route", "show", "table", "all"],
        "routes6": ["ip", "-j", "-6", "route", "show", "table", "all"],
        "rules": ["ip", "-j", "rule", "show"],
        "iperf_version": ["iperf3", "--version"],
        "ovs_version": ["ovs-vsctl", "--version"],
        "ovs": ["ovs-vsctl", "show"],
        "ovs_offload": ["ovs-vsctl", "get", "Open_vSwitch", ".", "other_config"],
        "devlink": ["devlink", "dev", "info"],
        "devlink_ports": ["devlink", "port", "show"],
        "ovs_datapath": ["ovs-appctl", "dpctl/show", "-s"],
    }
    files = {
        p: read(p)
        for p in (
            "/proc/sys/kernel/random/boot_id",
            "/proc/cmdline",
            "/proc/meminfo",
            "/proc/interrupts",
            "/proc/softirqs",
            "/sys/class/dmi/id/product_name",
            "/sys/class/dmi/id/sys_vendor",
            "/sys/class/dmi/id/bios_version",
            "/sys/class/dmi/id/bios_date",
            "/sys/devices/system/cpu/smt/active",
            "/sys/devices/system/cpu/intel_pstate/no_turbo",
            "/proc/sys/net/core/rmem_default",
            "/proc/sys/net/core/rmem_max",
            "/proc/sys/net/core/wmem_default",
            "/proc/sys/net/core/wmem_max",
            "/proc/sys/net/core/netdev_max_backlog",
            "/proc/sys/net/core/netdev_budget",
            "/proc/sys/net/core/netdev_budget_usecs",
            "/proc/sys/net/core/somaxconn",
            "/proc/sys/net/ipv4/tcp_rmem",
            "/proc/sys/net/ipv4/tcp_wmem",
            "/proc/sys/net/ipv4/tcp_mem",
            "/proc/sys/net/ipv4/tcp_limit_output_bytes",
            "/proc/sys/net/ipv4/tcp_moderate_rcvbuf",
            "/proc/sys/net/ipv4/tcp_congestion_control",
            "/proc/sys/net/ipv4/udp_mem",
            "/proc/sys/net/ipv4/udp_rmem_min",
            "/proc/sys/net/ipv4/udp_wmem_min",
        )
    }
    patterns = (
        "/proc/net/bonding/*",
        "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor",
        "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq",
        "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_max_freq",
        "/sys/devices/system/cpu/cpu[0-9]*/topology/thread_siblings_list",
        "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_min_freq",
        "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/energy_performance_preference",
        "/proc/irq/*/smp_affinity_list",
        "/sys/devices/system/node/node*/distance",
    )
    for pattern in patterns:
        for path in Path("/").glob(pattern.lstrip("/")):
            files[str(path)] = read(path)
    interfaces = request["interfaces"] or sorted(
        p.name for p in Path("/sys/class/net").iterdir() if p.is_dir()
    )
    for interface in interfaces:
        for label, args in {
            "driver": ["-i"],
            "offloads": ["-k"],
            "queues": ["-l"],
            "rings": ["-g"],
            "coalesce": ["-c"],
            "rss": ["-x"],
            "statistics": ["-S"],
            "link": [],
        }.items():
            commands[f"{interface}/{label}"] = ["ethtool", *args, interface]
        commands[f"{interface}/tc"] = ["tc", "-s", "filter", "show", "dev", interface, "ingress"]
        base = Path("/sys/class/net") / interface
        for name in (
            "device/numa_node",
            "device/uevent",
            "mtu",
            "address",
            "phys_port_name",
            "device/sriov_numvfs",
            "device/sriov_totalvfs",
        ):
            files[str(base / name)] = read(base / name)
        for path in base.glob("queues/*/*_cpus"):
            files[str(path)] = read(path)
        files[f"{interface}/pci"] = {"value": str((base / "device").resolve())}
        files[f"{interface}/physfn"] = {
            "value": str((base / "device/physfn").resolve())
            if (base / "device/physfn").exists()
            else None
        }
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        observations = dict(zip(commands, pool.map(command, commands.values()), strict=True))
    devices = observations["devlink"]
    if devices.get("returncode") == 0:
        for device in re.findall(r"^(pci/[^\s:]+:[^\s]+):$", devices["stdout"], re.M):
            observations[f"eswitch/{device}"] = command(
                ["devlink", "dev", "eswitch", "show", device]
            )
    return {
        "time": time.time(),
        "uuid": request["uuid"],
        "commands": observations,
        "files": files,
        "boundary": "Read-only observations; unavailable commands are retained, not passes.",
    }


def check(request):
    identity(request["uuid"], request.get("identity_kind", "dmi"))
    attachment = request["attachment"]
    links = command(["ip", "-j", "-d", "link", "show"])
    addresses = command(["ip", "-j", "address", "show", "dev", attachment["interface"]])
    if links.get("returncode") != 0 or addresses.get("returncode") != 0:
        raise ValueError("Cannot inspect attachment")
    by_name = {row["ifname"]: row for row in json.loads(links["stdout"])}
    vlan = by_name[attachment["interface"]]
    parent = by_name[attachment["parent"]]
    info = vlan.get("linkinfo", {})
    if (
        info.get("info_kind") != "vlan"
        or info.get("info_data", {}).get("id") != attachment["vlan"]
        or not any(key in vlan for key in ("link_index", "link"))
        or ("link_index" in vlan and vlan["link_index"] != parent["ifindex"])
        or ("link" in vlan and vlan["link"] != attachment["parent"])
        or vlan["mtu"] != request["mtu"]
        or "UP" not in vlan.get("flags", [])
        or parent["mtu"] < request["mtu"]
    ):
        raise ValueError("VLAN ID, parent, MTU or administrative state mismatch")
    found = {row["local"] for link in json.loads(addresses["stdout"]) for row in link["addr_info"]}
    if attachment["address"] not in found:
        raise ValueError("Source address missing from VLAN interface")
    device = Path("/sys/class/net") / attachment["parent"] / "device"
    if not device.exists() or device.resolve().name != attachment["pci"]:
        raise ValueError("Parent PCI identity mismatch")
    actual_numa = int((device / "numa_node").read_text())
    if actual_numa != attachment["numa_node"]:
        raise ValueError("Interface NUMA node mismatch")
    vf = (device / "physfn").exists()
    if vf != (attachment["kind"] == "vf"):
        raise ValueError("Parent PF/VF type mismatch")
    route = command(["ip", "-j", "route", "get", request["peer"], "from", attachment["address"]])
    if route.get("returncode") != 0:
        raise ValueError("Route lookup failed")
    routes = json.loads(route["stdout"])
    if len(routes) != 1 or routes[0].get("dev") != attachment["interface"]:
        raise ValueError("Traffic would use a different interface")
    if not set(request["cpus"]) <= os.sched_getaffinity(0):
        raise ValueError("Requested CPUs are unavailable")
    node = Path(f"/sys/devices/system/node/node{request['memory_node']}")
    if not node.is_dir():
        raise ValueError("Requested memory NUMA node is unavailable")
    cpulist = (node / "cpulist").read_text().strip()
    allowed = set()
    for part in cpulist.split(","):
        first, _, last = part.partition("-")
        allowed.update(range(int(first), int(last or first) + 1))
    if not set(request["cpus"]) <= allowed:
        raise ValueError("CPU placement differs from requested memory NUMA node")
    version = command(["iperf3", "--version"])
    numa = command(["numactl", "--show"])
    if version.get("returncode") != 0 or numa.get("returncode") != 0:
        raise ValueError("iperf3 and usable numactl are required")
    binding = command(bound(request, ["python3", "-c", "pass"]))
    if binding.get("returncode") != 0:
        raise ValueError("Requested CPU/memory binding is unavailable")
    return {
        "uuid": request["uuid"],
        "attachment": attachment,
        "route": route,
        "links": links,
        "addresses": addresses,
        "iperf": version,
        "numa": numa,
    }


def numeric_counters(text):
    """Parse unsigned driver/SNMP6 counters; keep the original dump alongside them."""
    return {
        match[1]: int(match[2])
        for match in re.finditer(r"^\s*([^:\s]+):?\s+(\d+)\s*$", text, re.M)
    }


def protocol_counters(text):
    """Read paired /proc/net/snmp and netstat headers without positional guesses."""
    lines = [line.split() for line in text.splitlines() if line.strip()]
    counters, errors = {}, []
    for index in range(0, len(lines), 2):
        header = lines[index]
        values = lines[index + 1] if index + 1 < len(lines) else []
        if not values or header[0] != values[0] or len(header) != len(values):
            errors.append("Malformed protocol header/value pair: " + header[0])
            continue
        try:
            counters.update(
                {
                    header[0].rstrip(":") + "." + name: int(value)
                    for name, value in zip(header[1:], values[1:], strict=True)
                }
            )
        except ValueError:
            errors.append("Non-integer protocol value: " + header[0])
    gauge_names = {
        "Ip.Forwarding",
        "Ip.DefaultTTL",
        "Ip.ReasmTimeout",
        "Tcp.RtoAlgorithm",
        "Tcp.RtoMin",
        "Tcp.RtoMax",
        "Tcp.MaxConn",
        "Tcp.CurrEstab",
    }
    return {
        "values": {key: value for key, value in counters.items() if key not in gauge_names},
        "gauges": {key: value for key, value in counters.items() if key in gauge_names},
        "errors": errors,
    }


def counter_delta(previous, current):
    """Do not turn resets, disappearing fields or first observations into zero."""
    return {
        "values": {
            key: value - previous[key]
            for key, value in current.items()
            if key in previous and value >= previous[key]
        },
        "new": sorted(current.keys() - previous.keys()),
        "decreased_or_reset": sorted(
            key for key in current if key in previous and current[key] < previous[key]
        ),
        "missing": sorted(previous.keys() - current.keys()),
    }


def interrupt_counters(observation):
    """Retain every IRQ/CPU count without repeating hundreds of zero columns."""
    if "value" not in observation:
        return observation
    lines = observation["value"].splitlines()
    cpus = lines[0].split() if lines else []
    if not cpus or not all(re.fullmatch(r"CPU\d+", cpu) for cpu in cpus):
        return {"unavailable": "Unrecognized interrupt CPU header", "raw": observation["value"]}
    rows, other = {}, []
    for line in lines[1:]:
        label, separator, rest = line.partition(":")
        fields = rest.split()
        if (
            not separator
            or len(fields) < len(cpus)
            or not all(field.isdigit() for field in fields[: len(cpus)])
        ):
            other.append(line)
            continue
        rows[label.strip()] = {
            "counts": {
                cpu: int(value)
                for cpu, value in zip(cpus, fields[: len(cpus)], strict=True)
                if value != "0"
            },
            "description": " ".join(fields[len(cpus) :]),
        }
    return {
        "cpus": cpus,
        "rows": rows,
        "other_lines": other,
        "encoding": "Sparse CPU columns: a missing CPU entry in a row means exactly zero. "
        "All IRQ rows are retained; global/non-per-CPU lines remain verbatim.",
    }


def network_observations(request):
    """Bound each read and filter socket details to the benchmark port only."""
    port = request.get("port", 55201)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Invalid telemetry socket port")
    selector = f"( sport = :{port} or dport = :{port} )"
    commands = {
        "tcp_sockets": ["ss", "-tinmH", selector],
        "udp_sockets": ["ss", "-uanmH", selector],
        "ceph_tcp_sockets": [
            "ss",
            "-tinmH",
            "( ( sport >= :6800 and sport <= :7300 ) or ( dport >= :6800 and dport <= :7300 ) )",
        ],
    }
    for interface in request["interfaces"]:
        commands[f"{interface}/statistics"] = ["ethtool", "-S", interface]
        commands[f"{interface}/qdisc"] = ["tc", "-s", "-j", "qdisc", "show", "dev", interface]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = dict(
            zip(
                commands,
                pool.map(lambda argv: command(argv, timeout=2), commands.values()),
                strict=True,
            )
        )
    for name, result in results.items():
        if name.endswith("/statistics") and result.get("returncode") == 0:
            result["counters"] = numeric_counters(result["stdout"])
    return results


def sample(request):
    identity(request["uuid"], request.get("identity_kind", "dmi"))
    paths = [
        "/proc/stat",
        "/proc/softirqs",
        "/proc/meminfo",
        "/proc/vmstat",
        "/proc/net/softnet_stat",
        "/proc/net/sockstat",
        "/proc/net/sockstat6",
        "/proc/net/snmp",
        "/proc/net/snmp6",
        "/proc/net/netstat",
    ]
    for interface in request["interfaces"]:
        paths.extend(str(p) for p in (Path("/sys/class/net") / interface / "statistics").glob("*"))
    paths.extend(str(p) for p in Path("/sys/devices/system/node").glob("node*/numastat"))
    paths.extend(str(p) for p in Path("/sys/devices/system/node").glob("node*/meminfo"))
    emit({"ready": True, "time": time.time(), "requested_period_seconds": 1})
    deadline = time.monotonic() + request["seconds"]
    previous = {}
    previous_start = None
    while time.monotonic() < deadline:
        started = time.monotonic()
        epoch = time.time()
        start_gap = started - previous_start if previous_start is not None else None
        previous_start = started
        processes = {}
        for directory in Path("/proc").glob("[0-9]*"):
            name = read(directory / "comm").get("value", "").strip()
            if directory.name == str(os.getpid()) or name in {
                "iperf3",
                "ovs-vswitchd",
                "ovsdb-server",
                "ovn-controller",
                "ping",
                "ceph-osd",
            }:
                keys = ("comm", "stat", "status")
                if name != "ceph-osd":
                    keys += ("numa_maps",)
                processes[directory.name] = {key: read(directory / key) for key in keys}
        files = {p: read(p) for p in paths}
        interrupts = interrupt_counters(read("/proc/interrupts"))
        observations = network_observations(request)
        current = {}
        for path in ("/proc/net/snmp", "/proc/net/netstat"):
            parsed = protocol_counters(files[path].get("value", ""))
            if "unavailable" in files[path]:
                parsed["unavailable"] = files[path]["unavailable"]
            observations[path] = parsed
            if not parsed["errors"] and "value" in files[path]:
                current[path] = parsed["values"]
        for name, result in observations.items():
            if "counters" in result:
                current[name] = result["counters"]
        deltas = {
            name: counter_delta(previous.get(name, {}), values) for name, values in current.items()
        }
        # A failed read breaks continuity: the next success is a new observation.
        previous = current
        elapsed = time.monotonic() - started
        emit(
            {
                "time": epoch,
                "monotonic": started,
                "collection_seconds": elapsed,
                "previous_start_gap_seconds": start_gap,
                "requested_period_seconds": 1,
                "overrun_seconds": max(0, elapsed - 1, (start_gap or 1) - 1),
                "clock_ticks": os.sysconf("SC_CLK_TCK"),
                "files": files,
                "interrupts": interrupts,
                "processes": processes,
                "observer_pid": os.getpid(),
                "network": observations,
                "network_counter_deltas": deltas,
                "memory_bandwidth": {"unavailable": "Requires platform-specific PMU collector"},
                "hardware_queue_occupancy": {
                    "unavailable": "No generic ethtool metric; queue counters and qdisc backlog "
                    "are recorded without claiming hardware ring occupancy"
                },
            }
        )
        # Account for JSON encoding and SSH backpressure when pacing the next sample.
        remaining = max(0, min(1 - (time.monotonic() - started), deadline - time.monotonic()))
        if select.select([sys.stdin], [], [], remaining)[0]:
            break


def bound(request, argv):
    return [
        "numactl",
        "--physcpubind=" + ",".join(map(str, request["cpus"])),
        "--membind=" + str(request["memory_node"]),
        *argv,
    ]


def server(request):
    identity(request["uuid"], request.get("identity_kind", "dmi"))
    argv = bound(
        request,
        [
            "iperf3",
            "-s",
            "-1",
            "-J",
            "-B",
            request["address"],
            "--bind-dev",
            request["interface"],
            "-p",
            str(request["port"]),
        ],
    )
    free = command(["ss", "-lntH", "sport", "=", f":{request['port']}"])
    if free.get("returncode") != 0 or free["stdout"].strip():
        raise ValueError("Benchmark port is already occupied")
    # Temporary files avoid a pipe filling while the client is still running.
    with tempfile.TemporaryFile(mode="w+") as out, tempfile.TemporaryFile(mode="w+") as err:
        process = subprocess.Popen(argv, stdout=out, stderr=err, text=True)
        try:
            deadline = time.monotonic() + 10
            ready = False
            while process.poll() is None and time.monotonic() < deadline:
                listeners = command(["ss", "-lntpH", "sport", "=", f":{request['port']}"])
                if (
                    listeners.get("returncode") == 0
                    and f"pid={process.pid}," in listeners["stdout"]
                ):
                    # Require this process, not a concurrent listener, to own the socket.
                    ready = True
                    break
                time.sleep(0.1)
            if not ready:
                raise RuntimeError("iperf3 server did not become ready")
            emit({"ready": True, "time": time.time()})
            deadline = time.monotonic() + request["seconds"]
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([sys.stdin], [], [], 0.2)[0]:
                    break
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            out.seek(0)
            err.seek(0)
            emit({"returncode": process.returncode, "stdout": out.read(), "stderr": err.read()})
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def client(request):
    identity(request["uuid"], request.get("identity_kind", "dmi"))
    argv = bound(request, request["argv"])
    started = time.time()
    # Probe RTT is ICMP RTT, not application latency or one-way delay.
    probe = [
        "ping",
        "-n",
        "-D",
        "-I",
        request["address"],
        "-i",
        "0.2",
        "-w",
        str(request["seconds"]),
        request["peer"],
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        traffic = pool.submit(command, argv, request["seconds"] + 15)
        latency = pool.submit(command, bound(request, probe), request["seconds"] + 5)
        return {"started": started, "traffic": traffic.result(), "latency": latency.result()}


def emit(value):
    print(json.dumps(value), flush=True)


def main():
    request = json.loads(sys.stdin.readline())
    # Controller supplies validated values; independently reject unsupported operations.
    operations = {
        "clock": lambda r: {"time": time.time()},
        "capture": capture,
        "check": check,
        "client": client,
        "ports": lambda r: command(["ss", "-lntH", "sport", "=", f":{r['port']}"]),
    }
    if request["operation"] == "sample":
        sample(request)
    elif request["operation"] == "server":
        server(request)
    else:
        emit(operations[request["operation"]](request))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        emit({"error": str(error)})
        sys.exit(1)
