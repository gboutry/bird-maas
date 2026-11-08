"""Native OVN chassis bindings, routed Geneve packets and tenant MTU."""

import importlib
import shlex
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKERS = ("probe", "probe-alt", "probe-r2a", "probe-r2b")
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("name", WORKERS)
def test_native_chassis_binding_and_no_host_uplinks_in_ovs(overlay, name):
    deployment, hosts = overlay
    ssh = deployment.ssh
    host = hosts[name]
    assert (
        ssh(name, "sudo", "ovn-appctl", "-t", "ovn-controller", "connection-status") == "connected"
    )
    assert (
        ssh(name, "sudo", "ovs-vsctl", "get", "Open_vSwitch", ".", "external_ids:system-id").strip(
            '"'
        )
        == host["uuid"]
    )
    assert (
        ssh(
            name, "sudo", "ovs-vsctl", "get", "Open_vSwitch", ".", "external_ids:ovn-encap-ip"
        ).strip('"')
        == host["vtep"]
    )
    assert (
        deployment.fabric("ovn-nbctl", "get", "Logical_Switch_Port", host["port"], "up") == "true"
    )
    binding = deployment.fabric("ovn-sbctl", "get", "Port_Binding", host["port"], "chassis").strip(
        "[]"
    )
    assert (
        deployment.fabric("ovn-sbctl", "get", "Chassis", binding, "name").strip('"')
        == host["uuid"]
    )
    interfaces = ssh(name, "sudo", "ovs-vsctl", "list-ifaces", "br-int").splitlines()
    expected_remotes = {h["vtep"] for other, h in hosts.items() if other != name}
    gateways = importlib.import_module("routing_expectations").gateway_intent()
    if gateways:
        expected_remotes |= {g["vtep"].split("/")[0] for g in gateways["gateways"].values()}
    assert "nlab-tenant" in interfaces and len(interfaces) == len(expected_remotes) + 1
    remotes = set()
    for interface in interfaces:
        if interface == "nlab-tenant":
            continue
        assert ssh(name, "sudo", "ovs-vsctl", "get", "Interface", interface, "type") == "geneve"
        remote = ssh(
            name, "sudo", "ovs-vsctl", "get", "Interface", interface, "options:remote_ip"
        ).strip('"')
        local = ssh(
            name, "sudo", "ovs-vsctl", "get", "Interface", interface, "options:local_ip"
        ).strip('"')
        assert local == host["vtep"]
        remotes.add(remote)
    assert remotes == expected_remotes
    assert not any(i.startswith(("enp", "bond", "lo")) for i in interfaces)


@pytest.mark.parametrize("name", WORKERS)
@pytest.mark.parametrize("payload", (56, 1372))
def test_every_directed_tenant_path_at_declared_mtu(overlay, name, payload):
    deployment, hosts = overlay
    for other, host in hosts.items():
        if other == name:
            continue
        result = deployment.ssh(
            name,
            "sudo",
            "ip",
            "netns",
            "exec",
            "nlab-tenant",
            "ping",
            "-n",
            "-M",
            "do",
            "-s",
            str(payload),
            "-c",
            "3",
            "-i",
            "0.2",
            "-W",
            "1",
            host["address"],
        )
        assert "3 packets transmitted, 3 received, 0% packet loss" in result


def test_geneve_packet_uses_stable_loopbacks_on_fast_underlay(overlay):
    deployment, hosts = overlay
    name, target = "probe", "probe-r2a"
    capture_args = [
        "sudo",
        "timeout",
        "12",
        "tcpdump",
        "-n",
        "-l",
        "-i",
        "any",
        "-c",
        "4",
        f"udp dst port 6081 and src host {hosts[name]['vtep']} "
        f"and dst host {hosts[target]['vtep']}",
    ]
    proc = subprocess.Popen(
        [
            "ssh",
            "-F",
            str(ROOT / "runtime/juju-bootstrap-ssh.conf"),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=5",
            "ubuntu@10.203.240.20",
            shlex.join(capture_args),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert proc.stderr is not None
        while True:
            line = proc.stderr.readline()
            assert line, "Capture ended before reporting readiness"
            if "listening on any" in line:
                break
        result = deployment.ssh(
            name,
            "sudo",
            "ip",
            "netns",
            "exec",
            "nlab-tenant",
            "ping",
            "-n",
            "-c",
            "8",
            "-i",
            "0.2",
            "-W",
            "1",
            hosts[target]["address"],
        )
        assert "8 packets transmitted, 8 received, 0% packet loss" in result
        stdout, stderr = proc.communicate(timeout=15)
        assert proc.returncode == 0, stderr
        lines = stdout.splitlines()
        assert len(lines) == 4
        assert all("enp7s0.3404 Out" in line or "enp8s0.3404 Out" in line for line in lines)
        assert all("Geneve" in line for line in lines)
        assert all(hosts[name]["vtep"] + "." in line for line in lines)
        assert all(hosts[target]["vtep"] + ".6081" in line for line in lines)
        output = ROOT / "runtime/ovn/geneve-source-capture.txt"
        output.write_text(stdout + "\n" + stderr)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.communicate(timeout=15)


def test_southbound_listener_requires_chassis_role_and_nb_is_local(overlay):
    deployment, _ = overlay
    assert deployment.fabric("ovn-nbctl", "get-connection") == ""
    assert deployment.fabric("ovn-sbctl", "get-connection") == (
        'read-write role="ovn-controller" pssl:6642:10.203.240.11'
    )
    assert deployment.fabric("ovn-sbctl", "get", "Connection", ".", "role") == "ovn-controller"


def test_chassis_certificate_cannot_modify_another_chassis(overlay):
    deployment, hosts = overlay
    directory = deployment.DIRECTORY
    try:
        with pytest.raises(subprocess.CalledProcessError) as error:
            deployment.ssh(
                "probe",
                "sudo",
                "ovn-sbctl",
                "--timeout=10",
                "--db=ssl:10.203.240.11:6642",
                "--private-key=" + directory + "/key.pem",
                "--certificate=" + directory + "/cert.pem",
                "--ca-cert=" + directory + "/ca.crt",
                "set",
                "Chassis",
                hosts["probe-r2a"]["uuid"],
                "hostname=unauthorized-chassis-change",
            )
        assert "permission error" in error.value.stderr.lower()
        actual = deployment.fabric(
            "ovn-sbctl", "get", "Chassis", hosts["probe-r2a"]["uuid"], "hostname"
        )
        assert actual == "probe-r2a"
        (ROOT / "runtime/ovn/rbac-rejection.txt").write_text(error.value.stderr)
    finally:
        deployment.fabric(
            "ovn-sbctl", "set", "Chassis", hosts["probe-r2a"]["uuid"], "hostname=probe-r2a"
        )
