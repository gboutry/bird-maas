"""Measure bounded native UDP flows and their captured Geneve egress paths.

Sixteen iperf3 streams are each capped at 50 kbit/s. No sysctl or routing
configuration is changed by this measurement.
"""

import argparse
import json
import re
import subprocess
import time

import deploy_ovn as lab


def start(name, *args):
    return subprocess.Popen(
        lab.ssh_args(name, *args), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


def captured_paths(text, source, target):
    pattern = re.compile(
        r"\S+ (?P<device>enp[78]s0\.3404) Out IP "
        + re.escape(source["vtep"])
        + r"\.(?P<outer>[0-9]+) > "
        + re.escape(target["vtep"])
        + r"\.6081: Geneve, .*: IP "
        + re.escape(source["address"])
        + r"\.(?P<inner>[0-9]+) > "
        + re.escape(target["address"])
        + r"\.5201: UDP, length (?P<length>[0-9]+)"
    )
    paths = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = pattern.fullmatch(line)
        if match is None:
            raise ValueError("Unexpected packet in the Geneve source/path capture: " + line)
        row = match.groupdict()
        if int(row["length"]) not in (4, 256):
            raise ValueError("Unexpected iperf3 UDP payload size")
        flow = paths.setdefault(
            row["inner"], {"devices": set(), "outer_ports": set(), "packets": 0}
        )
        flow["devices"].add(row["device"])
        flow["outer_ports"].add(int(row["outer"]))
        flow["packets"] += 1
    return {
        port: {
            "devices": sorted(row["devices"]),
            "outer_ports": sorted(row["outer_ports"]),
            "packets": row["packets"],
        }
        for port, row in paths.items()
    }


def measure(source_name, target_name, output, expected_paths):
    identities = lab.owned()
    lab.require_source_guards()
    hosts = json.loads((lab.ROOT / "runtime/ovn/manifest.json").read_text())
    if any(h["uuid"] != identities[name] for name, h in hosts.items()):
        raise ValueError("OVN manifest differs from native owned workers")
    source, target = hosts[source_name], hosts[target_name]
    result = {
        "started": time.time(),
        "source": source_name,
        "target": target_name,
        "source_kernel": lab.ssh(source_name, "uname", "-r"),
        "target_kernel": lab.ssh(target_name, "uname", "-r"),
        "hash_policy": lab.ssh(source_name, "cat", "/proc/sys/net/ipv4/fib_multipath_hash_policy"),
        "streams": 16,
        "bitrate_per_stream": 50000,
        "duration_seconds": 5,
    }
    processes = []
    try:
        if lab.ssh(
            target_name,
            "sudo",
            "ip",
            "netns",
            "exec",
            "nlab-tenant",
            "ss",
            "-lntH",
            "sport = :5201",
        ):
            raise RuntimeError("The owned measurement port already has a listener")
        server = start(
            target_name,
            "sudo",
            "timeout",
            "25",
            "ip",
            "netns",
            "exec",
            "nlab-tenant",
            "iperf3",
            "-s",
            "-1",
            "-J",
            "-B",
            target["address"],
            "-p",
            "5201",
        )
        processes.append(server)
        deadline = time.monotonic() + 10
        while True:
            listening = lab.ssh(
                target_name,
                "sudo",
                "ip",
                "netns",
                "exec",
                "nlab-tenant",
                "ss",
                "-lntH",
                "sport = :5201",
            )
            if listening:
                break
            if server.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("Native bounded iperf3 server did not become ready")
            time.sleep(0.1)
        indices = [
            json.loads(lab.ssh(source_name, "ip", "-j", "link", "show", "dev", device))[0][
                "ifindex"
            ]
            for device in ("enp7s0.3404", "enp8s0.3404")
        ]
        result["capture_ifindices"] = indices
        capture = start(
            source_name,
            "sudo",
            "timeout",
            "15",
            "tcpdump",
            "-n",
            "-l",
            "-i",
            "any",
            "-c",
            "512",
            f"(ifindex {indices[0]} or ifindex {indices[1]}) and udp dst port 6081 "
            f"and src host {source['vtep']} and dst host {target['vtep']} "
            "and geneve and udp dst port 5201",
        )
        processes.append(capture)
        assert capture.stderr is not None
        while True:
            line = capture.stderr.readline()
            if not line:
                raise RuntimeError("Native Geneve capture exited before readiness")
            if "listening on any" in line:
                break
        client = lab.ssh(
            source_name,
            "sudo",
            "ip",
            "netns",
            "exec",
            "nlab-tenant",
            "iperf3",
            "-u",
            "-P",
            "16",
            "-b",
            "50K",
            "-l",
            "256",
            "-t",
            "5",
            "-J",
            "--get-server-output",
            "--connect-timeout",
            "3000",
            "-B",
            source["address"],
            "-c",
            target["address"],
            "-p",
            "5201",
            timeout=30,
        )
        result["client"] = json.loads(client)
        stdout, stderr = server.communicate(timeout=15)
        result["server"] = json.loads(stdout)
        result["server_stderr"] = stderr
        if server.returncode != 0:
            raise RuntimeError("Native iperf3 server failed")
        stdout, stderr = capture.communicate(timeout=15)
        result["capture"] = {"stdout": stdout, "stderr": stderr, "exit": capture.returncode}
        if capture.returncode != 0:
            raise RuntimeError("Geneve capture did not collect the declared packet count")
        result["paths"] = captured_paths(stdout, source, target)
        ports = {str(c["local_port"]) for c in result["client"]["start"]["connected"]}
        if len(ports) != 16 or set(result["paths"]) != ports:
            raise RuntimeError("Captured flows differ from the native iperf3 stream set")
        if any(
            len(p["devices"]) != 1 or len(p["outer_ports"]) != 1 for p in result["paths"].values()
        ):
            raise RuntimeError("A stable flow changed its observed egress or Geneve source port")
        statistics = [s["udp"] for s in result["server"]["end"]["streams"]]
        if len(statistics) != 16 or any(
            s["packets"] <= 0 or s["lost_packets"] != 0 or s["out_of_order"] != 0
            for s in statistics
        ):
            raise RuntimeError("Native receiver reported missing, lost or reordered UDP packets")
        result["devices"] = sorted({d for p in result["paths"].values() for d in p["devices"]})
        if len(result["devices"]) != expected_paths:
            raise RuntimeError("Observed Geneve egress count differs from the declared path count")
        result["completed"] = time.time()
        print(
            json.dumps({key: result[key] for key in ("hash_policy", "devices", "paths")}, indent=2)
        )
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
                proc.communicate(timeout=30)
        output.write_text(json.dumps(result, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=lab.WORKERS, default="probe")
    parser.add_argument("--target", choices=lab.WORKERS, default="probe-r2a")
    parser.add_argument("--name", required=True, help="Unique public measurement label")
    parser.add_argument("--expect-paths", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    if args.source == args.target or re.fullmatch(r"[a-z0-9-]+", args.name) is None:
        raise ValueError("Use different workers and a lowercase measurement label")
    output = lab.ROOT / "runtime/geneve-flows"
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = output / (args.name + ".json")
    if target.exists():
        raise FileExistsError("Preserve existing measurement evidence; use a new label")
    measure(args.source, args.target, target, args.expect_paths)


if __name__ == "__main__":
    main()
