"""Restart native host routing and Juju agents through owned out-of-band control."""

import importlib
import json
import time
from pathlib import Path

import jubilant
import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKERS = {"probe": "r1a", "probe-alt": "r1b", "probe-r2a": "r2a", "probe-r2b": "r2b"}
PROJECT = "maas-bird-network-lab"
pytestmark = pytest.mark.integration


@pytest.fixture
def worker():
    juju = jubilant.Juju(model="network-lab:network-lab")
    juju.wait(
        lambda s: jubilant.all_active(s, *("bird-" + s for s in WORKERS.values())), timeout=240
    )
    return (
        juju,
        importlib.import_module("juju_workers"),
        importlib.import_module("host_acceptance"),
    )


@pytest.mark.parametrize("name", WORKERS)
def test_native_bird_stop_withdraws_routes_and_restart_restores_forwarding(worker, name):
    juju, bindings, host = worker
    binding = bindings.worker_binding(juju, name, "bird-" + WORKERS[name])
    receipt = {
        "worker_uuid": binding.uuid,
        "started": time.time(),
        "before": host.routing_ready(name),
    }
    recipe = importlib.import_module("generate_roles")
    try:
        host.root(name, "systemctl", "stop", "bird")
        receipt["stopped"] = time.time()
        assert (
            host.root(name, "systemctl", "is-active", "bird", check=False).stdout.strip()
            == "inactive"
        )

        def withdrawn():
            tables = {}
            for role in recipe.roles():
                rows = json.loads(
                    host.root(
                        name, "ip", "-j", "-4", "-N", "route", "show", "table", str(role["table"])
                    ).stdout
                )
                assert len(rows) == 1 and rows[0]["dst"] == "default"
                assert str(rows[0]["type"]) == "7" and str(rows[0]["protocol"]) == "4"
                assert rows[0]["metric"] == 42760
                tables[str(role["table"])] = rows
            return tables

        receipt["withdrawn"] = host.wait_for(withdrawn, timeout=15)
        receipt["withdrawal_observed"] = time.time()
        receipt["management_while_stopped"] = host.management(name)
        target = next(n for n in WORKERS if n != name)
        hosts = importlib.import_module("commission").HOSTS
        probe = host.root(
            name,
            "ping",
            "-n",
            "-c",
            "1",
            "-W",
            "1",
            "-I",
            hosts[name]["loopback"].split("/")[0],
            hosts[target]["loopback"].split("/")[0],
            check=False,
        )
        assert probe.returncode != 0
        receipt["managed_probe_while_stopped"] = {
            "status": probe.returncode,
            "stdout": probe.stdout,
            "stderr": probe.stderr,
        }
        receipt["fault_assertions_passed"] = True
    finally:
        try:
            host.root(name, "systemctl", "start", "bird")
            receipt["restart_requested"] = time.time()
            receipt["restored"] = host.wait_for(lambda: host.routing_ready(name), timeout=45)
            receipt["forwarding"] = host.packets(name)
            receipt["overlay_restored"] = host.wait_for(
                lambda: host.overlay_ready(name), timeout=45
            )
            receipt["restoration_observed"] = time.time()
            receipt["passed"] = receipt.get("fault_assertions_passed", False)
        finally:
            host.save(name, "bird", receipt)


