"""Native logical gateway /32 BGP and provider EVPN transport and recovery."""

import json
import os
import re
import selectors
import subprocess
import tempfile
import time
from contextlib import ExitStack
from itertools import pairwise
from pathlib import Path

import pytest
from maas_bird.runtime import write_atomic

PROJECT = "maas-bird-network-lab-tf"
TORS = ("r1a", "r1b", "r2a", "r2b")


def execute(node, *args):
    return subprocess.check_output(
        ["lxc", "exec", "--project", PROJECT, node, "--", *args], text=True, timeout=30
    )


@pytest.fixture(scope="module", autouse=True)
def owned_gateways():
    for rack in (1, 2):
        row = json.loads(
            subprocess.check_output(
                ["lxc", "query", f"/1.0/instances/gateway-r{rack}?project={PROJECT}"],
                text=True,
                timeout=30,
            )
        )
        assert row["type"] == "virtual-machine"
        assert row["status"] == "Running"
        assert row["config"]["user.maas-bird-owner"] == "network-lab"
        assert row["config"]["limits.cpu"] == "1"
        assert row["config"]["limits.memory"] == "2GiB"


@pytest.mark.parametrize("rack", (1, 2))
def test_native_gateway_hwe_and_strict_boot(rack):
    node = f"gateway-r{rack}"
    release = execute(node, "uname", "-r").strip()
    assert not release.startswith("6.8.")
    depends = execute(node, "dpkg-query", "-W", "-f=${Depends}", "linux-image-generic-hwe-24.04")
    assert f"linux-image-{release}" in depends
    status = json.loads(execute(node, "cloud-init", "status", "--format=json"))
    assert status["status"] == "done"
    assert status["extended_status"] == "done"
    assert status["errors"] == []
    assert status["recoverable_errors"] == {}


@pytest.mark.parametrize("rack", (1, 2))
def test_native_gateway_ipv6_bgp_and_ipv4_packets(rack):
    node, remote = f"gateway-r{rack}", 3 - rack
    peers = json.loads(execute(node, "vtysh", "-c", "show bgp summary json"))["ipv4Unicast"][
        "peers"
    ]
    assert set(peers) == {"fe80::1"}
    assert peers["fe80::1"]["state"] == "Established"
    assert peers["fe80::1"]["remoteAs"] == 65101 + (rack - 1) * 2
    assert peers["fe80::1"]["localAs"] == 4200000100 + rack
    address = json.loads(execute(node, "ip", "-j", "address", "show", "dev", "fast0.3404"))[0]
    assert not any(item["family"] == "inet" for item in address["addr_info"])
    destination = f"10.205.251.{remote}"
    routes = json.loads(execute(node, "ip", "-j", "route", "show", f"{destination}/32"))
    assert len(routes) == 1
    assert routes[0]["protocol"] == "bgp"
    assert routes[0]["via"] == {"family": "inet6", "host": "fe80::1"}
    assert routes[0]["dev"] == "fast0.3404"
    execute(node, "ping", "-n", "-c", "3", "-W", "2", "-I", f"10.205.251.{rack}", destination)
    patch = "patch-nlab-tf-localnet-to-br-int"
    assert set(execute(node, "ovs-vsctl", "list-ports", "br-provider").split()) == {
        "fast0.3405",
        patch,
    }
    assert execute(node, "ovs-vsctl", "get", "Interface", patch, "type").strip() == "patch"
    peer = execute(node, "ovs-vsctl", "get", "Interface", patch, "options:peer").strip().strip('"')
    assert execute(node, "ovs-vsctl", "iface-to-br", peer).strip() == "br-int"


@pytest.mark.parametrize("node", TORS)
def test_native_provider_evpn_vni_and_kernel_flood_entries(node):
    index = TORS.index(node)
    vni = json.loads(execute(node, "vtysh", "-c", "show evpn vni 3405 json"))
    assert vni["type"] == "L2"
    assert vni["bridge"] == "br-provider"
    assert vni["vxlanInterface"] == "vx-provider"
    ports = json.loads(execute(node, "ip", "-j", "link", "show", "master", "br-provider"))
    assert {port["ifname"] for port in ports} == {"fast0.3405", "vx-provider"}
    assert vni["vtepIp"] == f"10.205.250.{11 + index}"
    expected = {f"10.205.250.{11 + other}" for other in range(4) if other != index}
    assert {peer["ip"] for peer in vni["remoteVteps"]} == expected
    assert all(peer["flood"] == "HER" for peer in vni["remoteVteps"])
    entries = json.loads(execute(node, "bridge", "-j", "fdb", "show", "dev", "vx-provider"))
    assert {entry["dst"] for entry in entries if entry["mac"] == "00:00:00:00:00:00"} == expected


