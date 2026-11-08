"""Native OVN TLS admission, compute Geneve and provider NAT packets."""

import json
import re
import stat
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from maas_bird.runtime import write_atomic
from test_native_gateways import provider_capture, wait_capture_ready

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "maas-bird-network-lab-tf"
PKI = "/etc/ovn/network-lab"
WORKERS = ("probe", "probe-alt", "probe-r2a", "probe-r2b")


def run(node, *args):
    return subprocess.run(
        ["lxc", "exec", "--project", PROJECT, node, "--", *args],
        input="",
        capture_output=True,
        text=True,
        timeout=30,
    )


def execute(node, *args):
    result = run(node, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def rows(node, tool, columns, table):
    result = json.loads(
        execute(node, tool, "--format=json", f"--columns={columns}", "list", table)
    )
    return [dict(zip(result["headings"], values, strict=True)) for values in result["data"]]


@pytest.fixture(scope="module")
def configuration():
    path = ROOT / "runtime/terraform/lab/90-ovn/terraform.tfstate"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    outputs = {
        key: value["value"] for key, value in json.loads(path.read_text())["outputs"].items()
    }
    expected = {"gateway-r1", "gateway-r2"}
    if outputs["compute_enabled"]:
        expected |= set(WORKERS)
    assert set(outputs["chassis"]) == expected
    for node, chassis in outputs["chassis"].items():
        instance = json.loads(
            subprocess.check_output(
                ["lxc", "query", f"/1.0/instances/{node}?project={PROJECT}"],
                text=True,
                timeout=30,
            )
        )
        assert instance["type"] == "virtual-machine"
        assert instance["status"] == "Running"
        assert instance["config"]["user.maas-bird-owner"] == "network-lab"
        assert instance["config"]["volatile.uuid"] == chassis["uuid"]
    return outputs


def tls_arguments(configuration):
    return [
        "ovn-sbctl",
        "--timeout=5",
        f"--db=ssl:{configuration['central_address']}:6642",
        f"--private-key={PKI}/client.key",
        f"--certificate={PKI}/client.crt",
        f"--ca-cert={PKI}/ca.crt",
    ]


def test_native_central_listener_and_admission(configuration):
    node, address = configuration["central_instance"], configuration["central_address"]
    assert execute(node, "systemctl", "is-active", "ovn-northd").strip() == "active"
    assert execute(node, "ss", "-ltnH", "sport = :6641").strip() == ""
    assert rows(node, "ovn-sbctl", "target,role", "Connection") == [
        {"target": f"pssl:6642:{address}", "role": "ovn-controller"}
    ]
    expected = {row["uuid"]: name for name, row in configuration["chassis"].items()}
    chassis = rows(node, "ovn-sbctl", "name,hostname", "Chassis")
    assert {row["name"]: row["hostname"] for row in chassis} == expected
    encaps = rows(node, "ovn-sbctl", "chassis_name,ip,type", "Encap")
    assert {row["chassis_name"]: (row["ip"], row["type"]) for row in encaps} == {
        row["uuid"]: (row["vtep"], "geneve") for row in configuration["chassis"].values()
    }
    assert execute(node, "stat", "-c", "%a:%U:%G", f"{PKI}/server.key").strip() == "600:root:root"


@pytest.mark.parametrize("node", ("gateway-r1", "gateway-r2"))
def test_native_gateway_certificate_and_remote_database(configuration, node):
    assert execute(node, "stat", "-c", "%a:%U:%G", f"{PKI}/client.key").strip() == "600:root:root"
    assert execute(node, "stat", "-c", "%a", PKI).strip() == "700"
    subject = execute(
        node,
        "openssl",
        "x509",
        "-in",
        f"{PKI}/client.crt",
        "-noout",
        "-subject",
        "-nameopt",
        "RFC2253",
    ).strip()
    assert subject == f"subject=CN={configuration['chassis'][node]['uuid']}"
    result = json.loads(
        execute(
            node,
            *tls_arguments(configuration),
            "--format=json",
            "--columns=name",
            "list",
            "Chassis",
        )
    )
    assert {row[0] for row in result["data"]} == {
        row["uuid"] for row in configuration["chassis"].values()
    }


@pytest.mark.parametrize("node", WORKERS)
def test_native_compute_admission_gate(configuration, node):
    fields = json.loads(
        execute(
            node, "ovs-vsctl", "--format=json", "--columns=external_ids", "list", "Open_vSwitch"
        )
    )["data"][0][0][1]
    fields = dict(fields)
    assert "br-provider" not in execute(node, "ovs-vsctl", "list-br").split()
    if configuration["compute_enabled"]:
        assert fields["ovn-encap-ip"] == configuration["chassis"][node]["vtep"]
        assert fields["ovn-remote"] == f"ssl:{configuration['central_address']}:6642"
        assert "ovn-cms-options" not in fields
    else:
        assert "ovn-remote" not in fields
        assert "ovn-encap-ip" not in fields


@pytest.mark.parametrize("case", ("no-client", "untrusted-ca", "rbac"))
def test_native_tls_and_rbac_rejections(configuration, case):
    node, address = "gateway-r1", configuration["central_address"]
    if case == "rbac":
        name = f"nlab-negative-{uuid.uuid4()}"
        try:
            result = run(
                node, *tls_arguments(configuration), "chassis-add", name, "geneve", "10.205.251.99"
            )
            assert result.returncode != 0
            assert "RBAC rules" in result.stderr
            assert "prohibit row insertion" in result.stderr
        finally:
            execute(
                configuration["central_instance"], "ovn-sbctl", "--if-exists", "chassis-del", name
            )
        return
    arguments = [
        "openssl",
        "s_client",
        "-connect",
        f"{address}:6642",
        "-verify_return_error",
        "-verify_ip",
        address,
        "-quiet",
    ]
    if case == "no-client":
        arguments += ["-CAfile", f"{PKI}/ca.crt"]
        expected = "certificate required"
    else:
        arguments += [
            "-CAfile",
            "/etc/ssl/certs/ca-certificates.crt",
            "-cert",
            f"{PKI}/client.crt",
            "-key",
            f"{PKI}/client.key",
        ]
        expected = "certificate verify failed"
    result = run(node, *arguments)
    assert result.returncode != 0
    assert expected in result.stderr


def test_native_logical_port_intent(configuration):
    ports = rows(
        configuration["central_instance"],
        "ovn-nbctl",
        "name,type,addresses,port_security,options",
        "Logical_Switch_Port",
    )
    infrastructure = {"nlab-tf-tenant-router", "nlab-tf-provider-router", "nlab-tf-localnet"}
    assert {row["name"] for row in ports} == {
        f"tenant-{name}" for name in WORKERS
    } | infrastructure
    for row in ports:
        if row["name"] in infrastructure:
            assert row["port_security"] == ["set", []]
            if row["name"] == "nlab-tf-localnet":
                assert row["type"] == "localnet"
                assert row["addresses"] == "unknown"
                assert dict(row["options"][1]) == {"network_name": "physnet-provider"}
            else:
                assert row["type"] == "router"
                assert row["addresses"] == "router"
                target = (
                    "nlab-tf-router-tenant"
                    if row["name"] == "nlab-tf-tenant-router"
                    else "nlab-tf-router-provider"
                )
                assert dict(row["options"][1]) == {"router-port": target}
            continue
        assert row["type"] == ""
        name = row["name"].removeprefix("tenant-")
        index = WORKERS.index(name)
        identity = json.loads(
            subprocess.check_output(
                ["lxc", "query", f"/1.0/instances/{name}?project={PROJECT}"], text=True, timeout=30
            )
        )["config"]["volatile.uuid"]
        expected = f"02:00:00:ab:00:{20 + index:02x} 172.30.10.{20 + index}"
        assert row["addresses"] == row["port_security"] == expected
        assert dict(row["options"][1]) == {"requested-chassis": identity}


def test_native_router_and_gateway_priorities(configuration):
    node = configuration["central_instance"]
    priorities = rows(node, "ovn-nbctl", "chassis_name,priority", "Gateway_Chassis")
    assert {row["chassis_name"]: row["priority"] for row in priorities} == {
        configuration["chassis"]["gateway-r1"]["uuid"]: 100,
        configuration["chassis"]["gateway-r2"]["uuid"]: 90,
    }
    nat = rows(node, "ovn-nbctl", "type,external_ip,logical_ip", "NAT")
    assert {(row["type"], row["external_ip"], row["logical_ip"]) for row in nat} == {
        ("dnat_and_snat", f"192.0.2.{100 + index}", f"172.30.10.{20 + index}")
        for index in range(4)
    }


@pytest.fixture
def provider_echo(configuration):
    assert configuration["compute_enabled"], "Actual compute admission is required"
    node, address = "gateway-r2", "192.0.2.20"
    before = json.loads(execute(node, "ip", "-j", "address", "show", "br-provider"))[0]
    assert not any(item["family"] == "inet" for item in before["addr_info"])
    try:
        execute(node, "ip", "address", "add", f"{address}/24", "dev", "br-provider")
        yield node, address
    finally:
        execute(node, "ip", "address", "del", f"{address}/24", "dev", "br-provider")
        after = json.loads(execute(node, "ip", "-j", "address", "show", "br-provider"))[0]
        assert after["addr_info"] == before["addr_info"]


@pytest.mark.parametrize("node", WORKERS)
def test_native_compute_geneve_and_provider_nat(configuration, provider_echo, node):
    provider, destination = provider_echo
    index = WORKERS.index(node)
    tenant, floating = f"172.30.10.{20 + index}", f"192.0.2.{100 + index}"
    vtep = configuration["chassis"][node]["vtep"]
    remote = WORKERS[2] if index < 2 else WORKERS[0]
    rack = 1 if index < 2 else 2
    assert "br-provider" not in execute(node, "ovs-vsctl", "list-br").split()
    assert (
        execute(
            node, "ovs-vsctl", "get", "Open_vSwitch", ".", "external_ids:ovn-set-local-ip"
        ).strip('"\n')
        == "true"
    )
    assert json.loads(execute(node, "ip", "-j", "route", "show", f"{destination}/32")) == []
    processes, receipt = [], {"worker": node, "vtep": vtep, "captures": []}
    try:
        for tor in (f"r{rack}a", f"r{rack}b"):
            processes.append(
                (tor, provider_capture(tor, "fast0.3404", f"udp port 6081 and host {vtep}", 8))
            )
        processes.append(
            (provider, provider_capture(provider, "br-provider", f"icmp and host {floating}", 8))
        )
        for _, process in processes:
            wait_capture_ready(process)
        receipt["overlay"] = execute(
            node,
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
            "-I",
            "tenant0",
            f"172.30.10.{20 + WORKERS.index(remote)}",
        )
        assert "3 packets transmitted, 3 received" in receipt["overlay"]
        execute(
            node,
            "ip",
            "route",
            "add",
            f"{destination}/32",
            "via",
            "172.30.10.1",
            "dev",
            "tenant0",
            "src",
            tenant,
        )
        receipt["nat"] = execute(
            node,
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
            "-I",
            "tenant0",
            destination,
        )
        assert "3 packets transmitted, 3 received" in receipt["nat"]
        for capture_node, process in processes:
            output, error = process.communicate(timeout=15)
            receipt["captures"].append({"node": capture_node, "stdout": output, "stderr": error})
            assert process.returncode == 124
            assert "0 packets dropped by kernel" in error
        underlay = "\n".join(item["stdout"] for item in receipt["captures"][:2])
        packets = re.findall(r"(?ms)^\d\d:\d\d:\d\d\.\d+.*?(?=^\d\d:\d\d:\d\d\.\d+|\Z)", underlay)
        for inner in (
            f"{tenant} > {destination}",
            f"{tenant} > 172.30.10.{20 + WORKERS.index(remote)}",
        ):
            assert any(
                "Geneve" in packet
                and re.search(rf"(?m)^\s+{re.escape(vtep)}\.\d+ > \S+\.6081:", packet)
                and inner in packet
                and "ICMP echo request" in packet
                for packet in packets
            ), underlay
        translated = receipt["captures"][2]["stdout"]
        assert f"{floating} > {destination}" in translated, translated
        assert f"{tenant} > {destination}" not in translated
    finally:
        for _, process in processes:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=15)
        remaining = json.loads(execute(node, "ip", "-j", "route", "show", f"{destination}/32"))
        if remaining:
            assert len(remaining) == 1
            assert remaining[0]["gateway"] == "172.30.10.1"
            assert remaining[0]["dev"] == "tenant0"
            assert remaining[0]["prefsrc"] == tenant
            execute(
                node,
                "ip",
                "route",
                "del",
                f"{destination}/32",
                "via",
                "172.30.10.1",
                "dev",
                "tenant0",
            )
        directory = ROOT / "runtime/terraform/acceptance/compute-geneve"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_atomic(
            directory / f"{node}-{time.time_ns()}.json",
            json.dumps(receipt, indent=2) + "\n",
            0o600,
        )