@pytest.mark.parametrize("name", WORKERS)
def test_native_juju_agent_restart_keeps_routing_process_and_packets(worker, name):
    juju, bindings, host = worker
    binding = bindings.worker_binding(juju, name, "bird-" + WORKERS[name])
    service = "jujud-machine-" + binding.machine + ".service"
    before_pid = host.root(
        name, "systemctl", "show", "bird", "--property=MainPID", "--value"
    ).stdout
    assert int(before_pid) > 0
    assert host.root(name, "systemctl", "is-active", service).stdout.strip() == "active"
    receipt = {
        "worker_uuid": binding.uuid,
        "service": service,
        "started": time.time(),
        "bird_pid_before": int(before_pid),
        "before": host.routing_ready(name),
    }
    try:
        host.root(name, "systemctl", "stop", service)
        assert (
            host.root(name, "systemctl", "is-active", service, check=False).stdout.strip()
            == "inactive"
        )
        receipt["stopped"] = time.time()
        receipt["management_while_stopped"] = host.management(name)
        receipt["routes_while_stopped"] = host.routing_ready(name)
        receipt["packets_while_stopped"] = host.packets(name)
        receipt["overlay_while_agent_stopped"] = host.overlay_ready(name)
        assert (
            host.root(name, "systemctl", "show", "bird", "--property=MainPID", "--value").stdout
            == before_pid
        )
        receipt["fault_assertions_passed"] = True
    finally:
        try:
            host.root(name, "systemctl", "start", service)
            assert host.root(name, "systemctl", "is-active", service).stdout.strip() == "active"
            juju.wait(lambda s: jubilant.all_active(s, "bird-" + WORKERS[name]), timeout=180)
            assert (
                juju.exec(
                    "sudo", "cat", "/sys/class/dmi/id/product_uuid", unit=binding.unit
                ).stdout.strip()
                == binding.uuid
            )
            receipt["restored"] = host.routing_ready(name)
            receipt["restored_packets"] = host.packets(name)
            receipt["overlay_restored"] = host.wait_for(
                lambda: host.overlay_ready(name), timeout=45
            )
            after_pid = host.root(
                name, "systemctl", "show", "bird", "--property=MainPID", "--value"
            ).stdout
            assert after_pid == before_pid
            receipt["bird_pid_after"] = int(after_pid)
            receipt["restoration_observed"] = time.time()
            receipt["passed"] = receipt.get("fault_assertions_passed", False)
        finally:
            host.save(name, "juju-agent", receipt)