@pytest.fixture
def provider_endpoints():
    created = []
    try:
        for rack in (1, 2):
            node, address = f"gateway-r{rack}", f"192.0.2.{rack * 10}/24"
            link = json.loads(execute(node, "ip", "-j", "address", "show", "dev", "br-provider"))[
                0
            ]
            assert not any(item["family"] == "inet" for item in link["addr_info"])
            execute(node, "ip", "address", "add", address, "dev", "br-provider")
            created.append((node, address))
            execute(node, "ip", "route", "flush", "cache")
        yield
    finally:
        for node, address in reversed(created):
            execute(node, "ip", "address", "delete", address, "dev", "br-provider")


def test_native_provider_packets_full_mtu_and_mac_routes(provider_endpoints):
    nodes = ("spine0", "spine1", *TORS)
    before = {
        node: json.loads(execute(node, "nstat", "-az", "-j", "IpFragCreates"))["kernel"][
            "IpFragCreates"
        ]
        for node in nodes
    }
    for rack in (1, 2):
        destination = f"192.0.2.{(3 - rack) * 10}"
        for size in (1400, 1472):
            execute(
                f"gateway-r{rack}",
                "ping",
                "-n",
                "-c",
                "3",
                "-W",
                "2",
                "-M",
                "do",
                "-s",
                str(size),
                "-I",
                "br-provider",
                destination,
            )
        tor = f"r{rack}a"
        mac, remote_vtep = f"02:00:00:ee:0{3 - rack}:01", f"10.205.250.{11 + (2 - rack) * 2}"
        entries = json.loads(execute(tor, "bridge", "-j", "fdb", "show", "dev", "vx-provider"))
        assert any(
            entry["mac"] == mac
            and entry.get("dst") == remote_vtep
            and "extern_learn" in entry["flags"]
            for entry in entries
        )
        records = json.loads(
            execute(tor, "vtysh", "-c", "show bgp l2vpn evpn route type macip json")
        )
        paths = [
            path
            for rd in records.values()
            if isinstance(rd, dict)
            for route in rd.values()
            if isinstance(route, dict)
            for group in route.get("paths", [])
            for path in group
        ]
        matching = [path for path in paths if path.get("mac") == mac]
        assert matching
        assert all(path["routeType"] == 2 for path in matching)
        assert all("RT:65000:3405" in path["extendedCommunity"]["string"] for path in matching)
        assert any(hop["ip"] == remote_vtep for path in matching for hop in path["nexthops"])

    after = {
        node: json.loads(execute(node, "nstat", "-az", "-j", "IpFragCreates"))["kernel"][
            "IpFragCreates"
        ]
        for node in nodes
    }
    assert after == before


@pytest.mark.parametrize("rack", (1, 2))
def test_native_gateway_lldp_receive_only(rack):
    configuration = execute(
        f"gateway-r{rack}", "lldpcli", "-f", "keyvalue", "show", "configuration"
    )
    assert "configuration.config.rx-only=yes\n" in configuration
    assert "configuration.config.iface-pattern=fast0\n" in configuration


