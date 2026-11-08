"""Real isolated gateway-role NAT traffic; compute BIRD admission is separate."""

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from maas_bird.runtime import write_atomic
from test_native_frr_routing import core_service_health

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "maas-bird-network-lab-tf"
CLIENT = "gateway-r2"
CENTRAL = "maas"
ROUTER = "nlab-tf-router"


def run(node, *args, check=True):
    result = subprocess.run(
        ["lxc", "exec", "--project", PROJECT, node, "--", *args],
        capture_output=True,
        text=True,
        timeout=40,
    )
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def execute(node, *args):
    return run(node, *args).stdout


def nb(*args):
    return execute(CENTRAL, "ovn-nbctl", "--timeout=30", *args)


@pytest.fixture
def endpoint():
    state = ROOT / "runtime/terraform/lab/90-ovn/terraform.tfstate"
    values = {
        key: value["value"] for key, value in json.loads(state.read_text())["outputs"].items()
    }
    for node in ("gateway-r1", "gateway-r2", CENTRAL):
        instance = json.loads(
            subprocess.check_output(
                ["lxc", "query", f"/1.0/instances/{node}?project={PROJECT}"], text=True, timeout=30
            )
        )
        assert instance["status"] == "Running"
        assert instance["config"]["user.maas-bird-owner"] == "network-lab"
        if node != CENTRAL:
            assert instance["config"]["volatile.uuid"] == values["chassis"][node]["uuid"]
    token = uuid.uuid4().hex[:8]
    namespace, interface, port = f"nlab-nat-{token}", f"nlabt{token}", f"nlab-test-{token}"
    tenant, external, floating = "172.30.10.250", "192.0.2.20", "192.0.2.199"
    mac = "02:00:00:ab:00:fa"
    link = json.loads(execute(CLIENT, "ip", "-j", "address", "show", "dev", "br-provider"))[0]
    assert not any(row["family"] == "inet" for row in link["addr_info"])
    nat = json.loads(nb("--format=json", "--columns=external_ip", "list", "NAT"))["data"]
    assert [floating] not in nat
    try:
        execute(CLIENT, "ip", "netns", "add", namespace)
        execute(
            CLIENT,
            "ip",
            "link",
            "add",
            interface,
            "type",
            "veth",
            "peer",
            "name",
            "eth0",
            "netns",
            namespace,
        )
        execute(CLIENT, "ip", "link", "set", interface, "mtu", "1400", "up")
        execute(CLIENT, "ip", "-n", namespace, "link", "set", "lo", "up")
        execute(
            CLIENT,
            "ip",
            "-n",
            namespace,
            "link",
            "set",
            "eth0",
            "address",
            mac,
            "mtu",
            "1400",
            "up",
        )
        execute(CLIENT, "ip", "-n", namespace, "address", "add", f"{tenant}/24", "dev", "eth0")
        execute(CLIENT, "ip", "-n", namespace, "route", "add", "default", "via", "172.30.10.1")
        execute(
            CLIENT,
            "ovs-vsctl",
            "add-port",
            "br-int",
            interface,
            "--",
            "set",
            "Interface",
            interface,
            f"external_ids:iface-id={port}",
        )
        nb(
            "lsp-add",
            "nlab-tf-tenant",
            port,
            "--",
            "lsp-set-addresses",
            port,
            f"{mac} {tenant}",
            "--",
            "lsp-set-port-security",
            port,
            f"{mac} {tenant}",
            "--",
            "lsp-set-options",
            port,
            f"requested-chassis={values['chassis'][CLIENT]['uuid']}",
        )
        nb("lr-nat-add", ROUTER, "dnat_and_snat", floating, tenant)
        execute(CLIENT, "ip", "address", "add", f"{external}/24", "dev", "br-provider")
        nb("--wait=hv", "sync")
        route = json.loads(execute(CLIENT, "ip", "-n", namespace, "-j", "route", "get", external))[
            0
        ]
        assert route["dev"] == "eth0"
        assert route["gateway"] == "172.30.10.1"
        yield {
            "namespace": namespace,
            "tenant": tenant,
            "external": external,
            "floating": floating,
            "gateways": {
                name: values["chassis"][name]["uuid"] for name in ("gateway-r1", "gateway-r2")
            },
        }
    finally:
        nb("lr-nat-del", ROUTER, "dnat_and_snat", floating)
        nb("--if-exists", "lsp-del", port)
        execute(CLIENT, "ovs-vsctl", "--if-exists", "del-port", "br-int", interface)
        if run(CLIENT, "ip", "link", "show", "dev", interface, check=False).returncode == 0:
            execute(CLIENT, "ip", "link", "delete", interface)
        if namespace in execute(CLIENT, "ip", "netns", "list").split():
            execute(CLIENT, "ip", "netns", "delete", namespace)
        addresses = json.loads(
            execute(CLIENT, "ip", "-j", "address", "show", "dev", "br-provider")
        )[0]["addr_info"]
        if any(address.get("local") == external for address in addresses):
            execute(CLIENT, "ip", "address", "delete", f"{external}/24", "dev", "br-provider")