@pytest.mark.parametrize("name", WORKERS)
@pytest.mark.parametrize("failed_parent", ("enp7s0", "enp8s0"))
def test_bfd_only_loss_with_link_and_neighbor_forwarding_retained(worker, name, failed_parent):
    juju, bindings, host = worker
    binding = bindings.worker_binding(juju, name, "bird-" + WORKERS[name])
    interface = failed_parent + ".3404"
    survivor = "enp8s0" if failed_parent == "enp7s0" else "enp7s0"
    receipt = {
        "worker_uuid": binding.uuid,
        "failed_bfd_interface": interface,
        "started": time.time(),
        "before": host.routing_ready(name),
    }
    qdiscs = json.loads(host.root(name, "tc", "-j", "qdisc", "show", "dev", interface).stdout)
    assert len(qdiscs) == 1 and qdiscs[0]["kind"] == "noqueue"
    host.root(name, "tc", "qdisc", "add", "dev", interface, "clsact")
    try:
        for direction in ("ingress", "egress"):
            host.root(
                name,
                "tc",
                "filter",
                "add",
                "dev",
                interface,
                direction,
                "protocol",
                "ipv6",
                "pref",
                "43560",
                "handle",
                "0xbfd",
                "flower",
                "ip_proto",
                "udp",
                "dst_port",
                "3784",
                "action",
                "drop",
            )
        receipt["bfd_drop_installed"] = time.time()
        receipt["surviving"] = host.wait_for(
            lambda: host.routing_ready(name, gp_parent=survivor), timeout=30
        )
        receipt["convergence_observed"] = time.time()
        links = json.loads(host.root(name, "ip", "-j", "link", "show", "dev", interface).stdout)
        assert len(links) == 1 and {"UP", "LOWER_UP"} <= set(links[0]["flags"])
        neighbor = host.root(
            name, "ping", "-6", "-n", "-I", interface, "-c", "3", "-i", "0.2", "-W", "1", "fe80::1"
        ).stdout
        assert "3 packets transmitted, 3 received, 0% packet loss" in neighbor
        receipt["neighbor_forwarding_while_bfd_blocked"] = neighbor
        receipt["role_packets"] = host.packets(name)
        receipt["surviving_after_packets"] = host.routing_ready(name, gp_parent=survivor)
        receipt["overlay_while_bfd_blocked"] = host.overlay_ready(name)
        receipt["management"] = host.management(name)
        receipt["filters"] = {
            direction: json.loads(
                host.root(
                    name, "tc", "-j", "-s", "filter", "show", "dev", interface, direction
                ).stdout
            )
            for direction in ("ingress", "egress")
        }
        for filters in receipt["filters"].values():
            common = {"protocol": "ipv6", "pref": 43560, "kind": "flower", "chain": 0}
            headers = [r for r in filters if "options" not in r]
            entries = [r for r in filters if "options" in r]
            assert headers in ([], [common]) and len(entries) == 1
            assert all({k: r[k] for k in common} == common for r in entries)
            options = entries[0]["options"]
            assert options["handle"] == 0xBFD
            assert options["keys"] == {"eth_type": "ipv6", "ip_proto": "udp", "dst_port": 3784}
            assert len(options["actions"]) == 1
            action = options["actions"][0]
            assert action["kind"] == "gact" and action["control_action"] == {"type": "drop"}
            assert action["stats"]["drops"] > 0
        receipt["fault_assertions_passed"] = True
    except BaseException as error:
        receipt["failure"] = {
            "type": type(error).__name__,
            "message": str(error),
            "notes": getattr(error, "__notes__", []),
        }
        receipt["failure_host_state"] = {
            "protocols": host.root(name, "birdc", "show", "protocols", check=False).stdout,
            "bfd": host.root(name, "birdc", "show", "bfd", "sessions", check=False).stdout,
            "gp_routes": host.root(
                name, "ip", "-j", "-4", "-N", "route", "show", "table", "4400", check=False
            ).stdout,
        }
        receipt["failure_tor_state"] = {}
        for tor in WORKERS.values():
            prefix = ("docker", "exec", "clab-maas-bird-fabric-" + tor)
            receipt["failure_tor_state"][tor] = {
                "bgp": host.root(
                    "fabric", *prefix, "vtysh", "-c", "show bgp summary json", check=False
                ).stdout,
                "bfd": host.root(
                    "fabric", *prefix, "vtysh", "-c", "show bfd peers json", check=False
                ).stdout,
                "routes": host.root(
                    "fabric", *prefix, "ip", "-j", "route", "show", "protocol", "bgp", check=False
                ).stdout,
            }
        raise
    finally:
        try:
            for direction in ("ingress", "egress"):
                filters = json.loads(
                    host.root(
                        name, "tc", "-j", "filter", "show", "dev", interface, direction
                    ).stdout
                )
                assert all(r["pref"] == 43560 and r["kind"] == "flower" for r in filters)
            host.root(name, "tc", "qdisc", "del", "dev", interface, "clsact")
            receipt["drop_removed"] = time.time()
            receipt["restored"] = host.wait_for(lambda: host.routing_ready(name), timeout=45)
            receipt["restored_packets"] = host.packets(name)
            after = json.loads(
                host.root(name, "tc", "-j", "qdisc", "show", "dev", interface).stdout
            )
            assert len(after) == 1 and after[0]["kind"] == "noqueue"
            receipt["passed"] = receipt.get("fault_assertions_passed", False)
        finally:
            host.save(name, "bfd-" + failed_parent, receipt)