def wait_provider_health(tor, peer, fault, disabled):
    """Require the surviving control plane, kernel path and actual packets."""
    deadline = time.monotonic() + 30
    attempts = []
    while True:
        try:
            summary = json.loads(execute(tor, "vtysh", "-c", "show bgp vrf all summary json"))
            evpn = json.loads(execute(tor, "vtysh", "-c", "show bgp l2vpn evpn summary json"))
            active = {"spine0", "spine1"} - ({peer} if disabled else set())
            assert {
                name for name, row in evpn["peers"].items() if row["state"] == "Established"
            } == active
            for vrf, suffix in (("default", ""), ("access", ".3420"), ("replication", ".3430")):
                peers = summary[vrf]["ipv4Unicast"]["peers"]
                expected = active if fault == "physical" else {"spine0", "spine1"}
                assert {
                    name
                    for name in ("spine0", "spine1")
                    if peers[name + suffix]["state"] == "Established"
                } == expected
            destination = "10.205.250.13/32" if tor == "r1a" else "10.205.250.11/32"
            routes = json.loads(execute(tor, "ip", "-j", "route", "show", destination))
            assert len(routes) == 1
            hops = routes[0].get("nexthops", [routes[0]])
            expected = active if fault == "physical" else {"spine0", "spine1"}
            assert {hop["dev"] for hop in hops} == expected
            packets = {}
            for rack in (1, 2):
                packets[str(rack)] = execute(
                    f"gateway-r{rack}",
                    "ping",
                    "-n",
                    "-c",
                    "3",
                    "-i",
                    "0.2",
                    "-W",
                    "1",
                    "-M",
                    "do",
                    "-s",
                    "1472",
                    "-I",
                    "br-provider",
                    f"192.0.2.{(3 - rack) * 10}",
                )
                assert "3 packets transmitted, 3 received" in packets[str(rack)]
            return {"bgp": summary, "evpn": evpn, "route": routes, "packets": packets}
        except (AssertionError, subprocess.CalledProcessError) as error:
            attempts.append(str(error))
            if time.monotonic() >= deadline:
                raise AssertionError(f"Provider did not recover: {attempts[-3:]}") from error
            time.sleep(0.2)