def capture(interface, expression):
    return subprocess.Popen(
        [
            "lxc",
            "exec",
            "--project",
            PROJECT,
            CLIENT,
            "--",
            "timeout",
            "8",
            "tcpdump",
            "-n",
            "-l",
            "-vv",
            "-i",
            interface,
            expression,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_native_gateway_nat_and_geneve_packets(endpoint):
    geneve = capture("fast0.3404", "udp port 6081")
    provider = capture("br-provider", f"icmp and host {endpoint['floating']}")
    try:
        time.sleep(0.5)
        output = execute(
            CLIENT,
            "ip",
            "netns",
            "exec",
            endpoint["namespace"],
            "ping",
            "-n",
            "-c",
            "3",
            "-i",
            "0.2",
            "-W",
            "2",
            "-M",
            "do",
            "-s",
            "1372",
            endpoint["external"],
        )
        assert "3 packets transmitted, 3 received" in output
        tunnel, tunnel_error = geneve.communicate(timeout=15)
        translated, provider_error = provider.communicate(timeout=15)
        record(endpoint, "geneve", {"trace": tunnel, "stderr": tunnel_error})
        record(endpoint, "provider", {"trace": translated, "stderr": provider_error})
        assert geneve.returncode == provider.returncode == 124
        assert "listening on" in tunnel_error
        assert "listening on" in provider_error
        packets = re.findall(r"(?ms)^\d\d:\d\d:\d\d\.\d+.*?(?=^\d\d:\d\d:\d\d\.\d+|\Z)", tunnel)
        matching = [
            packet
            for packet in packets
            if "10.205.251.2." in packet
            and "10.205.251.1.6081" in packet
            and "Geneve" in packet
            and "172.30.10.250 > 192.0.2.20" in packet
            and "ICMP echo request" in packet
        ]
        assert matching, tunnel
        assert "192.0.2.199 > 192.0.2.20" in translated, translated
        assert "172.30.10.250 > 192.0.2.20" not in translated
    finally:
        for process in (geneve, provider):
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=15)


def record(endpoint, label, values):
    directory = ROOT / "runtime/terraform/acceptance/ovn-nat"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_atomic(
        directory / f"{endpoint['namespace']}-{label}.json",
        json.dumps(values, indent=2) + "\n",
        0o600,
    )


def active_gateway():
    binding = json.loads(
        execute(
            CENTRAL,
            "ovn-sbctl",
            "--format=json",
            "--columns=chassis",
            "find",
            "Port_Binding",
            "logical_port=cr-nlab-tf-router-provider",
        )
    )["data"]
    if len(binding) != 1 or binding[0][0][0] != "uuid":
        return None
    return (
        execute(CENTRAL, "ovn-sbctl", "--if-exists", "get", "Chassis", binding[0][0][1], "name")
        .strip()
        .strip('"')
    )


def ping(endpoint, count=1):
    return run(
        CLIENT,
        "ip",
        "netns",
        "exec",
        endpoint["namespace"],
        "ping",
        "-n",
        "-c",
        str(count),
        "-i",
        "0.2",
        "-W",
        "1",
        endpoint["external"],
        check=False,
    )


def await_forwarding(endpoint, gateway, timeout):
    started = time.monotonic()
    attempts = []
    while True:
        result = ping(endpoint)
        active = active_gateway()
        attempts.append(
            {
                "elapsed": time.monotonic() - started,
                "returncode": result.returncode,
                "active": active,
                "output": result.stdout,
            }
        )
        if result.returncode == 0 and active == gateway:
            record(endpoint, f"convergence-{uuid.uuid4().hex[:8]}", {"attempts": attempts})
            return
        if time.monotonic() - started >= timeout:
            record(endpoint, f"failed-convergence-{uuid.uuid4().hex[:8]}", {"attempts": attempts})
            pytest.fail(f"Gateway forwarding did not converge within {timeout}s; active={active}")
        time.sleep(0.2)


@pytest.mark.parametrize("interface", ("fast0", "fast0.3405"))
def test_native_gateway_uplink_failure_and_restore(endpoint, interface):
    primary, backup = endpoint["gateways"]["gateway-r1"], endpoint["gateways"]["gateway-r2"]
    await_forwarding(endpoint, primary, 30)
    try:
        execute("gateway-r1", "ip", "link", "set", interface, "down")
        execute("gateway-r1", "ping", "-n", "-c", "2", "-W", "1", "10.203.241.10")
        if interface == "fast0.3405":
            peers = json.loads(execute("gateway-r1", "vtysh", "-c", "show bgp summary json"))[
                "ipv4Unicast"
            ]["peers"]
            assert peers["fe80::1"]["state"] == "Established"
        await_forwarding(endpoint, backup, 30)
        stable = ping(endpoint, 3)
        assert stable.returncode == 0
        assert "3 packets transmitted, 3 received" in stable.stdout
    finally:
        execute("gateway-r1", "ip", "link", "set", interface, "up")
        await_forwarding(endpoint, primary, 45)


def core_connection_counters():
    result = {}
    tors = ("r1a", "r1b", "r2a", "r2b")
    for node in ("spine0", "spine1", *tors):
        summary = json.loads(execute(node, "vtysh", "-c", "show bgp vrf all summary json"))
        peers = tors if node.startswith("spine") else ("spine0", "spine1")
        counters = {}
        for vrf, suffix in (("default", ""), ("access", ".3420"), ("replication", ".3430")):
            for peer in peers:
                row = summary[vrf]["ipv4Unicast"]["peers"][peer + suffix]
                assert row["state"] == "Established", (node, vrf, peer)
                counters[f"{vrf}/{peer}"] = {
                    key: row[key] for key in ("connectionsEstablished", "connectionsDropped")
                }
        result[node] = {"bgpd_pid": execute(node, "pidof", "bgpd").strip(), "peers": counters}
        if node in tors:
            destination = "10.205.250.13" if node.startswith("r1") else "10.205.250.11"
            routes = json.loads(execute(node, "ip", "-j", "route", "show", f"{destination}/32"))
            assert len(routes) == 1 and routes[0]["protocol"] == "bgp"
            hops = routes[0]["nexthops"]
            assert {hop["dev"] for hop in hops} == {"spine0", "spine1"}
            assert all(
                hop["via"]["family"] == "inet6" and "dead" not in hop.get("flags", [])
                for hop in hops
            )
            result[node]["kernel_paths"] = sorted(
                (hop["dev"], hop["via"]["host"], hop.get("weight", 1)) for hop in hops
            )
    return result


@pytest.mark.parametrize("spine", ("spine0", "spine1"))
def test_native_gateway_nat_with_spine_frr_stopped(endpoint, spine):
    instance = json.loads(
        subprocess.check_output(
            ["lxc", "query", f"/1.0/instances/{spine}?project={PROJECT}"],
            text=True,
            timeout=30,
        )
    )
    assert instance["status"] == "Running" and instance["type"] == "container"
    assert instance["config"]["user.maas-bird-owner"] == "network-lab"
    assert instance["expanded_config"].get("security.privileged", "false") == "false"
    primary = endpoint["gateways"]["gateway-r1"]
    await_forwarding(endpoint, primary, 30)
    configuration = execute(spine, "sha256sum", "/etc/frr/frr.conf")
    before_pid = execute(spine, "pidof", "bgpd").strip()
    receipt = {
        "spine": spine,
        "before_bgpd_pid": before_pid,
        "configuration_sha256": configuration,
        "before": core_service_health(spine, False),
    }
    try:
        execute(spine, "systemctl", "stop", "frr")
        state = run(spine, "systemctl", "is-active", "frr", check=False)
        assert state.returncode != 0 and state.stdout.strip() == "inactive"
        receipt["held_core"] = core_service_health(spine, True)
        packets = execute(
            CLIENT,
            "ip",
            "netns",
            "exec",
            endpoint["namespace"],
            "ping",
            "-n",
            "-c",
            "5",
            "-i",
            "0.2",
            "-W",
            "1",
            "-M",
            "do",
            "-s",
            "1372",
            endpoint["external"],
        )
        receipt["held_nat_packets"] = packets
        receipt["held_gateway"] = active_gateway()
        assert "5 packets transmitted, 5 received" in packets
        assert receipt["held_gateway"] == primary
        receipt["fault_assertions_passed"] = True
    finally:
        try:
            execute(spine, "systemctl", "start", "frr")
            receipt["restored_core"] = core_service_health(spine, False)
            await_forwarding(endpoint, primary, 30)
            assert execute(spine, "sha256sum", "/etc/frr/frr.conf") == configuration
            receipt["after_bgpd_pid"] = execute(spine, "pidof", "bgpd").strip()
            assert receipt["after_bgpd_pid"] and receipt["after_bgpd_pid"] != before_pid
            receipt["restored"] = True
        finally:
            record(endpoint, f"spine-frr-{spine}", receipt)


@pytest.mark.parametrize("node", ("gateway-r1", "gateway-r2"))
@pytest.mark.parametrize(
    "service", ("systemd-networkd", "ovs-vswitchd", "systemd-networkd+ovs-vswitchd")
)
def test_native_gateway_service_restart(endpoint, node, service):
    """Package-maintenance restarts must recover the real routed NAT path."""
    primary = endpoint["gateways"]["gateway-r1"]
    remote = "10.205.251.2/32" if node == "gateway-r1" else "10.205.251.1/32"
    await_forwarding(endpoint, primary, 30)
    recovered = False
    try:
        for name in service.split("+"):
            execute(node, "systemctl", "restart", name)
        await_forwarding(endpoint, primary, 30)
        route = json.loads(execute(node, "ip", "-j", "route", "show", remote))
        assert len(route) == 1 and route[0]["protocol"] == "bgp", route
        assert route[0]["via"]["family"] == "inet6", route
        recovered = True
    finally:
        record(
            endpoint,
            f"restart-{node}-{service}",
            {
                "recovered": recovered,
                "route": json.loads(execute(node, "ip", "-j", "route", "show", remote)),
                "ovs_other_config": execute(
                    node, "ovs-vsctl", "get", "Open_vSwitch", ".", "other_config"
                ),
                "bfd": execute(
                    node,
                    "ovs-vsctl",
                    "--format=json",
                    "--columns=name,bfd_status",
                    "list",
                    "Interface",
                ),
            },
        )
        if not recovered:
            # Restore the owned fixture after preserving the failed observations.
            execute(node, "systemctl", "restart", "frr")
            execute(
                node,
                "ovs-vsctl",
                "remove",
                "Open_vSwitch",
                ".",
                "other_config",
                "flow-restore-wait",
            )
            await_forwarding(endpoint, primary, 30)


@pytest.mark.slow
def test_native_gateway_nat_stability(endpoint):
    seconds = int(os.environ.get("NLAB_NAT_STABILITY_SECONDS", "300"))
    assert 30 <= seconds <= 7200
    primary = endpoint["gateways"]["gateway-r1"]
    await_forwarding(endpoint, primary, 30)
    before = core_connection_counters()
    directory = ROOT / "runtime/terraform/acceptance/ovn-nat"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    trace_path = directory / f"{endpoint['namespace']}-stability.txt"
    write_atomic(trace_path, "", 0o600)
    record(
        endpoint,
        "stability-start",
        {"seconds": seconds, "core_before": before, "trace": str(trace_path)},
    )
    processes = []
    started = time.monotonic()
    try:
        with trace_path.open("a") as output:
            process = subprocess.Popen(
                [
                    "lxc",
                    "exec",
                    "--project",
                    PROJECT,
                    CLIENT,
                    "--",
                    "ip",
                    "netns",
                    "exec",
                    endpoint["namespace"],
                    "stdbuf",
                    "-oL",
                    "ping",
                    "-n",
                    "-D",
                    "-O",
                    "-i",
                    "1",
                    "-w",
                    str(seconds),
                    "-W",
                    "1",
                    "-M",
                    "do",
                    "-s",
                    "1372",
                    endpoint["external"],
                ],
                text=True,
                stdout=output,
                stderr=subprocess.PIPE,
            )
            processes.append(process)
            _, errors = process.communicate(timeout=seconds + 30)
        elapsed = time.monotonic() - started
        trace = trace_path.read_text()
        counts = re.search(r"(\d+) packets transmitted, (\d+) received", trace)
        assert counts is not None
        sent, received = int(counts[1]), int(counts[2])
        rows = [
            (float(stamp), int(sequence))
            for stamp, sequence in re.findall(
                r"(?m)^\[(\d+\.\d+)\].* bytes from .*: icmp_seq=(\d+)", trace
            )
        ]
        after = core_connection_counters()
        result = {
            "seconds": seconds,
            "elapsed_seconds": elapsed,
            "transmitted": sent,
            "received": received,
            "core_before": before,
            "core_after": after,
            "stderr": errors,
            "trace": str(trace_path),
            "returncode": process.returncode,
            "active_gateway": active_gateway(),
        }
        record(endpoint, "stability-result", result)
        assert process.returncode == 0 and elapsed >= seconds
        assert sent >= 20 and received == sent == len(rows)
        assert len({sequence for _, sequence in rows}) == len(rows)
        assert all(b[1] > a[1] for a, b in zip(rows, rows[1:], strict=False))
        assert after == before
        assert result["active_gateway"] == primary
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)
