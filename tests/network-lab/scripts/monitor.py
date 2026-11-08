"""Record host resources; stop only owned lab instances at the thermal ceiling.

Run with Python on the LXD host. Output is JSONL, without credentials. The
project CPU limit bounds allocated CPUs, not all activity on this laptop.
"""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

PROJECT = "maas-bird-network-lab"


def command(*args):
    return subprocess.check_output(args, text=True, timeout=30)


def cpu_times():
    fields = [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    return sum(fields), fields[3] + fields[4]


def temperature_sensors(root=Path("/sys")):
    sensors = {}
    for zone in (root / "class/thermal").glob("thermal_zone*"):
        try:
            label = (zone / "type").read_text().strip()
            sensors[f"thermal/{zone.name}/{label}"] = int((zone / "temp").read_text()) / 1000
        except OSError:
            continue
    for device in (root / "class/hwmon").glob("hwmon*"):
        try:
            driver = (device / "name").read_text().strip()
            if driver not in {"k10temp", "coretemp", "zenpower"}:
                continue
            for sensor in device.glob("temp*_input"):
                label_file = sensor.with_name(sensor.name.replace("_input", "_label"))
                label = label_file.read_text().strip() if label_file.exists() else sensor.name
                sensors[f"cpu/{device.name}/{driver}/{label}"] = int(sensor.read_text()) / 1000
        except OSError:
            continue
    return sensors


def lab_resources(project=PROJECT):
    """Read QEMU host processes; guest-agent counters can be zero during PXE."""
    instances = json.loads(
        command("lxc", "query", f"/1.0/instances?project={project}&recursion=2")
    )
    result = {}
    for instance in instances:
        if instance["status"] != "Running" or instance["type"] != "virtual-machine":
            continue
        pid = instance["state"]["pid"]
        try:
            status = (Path("/proc") / str(pid) / "status").read_text()
            rss = next(
                int(s.split()[1]) * 1024 for s in status.splitlines() if s.startswith("VmRSS:")
            )
            stat = (Path("/proc") / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()
            result[instance["name"]] = {
                "pid": pid,
                "rss_bytes": rss,
                "cpu_seconds": (int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK"),
            }
        except (OSError, StopIteration):
            # The process may exit during a sample; never report it as zero usage.
            result[instance["name"]] = {"pid": pid, "unavailable": True}
    return result


def project_exists(project):
    projects = json.loads(command("lxc", "query", "/1.0/projects"))
    if not isinstance(projects, list) or any(
        not isinstance(url, str) or not url.startswith("/1.0/projects/") or url == "/1.0/projects/"
        for url in projects
    ):
        raise ValueError("Invalid LXD project inventory")
    return f"/1.0/projects/{quote(project, safe='')}" in projects


def stop_owned(project=PROJECT):
    if not project_exists(project):
        return
    try:
        instances = json.loads(command("lxc", "list", "--project", project, "--format=json"))
    except subprocess.CalledProcessError:
        if not project_exists(project):
            return
        raise
    running = [instance for instance in instances if instance["status"] == "Running"]
    if any(i["config"].get("user.maas-bird-owner") != "network-lab" for i in running):
        raise RuntimeError("Refusing to stop instance without lab ownership")
    for instance in running:
        command("lxc", "stop", "--project", project, instance["name"], "--force")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument("--ceiling-c", type=float, default=75)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument(
        "--latch-stop",
        action="store_true",
        help="After a thermal trip, keep stopping newly started owned instances until restarted",
    )
    args = parser.parse_args()
    if args.interval <= 0 or not 60 <= args.ceiling_c <= 90:
        parser.error("interval must be positive and ceiling must be 60..90 C")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    previous = cpu_times()
    previous_lab = {}
    previous_time = time.monotonic()
    thermal_latched = False
    with args.output.open("a", buffering=1) as output:
        while True:
            time.sleep(args.interval)
            current = cpu_times()
            total, idle = (current[i] - previous[i] for i in (0, 1))
            previous = current
            memory = dict(
                (line.split()[0].rstrip(":"), int(line.split()[1]))
                for line in Path("/proc/meminfo").read_text().splitlines()
            )
            sensors = temperature_sensors()
            temps = list(sensors.values())
            over_ceiling = bool(temps and max(temps) >= args.ceiling_c)
            thermal_latched = thermal_latched or over_ceiling
            now = time.monotonic()
            try:
                lab = lab_resources(args.project)
                lab_error = None
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                lab = {}
                lab_error = type(exc).__name__
            cpu_deltas = [
                max(0, sample["cpu_seconds"] - previous_lab[name]["cpu_seconds"])
                for name, sample in lab.items()
                if "cpu_seconds" in sample
                and name in previous_lab
                and "cpu_seconds" in previous_lab[name]
                and previous_lab[name]["pid"] == sample["pid"]
            ]
            record = {
                "project": args.project,
                "timestamp": time.time(),
                "host_cpu_percent": 100 * (total - idle) / total if total else 0,
                "host_used_kib": memory["MemTotal"] - memory["MemAvailable"],
                "host_available_kib": memory["MemAvailable"],
                "temperatures_c": temps,
                "temperature_sensors_c": sensors,
                "thermal_stop": thermal_latched if args.latch_stop else over_ceiling,
                "thermal_latched": args.latch_stop and thermal_latched,
                "lab_qemu": lab,
                "lab_qemu_cpu_cores": sum(cpu_deltas) / (now - previous_time)
                if cpu_deltas
                else None,
                "lab_sample_error": lab_error,
            }
            previous_lab = lab
            previous_time = now
            output.write(json.dumps(record) + "\n")
            if record["thermal_stop"]:
                stop_owned(args.project)
                if not args.latch_stop:
                    raise SystemExit("Lab stopped at thermal ceiling")


if __name__ == "__main__":
    main()