@pytest.mark.parametrize("tor", ("r1a", "r2a"))
@pytest.mark.parametrize("peer", ("spine0", "spine1"))
@pytest.mark.parametrize("fault", ("physical", "evpn"))
def test_native_provider_spine_and_evpn_peer_recovery(provider_endpoints, tor, peer, fault):
    row = json.loads(
        subprocess.check_output(
            ["lxc", "query", f"/1.0/instances/{tor}?project={PROJECT}"],
            text=True,
            timeout=30,
        )
    )
    assert row["status"] == "Running" and row["type"] == "container"
    assert row["config"]["user.maas-bird-owner"] == "network-lab"
    assert row["config"]["limits.cpu"] == "1"
    assert row["config"]["limits.memory"] == "768MiB"
    assert row["expanded_config"].get("security.privileged", "false") == "false"
    asn = 65101 if tor == "r1a" else 65103
    filter_script = """vtysh -c 'show running-config' | awk -v peer="$1" '
    $1 == "address-family" { inside = ($2 == "l2vpn" && $3 == "evpn") }
    $1 == "exit-address-family" { inside = 0 }
    inside && $1 == "neighbor" && $2 == peer {
        if ($3 == "password") exit 1
        sub(/^[[:space:]]+/, ""); print
    }'"""

    def af_lines():
        return execute(tor, "sh", "-ceu", filter_script, "filter", peer).splitlines()

    original = af_lines()
    assert f"neighbor {peer} activate" in original
    assert f"neighbor {peer} route-map LAB-PROVIDER-EVPN in" in original
    assert f"neighbor {peer} route-map LAB-PROVIDER-EVPN out" in original
    assert f"neighbor {peer} maximum-prefix 128" in original
    receipt = {"tor": tor, "peer": peer, "fault": fault, "flows": [], "original_af": original}
    processes = []
    captures = []
    files = ExitStack()

    def change(disable):
        if fault == "physical":
            execute(tor, "ip", "link", "set", peer, "down" if disable else "up")
        else:
            execute(
                tor,
                "vtysh",
                "-c",
                "configure terminal",
                "-c",
                f"router bgp {asn}",
                "-c",
                "address-family l2vpn evpn",
                *[
                    argument
                    for line in ([f"no neighbor {peer} activate"] if disable else original)
                    for argument in ("-c", line)
                ],
                "-c",
                "end",
            )

    receipt["before"] = wait_provider_health(tor, peer, fault, False)
    try:
        for rack in (1, 2):
            output = files.enter_context(tempfile.TemporaryFile())
            errors = files.enter_context(tempfile.TemporaryFile())
            captures.append((output, errors))
            processes.append(
                subprocess.Popen(
                    [
                        "lxc",
                        "exec",
                        "--project",
                        PROJECT,
                        f"gateway-r{rack}",
                        "--",
                        "stdbuf",
                        "-oL",
                        "ping",
                        "-n",
                        "-D",
                        "-O",
                        "-w",
                        "30",
                        "-i",
                        "0.1",
                        "-W",
                        "1",
                        "-M",
                        "do",
                        "-s",
                        "1472",
                        "-I",
                        "br-provider",
                        f"192.0.2.{(3 - rack) * 10}",
                    ],
                    text=True,
                    stdout=output,
                    stderr=errors,
                )
            )
        ready = time.monotonic() + 10
        while True:
            traces = [os.pread(output.fileno(), 1_000_000, 0).decode() for output, _ in captures]
            if all(len(re.findall(r"bytes from .*: icmp_seq=", trace)) >= 3 for trace in traces):
                break
            assert all(process.poll() is None for process in processes)
            assert time.monotonic() < ready, traces
            time.sleep(0.1)
        receipt["baseline_traces"] = traces
        receipt["baseline_last_sequences"] = [
            int(re.findall(r"bytes from .*: icmp_seq=(\d+)", trace)[-1]) for trace in traces
        ]
        receipt["fault_before"] = time.time()
        change(True)
        receipt["fault_after"] = time.time()
        receipt["surviving"] = wait_provider_health(tor, peer, fault, True)
        receipt["forwarding_observed"] = time.time()
        receipt["surviving_last_sequences"] = [
            int(
                re.findall(
                    r"bytes from .*: icmp_seq=(\d+)",
                    os.pread(output.fileno(), 1_000_000, 0).decode(),
                )[-1]
            )
            for output, _ in captures
        ]
        for rack, process in enumerate(processes, 1):
            process.communicate(timeout=40)
            output_file, error_file = captures[rack - 1]
            trace = os.pread(output_file.fileno(), 1_000_000, 0).decode()
            errors = os.pread(error_file.fileno(), 1_000_000, 0).decode()
            rows = [
                (float(stamp), int(sequence))
                for stamp, sequence in re.findall(
                    r"(?m)^\[(\d+\.\d+)\].* bytes from .*: icmp_seq=(\d+)",
                    trace,
                )
            ]
            counts = re.search(r"(\d+) packets transmitted, (\d+) received", trace)
            flow = {
                "rack": rack,
                "trace": trace,
                "stderr": errors,
                "returncode": process.returncode,
            }
            receipt["flows"].append(flow)
            assert counts is not None
            flow.update(transmitted=int(counts[1]), received=int(counts[2]))
            assert process.returncode == 0 and len(rows) >= 20
            assert rows[0][1] <= receipt["baseline_last_sequences"][rack - 1]
            assert rows[-1][0] - rows[0][0] >= 25
            assert (
                sum(
                    sequence > receipt["surviving_last_sequences"][rack - 1]
                    for _, sequence in rows
                )
                >= 5
            )
            flow["max_inter_reply_gap_seconds"] = max(b[0] - a[0] for a, b in pairwise(rows))
            flow["duplicate_replies"] = len(rows) - len({sequence for _, sequence in rows})
            flow["reordered_replies"] = sum(b[1] < a[1] for a, b in pairwise(rows))
            assert flow["duplicate_replies"] == flow["reordered_replies"] == 0
        receipt["fault_assertions_passed"] = True
    finally:
        try:
            change(False)
            receipt["restored"] = wait_provider_health(tor, peer, fault, False)
            assert af_lines() == original
            receipt["restored_at"] = time.time()
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=5)
            directory = (
                Path(__file__).resolve().parents[2] / "runtime/terraform/acceptance/provider"
            )
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                write_atomic(
                    directory / f"{tor}-{peer}-{fault}-{time.time_ns()}.json",
                    json.dumps(receipt, indent=2) + "\n",
                    0o600,
                )
            finally:
                files.close()