@pytest.mark.parametrize("name", WORKERS)
def test_forwarding_blackhole_is_detected_while_bgp_and_bfd_stay_up(worker, name):
    juju, bindings, host = worker
    binding = bindings.worker_binding(juju, name, "bird-" + WORKERS[name])
    hosts = importlib.import_module("commission").HOSTS
    manifest = json.loads((ROOT / "runtime/ovn/manifest.json").read_text())
    target = next(n for n in WORKERS if hosts[n]["rack"] != hosts[name]["rack"])
    source_ip = hosts[name]["loopback"].split("/")[0]
    target_ip = hosts[target]["loopback"].split("/")[0]
    receipt = {
        "worker_uuid": binding.uuid,
        "source": source_ip,
        "destination": target_ip,
        "started": time.time(),
        "before": host.routing_ready(name),
        "overlay_before": host.overlay_ready(name),
    }
    interfaces = ("enp7s0.3404", "enp8s0.3404")
    for interface in interfaces:
        qdiscs = json.loads(host.root(name, "tc", "-j", "qdisc", "show", "dev", interface).stdout)
        assert len(qdiscs) == 1 and qdiscs[0]["kind"] == "noqueue"
    created = []
    try:
        for interface in interfaces:
            host.root(name, "tc", "qdisc", "add", "dev", interface, "clsact")
            created.append(interface)
            host.root(
                name,
                "tc",
                "filter",
                "add",
                "dev",
                interface,
                "egress",
                "protocol",
                "ip",
                "pref",
                "43561",
                "handle",
                "0xbee",
                "flower",
                "src_ip",
                source_ip + "/32",
                "dst_ip",
                target_ip + "/32",
                "action",
                "drop",
            )
        receipt["drop_installed"] = time.time()
        managed = host.root(
            name,
            "ping",
            "-n",
            "-M",
            "do",
            "-s",
            "1472",
            "-I",
            source_ip,
            "-c",
            "3",
            "-i",
            "0.2",
            "-W",
            "1",
            target_ip,
            check=False,
        )
        assert managed.returncode == 1 and "3 packets transmitted, 0 received" in managed.stdout
        receipt["managed_forwarding_failure"] = {
            "status": managed.returncode,
            "stdout": managed.stdout,
            "stderr": managed.stderr,
        }
        tenant = host.root(
            name,
            "ip",
            "netns",
            "exec",
            "nlab-tenant",
            "ping",
            "-n",
            "-M",
            "do",
            "-s",
            "1372",
            "-c",
            "3",
            "-i",
            "0.2",
            "-W",
            "1",
            manifest[target]["address"],
            check=False,
        )
        assert tenant.returncode == 1 and "3 packets transmitted, 0 received" in tenant.stdout
        receipt["tenant_forwarding_failure"] = {
            "status": tenant.returncode,
            "stdout": tenant.stdout,
            "stderr": tenant.stderr,
        }
        receipt["control_plane_while_blackholed"] = host.routing_ready(name)
        assert (
            host.root(
                name, "ovn-appctl", "-t", "ovn-controller", "connection-status"
            ).stdout.strip()
            == "connected"
        )
        receipt["management"] = host.management(name)
        receipt["filters"] = {}
        drops = 0
        for interface in interfaces:
            rows = json.loads(
                host.root(
                    name, "tc", "-j", "-s", "filter", "show", "dev", interface, "egress"
                ).stdout
            )
            receipt["filters"][interface] = rows
            common = {"protocol": "ip", "pref": 43561, "kind": "flower", "chain": 0}
            headers = [r for r in rows if "options" not in r]
            entries = [r for r in rows if "options" in r]
            assert headers in ([], [common]) and len(entries) == 1
            assert {k: entries[0][k] for k in common} == common
            options = entries[0]["options"]
            assert options["handle"] == 0xBEE
            assert options["keys"] == {
                "eth_type": "ipv4",
                "src_ip": source_ip,
                "dst_ip": target_ip,
            }
            assert len(options["actions"]) == 1
            action = options["actions"][0]
            assert action["kind"] == "gact" and action["control_action"] == {"type": "drop"}
            drops += action["stats"]["drops"]
        assert drops >= 6
        receipt["fault_assertions_passed"] = True
    finally:
        try:
            for interface in created:
                rows = json.loads(
                    host.root(
                        name, "tc", "-j", "filter", "show", "dev", interface, "egress"
                    ).stdout
                )
                assert all(
                    r["pref"] == 43561 and r["kind"] == "flower" and r["protocol"] == "ip"
                    for r in rows
                )
                host.root(name, "tc", "qdisc", "del", "dev", interface, "clsact")
            receipt["drop_removed"] = time.time()
            receipt["restored"] = host.routing_ready(name)
            receipt["restored_role_packets"] = host.packets(name)
            receipt["restored_overlay"] = host.wait_for(
                lambda: host.overlay_ready(name), timeout=45
            )
            for interface in interfaces:
                rows = json.loads(
                    host.root(name, "tc", "-j", "qdisc", "show", "dev", interface).stdout
                )
                assert len(rows) == 1 and rows[0]["kind"] == "noqueue"
            receipt["passed"] = receipt.get("fault_assertions_passed", False)
        finally:
            host.save(name, "forwarding-blackhole", receipt)
