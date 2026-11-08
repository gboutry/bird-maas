"""Native host state and packet assertions shared by network-lab fault tests."""

import importlib
import json
import subprocess
import time
from decimal import Decimal

import lab_policy
from commission import PROJECT, ROOT
from maas_bird.render import protocol_name
from maas_bird.runtime import write_atomic
from render_host_fabric import WORKERS


def root(name, *args, check=True):
    result = subprocess.run(
        ["lxc", "exec", "--project", PROJECT, name, "--", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check:
        try:
            result.check_returncode()
        except subprocess.CalledProcessError as error:
            error.add_note(
                "Native stdout:\n" + result.stdout + "\nNative stderr:\n" + result.stderr
            )
            raise
    return result


def wait_for(check, timeout=45):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return check()
        except (AssertionError, subprocess.CalledProcessError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def routing_ready(name, gp_parent=None, parents=("enp7s0", "enp8s0"), unavailable_tors=()):
    assert parents and set(parents) <= {"enp7s0", "enp8s0"}
    assert set(unavailable_tors) <= set(WORKERS.values())
    recipe = importlib.import_module("generate_roles")
    hosts = importlib.import_module("commission").HOSTS
    expectations = importlib.import_module("routing_expectations")
    tables = {}
    for role in recipe.roles():
        rows = json.loads(
            root(name, "ip", "-j", "-4", "-N", "route", "show", "table", str(role["table"])).stdout
        )
        tables[str(role["table"])] = rows
        learned = {r["dst"].removesuffix("/32"): r for r in rows if str(r.get("protocol")) == "12"}
        expected = (
            expectations.general_paths(name, parents=(gp_parent,) if gp_parent else parents)
            if role["domain"] == "general"
            else {
                recipe.address(other, target).split("/")[0]: {
                    f"{parent}.{role['vlan']}" for parent in parents
                }
                for other in hosts
                if other != name
                for target in recipe.roles()
                if target["domain"] == role["domain"]
            }
        )
        if role["domain"] == "general" and unavailable_tors:
            gateways = expectations.gateway_intent()
            if gateways:
                for gateway in gateways["gateways"].values():
                    if gateway["tor"] in unavailable_tors:
                        del expected[gateway["vtep"].split("/")[0]]
        assert set(learned) == set(expected)
        for destination, devices in expected.items():
            hops = learned[destination].get("nexthops", [learned[destination]])
            assert len(hops) == len(devices) and {h["dev"] for h in hops} == devices
        guards = [r for r in rows if r.get("dst") == "default"]
        assert len(guards) == 1 and str(guards[0]["type"]) == "7"
        assert str(guards[0]["protocol"]) == "4" and guards[0]["metric"] == 42760
    protocols = root(name, "birdc", "show", "protocols").stdout
    bgp = [
        r.split() for r in protocols.splitlines() if len(r.split()) >= 6 and r.split()[1] == "BGP"
    ]
    declared = lab_policy.read("all-attachments.yaml")
    attachments = json.loads(declared["nic-" + WORKERS[name]]["attachments"])
    expected_protocols = {protocol_name(a["domain"], a["id"]) for a in attachments}
    actual_protocols = {r[0] for r in bgp}
    if len(parents) == 2:
        assert len(bgp) == 10 and actual_protocols == expected_protocols
    else:
        assert len(bgp) == len(actual_protocols) and actual_protocols <= expected_protocols
    expected_up = {
        protocol_name(a["domain"], a["id"])
        for a in attachments
        if a["uplink"] in parents
        and (not gp_parent or a["domain"] != "general" or a["uplink"] == gp_parent)
    }
    assert expected_up <= actual_protocols
    assert {r[0] for r in bgp if r[3] == "up" and "Established" in r[5:]} == expected_up
    bfd = root(name, "birdc", "show", "bfd", "sessions").stdout
    sessions = [r.split() for r in bfd.splitlines() if r.strip().startswith("fe80:")]
    all_interfaces = {
        f"{parent}.{role['vlan']}" for parent in ("enp7s0", "enp8s0") for role in recipe.roles()
    }
    expected_interfaces = {
        f"{parent}.{role['vlan']}"
        for parent in parents
        for role in recipe.roles()
        if not gp_parent or role["domain"] != "general" or parent == gp_parent
    }
    if gp_parent or len(parents) != 2:
        assert len(expected_interfaces) <= len(sessions) <= 10
    else:
        assert len(sessions) == 10
    assert len({r[1] for r in sessions}) == len(sessions)
    assert all(r[0] == "fe80::1" and r[1] in all_interfaces for r in sessions)
    up = [r for r in sessions if r[2] == "Up"]
    assert {r[1] for r in up} == expected_interfaces
    assert all(
        Decimal(r[-2]) == Decimal("0.300") and Decimal(r[-1]) == Decimal("0.900") for r in up
    )
    return {"tables": tables, "bfd": bfd, "protocols": protocols}


def packets(name):
    recipe = importlib.import_module("generate_roles")
    hosts = importlib.import_module("commission").HOSTS
    target = next(n for n in hosts if hosts[n]["rack"] != hosts[name]["rack"])
    results = {}
    for role in recipe.roles():
        source = recipe.address(name, role).split("/")[0]
        destination = recipe.address(target, role).split("/")[0]
        text = root(
            name,
            "ping",
            "-n",
            "-M",
            "do",
            "-s",
            "1472",
            "-I",
            source,
            "-c",
            "3",
            "-i",
            "0.2",
            "-W",
            "1",
            destination,
        ).stdout
        assert "3 packets transmitted, 3 received, 0% packet loss" in text
        results[source + "->" + destination] = text
    return results


def management(name):
    text = root(name, "ping", "-n", "-c", "3", "-i", "0.2", "-W", "1", "10.203.240.10").stdout
    assert "3 packets transmitted, 3 received, 0% packet loss" in text
    return text


def overlay_ready(name):
    services = root(name, "systemctl", "is-active", "ovn-controller", "network-lab-tenant").stdout
    assert services.split() == ["active", "active"]
    assert (
        root(name, "ovn-appctl", "-t", "ovn-controller", "connection-status").stdout.strip()
        == "connected"
    )
    manifest = json.loads((ROOT / "runtime/ovn/manifest.json").read_text())
    hosts = importlib.import_module("commission").HOSTS
    target = next(n for n in WORKERS if hosts[n]["rack"] != hosts[name]["rack"])
    text = root(
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
    ).stdout
    assert "3 packets transmitted, 3 received, 0% packet loss" in text
    return {"services": services, "packets": text}


def save(name, kind, receipt):
    directory = ROOT / "runtime/process-recovery"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_atomic(
        directory / f"{name}-{kind}-{time.time_ns()}.json",
        json.dumps(receipt, indent=2) + "\n",
        0o600,
    )