def provider_capture(node, interface, expression, seconds):
    return subprocess.Popen(
        [
            "lxc",
            "exec",
            "--project",
            PROJECT,
            node,
            "--",
            "timeout",
            str(seconds),
            "tcpdump",
            "-n",
            "-l",
            "-vv",
            "-i",
            interface,
            expression,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def wait_capture_ready(process):
    assert process.stderr is not None
    with selectors.DefaultSelector() as ready:
        ready.register(process.stderr, selectors.EVENT_READ)
        deadline = time.monotonic() + 10
        startup = ""
        while True:
            assert time.monotonic() < deadline
            if not ready.select(timeout=1):
                continue
            chunk = os.read(process.stderr.fileno(), 4096)
            assert chunk, process.poll()
            startup += chunk.decode()
            if "listening on" in startup:
                return startup


def test_native_provider_vxlan_packet_capture(provider_endpoints):
    captures = []
    traces = []
    outputs = []
    try:
        for peer in ("spine0", "spine1"):
            captures.append(provider_capture("r1a", peer, "udp port 4789", 8))
        for process in captures:
            wait_capture_ready(process)
        execute(
            "gateway-r1",
            "ping",
            "-n",
            "-M",
            "do",
            "-s",
            "1472",
            "-c",
            "3",
            "-i",
            "0.2",
            "-W",
            "1",
            "-I",
            "br-provider",
            "192.0.2.20",
        )
        for process in captures:
            output, errors = process.communicate(timeout=15)
            outputs.append(output)
            traces.append({"trace": output, "stderr": errors, "returncode": process.returncode})
            assert process.returncode == 124
        packets = [
            packet
            for output in outputs
            for packet in re.findall(
                r"(?ms)^\d\d:\d\d:\d\d\.\d+.*?(?=^\d\d:\d\d:\d\d\.\d+|\Z)",
                output,
            )
        ]
        assert any(
            "10.205.250.11." in packet
            and "10.205.250.13.4789" in packet
            and "vni 3405" in packet
            and "192.0.2.10 > 192.0.2.20" in packet
            and "ICMP echo request" in packet
            for packet in packets
        ), traces
    finally:
        for process in captures:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=15)
        directory = Path(__file__).resolve().parents[2] / "runtime/terraform/acceptance/provider"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_atomic(
            directory / f"vxlan-capture-{time.time_ns()}.json",
            json.dumps(traces, indent=2) + "\n",
            0o600,
        )


def test_native_provider_broadcast_isolated_from_routed_roles(provider_endpoints):
    link = json.loads(execute("gateway-r1", "ip", "-j", "link", "show", "br-provider"))[0]
    captures = []
    traces = []
    try:
        for node in TORS:
            for vlan in range(3400, 3406):
                interface = f"fast0.{vlan}"
                process = provider_capture(node, interface, f"ether src {link['address']}", 20)
                captures.append((node, interface, vlan == 3405, process))
        for _, _, _, process in captures:
            wait_capture_ready(process)
        assert all(process.poll() is None for _, _, _, process in captures)
        result = subprocess.run(
            [
                "lxc",
                "exec",
                "--project",
                PROJECT,
                "gateway-r1",
                "--",
                "ping",
                "-b",
                "-n",
                "-c",
                "3",
                "-i",
                "0.2",
                "-W",
                "1",
                "-M",
                "do",
                "-s",
                "1472",
                "-I",
                "br-provider",
                "192.0.2.255",
            ],
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert result.returncode in (0, 1), result.stderr
        assert "3 packets transmitted" in result.stdout
        for node, interface, provider, process in captures:
            output, errors = process.communicate(timeout=20)
            traces.append(
                {
                    "node": node,
                    "interface": interface,
                    "provider": provider,
                    "source_mac": link["address"],
                    "trace": output,
                    "stderr": errors,
                    "returncode": process.returncode,
                }
            )
            assert process.returncode == 124
            assert "0 packets dropped by kernel" in errors
            if provider:
                sequences = re.findall(
                    r"192\.0\.2\.10 > 192\.0\.2\.255: ICMP echo request, id \d+, seq (\d+)",
                    output,
                )
                assert sorted(sequences) == ["1", "2", "3"], output
            else:
                assert not output.strip(), (node, interface, output)
    finally:
        for _, _, _, process in captures:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=15)
        directory = Path(__file__).resolve().parents[2] / "runtime/terraform/acceptance/provider"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_atomic(
            directory / f"broadcast-isolation-{time.time_ns()}.json",
            json.dumps(traces, indent=2) + "\n",
            0o600,
        )
