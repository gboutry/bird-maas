"""Withdraw real host fast interfaces and exercise the native OVN dataplane."""

import importlib
import json
import shlex
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration


def routes(deployment):
    return json.loads(
        deployment.ssh("probe", "ip", "-j", "-4", "-N", "route", "show", "table", "4400")
    )


def wait_paths(deployment, devices, timeout=45):
    deadline = time.monotonic() + timeout
    while True:
        found = routes(deployment)
        learned = [r for r in found if str(r.get("protocol")) == "12"]
        if not devices:
            valid = not learned
        else:
            expected = importlib.import_module("routing_expectations").general_paths(
                "probe", parents=tuple(d.removesuffix(".3404") for d in devices)
            )
            valid = {r["dst"].removesuffix("/32") for r in learned} == set(expected) and all(
                len(r.get("nexthops", [r])) == len(expected[r["dst"].removesuffix("/32")])
                and {h["dev"] for h in r.get("nexthops", [r])}
                == expected[r["dst"].removesuffix("/32")]
                for r in learned
            )
        guards = [r for r in found if str(r.get("protocol")) == "4"]
        valid = valid and len(guards) == 1 and guards[0].get("dst") == "default"
        valid = valid and str(guards[0].get("type")) == "7" and guards[0]["metric"] == 42760
        if valid:
            return found
        assert time.monotonic() < deadline, found
        time.sleep(0.5)


def wait_return_routes(deployment, active, timeout=45):
    deadline = time.monotonic() + timeout
    while True:
        snapshot = {}
        valid = True
        for node in ("spine0", "spine1", "r1a", "r1b", "r2a", "r2b"):
            found = json.loads(
                deployment.fabric(
                    "docker",
                    "exec",
                    "clab-maas-bird-fabric-" + node,
                    "ip",
                    "-j",
                    "-4",
                    "route",
                    "show",
                    "exact",
                    "10.205.10.20/32",
                )
            )
            snapshot[node] = found
            if not active:
                valid = valid and not found
                continue
            if node.startswith("spine"):
                devices = {"eth1" if tor == "r1a" else "eth2" for tor in active}
            elif node in active:
                devices = {"eth3.3404"}
            else:
                devices = {"eth1", "eth2"}
            valid = valid and len(found) == 1 and found[0].get("protocol") == "bgp"
            valid = valid and {h["dev"] for h in found[0].get("nexthops", found)} == devices
        if valid:
            return snapshot
        assert time.monotonic() < deadline, snapshot
        time.sleep(0.5)


def tenant_packets(deployment, hosts):
    results = {}
    for name in hosts:
        if name == "probe":
            continue
        for source, target in (("probe", name), (name, "probe")):
            text = deployment.ssh(
                source,
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
                "1372",
                "-c",
                "3",
                "-i",
                "0.2",
                "-W",
                "1",
                hosts[target]["address"],
            )
            assert "3 packets transmitted, 3 received, 0% packet loss" in text
            results[source + "->" + target] = text
    return results


@pytest.mark.parametrize(
    ("failed", "surviving"), (("enp7s0", "enp8s0.3404"), ("enp8s0", "enp7s0.3404"))
)
def test_each_fast_parent_withdrawal_and_geneve_recovery(overlay, failed, surviving):
    deployment, hosts = overlay
    result = {"started": time.time(), "failed_parent": failed}
    try:
        result["before"] = wait_paths(deployment, {"enp7s0.3404", "enp8s0.3404"})
        result["return_routes_before"] = wait_return_routes(deployment, {"r1a", "r1b"})
        try:
            deployment.ssh("probe", "sudo", "ip", "link", "set", failed, "down")
            result["link_down_at"] = time.time()
            result["surviving_routes"] = wait_paths(deployment, {surviving})
            active = {"r1b"} if failed == "enp7s0" else {"r1a"}
            result["surviving_return_routes"] = wait_return_routes(deployment, active)
            result["surviving_routes_observed_at"] = time.time()
            result["surviving_packets"] = tenant_packets(deployment, hosts)
            vtep = deployment.ssh(
                "probe",
                "sudo",
                "ovs-vsctl",
                "get",
                "Open_vSwitch",
                ".",
                "external_ids:ovn-encap-ip",
            )
            assert vtep.strip('"') == hosts["probe"]["vtep"]
        finally:
            deployment.ssh("probe", "sudo", "ip", "link", "set", failed, "up")
            result["link_up_at"] = time.time()
        result["restored_routes"] = wait_paths(deployment, {"enp7s0.3404", "enp8s0.3404"})
        result["restored_return_routes"] = wait_return_routes(deployment, {"r1a", "r1b"})
        result["restored_packets"] = tenant_packets(deployment, hosts)
        result["completed"] = time.time()
    finally:
        (ROOT / f"runtime/ovn/{failed}-fault.json").write_text(json.dumps(result, indent=2) + "\n")


def test_both_fast_paths_fail_closed_without_management_geneve(overlay):
    deployment, hosts = overlay
    result = {"started": time.time()}
    captures = []
    try:
        result["before"] = wait_paths(deployment, {"enp7s0.3404", "enp8s0.3404"})
        try:
            for device in ("enp7s0", "enp8s0"):
                deployment.ssh("probe", "sudo", "ip", "link", "set", device, "down")
            result["failed_closed_routes"] = wait_paths(deployment, set())
            result["withdrawn_return_routes"] = wait_return_routes(deployment, set())
            command = [
                "sudo",
                "timeout",
                "5",
                "tcpdump",
                "-n",
                "-l",
                "-i",
                "bond0",
                "udp dst port 6081",
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
                    shlex.join(command),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            captures.append(proc)
            assert proc.stderr is not None
            while True:
                line = proc.stderr.readline()
                assert line, "Capture ended before readiness"
                if "listening on bond0" in line:
                    break
            with pytest.raises(subprocess.CalledProcessError) as ping:
                deployment.ssh(
                    "probe",
                    "sudo",
                    "ip",
                    "netns",
                    "exec",
                    "nlab-tenant",
                    "ping",
                    "-n",
                    "-c",
                    "3",
                    "-i",
                    "0.2",
                    "-W",
                    "1",
                    hosts["probe-r2a"]["address"],
                )
            result["tenant_failure"] = ping.value.stdout
            assert ping.value.returncode == 1 and "0 received" in ping.value.stdout
            with pytest.raises(subprocess.CalledProcessError) as lookup:
                deployment.ssh(
                    "probe",
                    "ip",
                    "route",
                    "get",
                    hosts["probe-r2a"]["vtep"],
                    "from",
                    hosts["probe"]["vtep"],
                )
            assert lookup.value.returncode == 2
            assert lookup.value.stderr == "RTNETLINK answers: No route to host\n"
            stdout, stderr = proc.communicate(timeout=12)
            result["management_capture"] = {
                "stdout": stdout,
                "stderr": stderr,
                "exit": proc.returncode,
            }
            assert proc.returncode == 124
            assert stdout.strip() == "" and "0 packets captured" in stderr
            result["management_ping"] = deployment.ssh(
                "probe", "ping", "-n", "-c", "2", "-W", "1", "10.203.240.11"
            )
            assert "2 packets transmitted, 2 received, 0% packet loss" in result["management_ping"]
        finally:
            errors = []
            for device in ("enp7s0", "enp8s0"):
                try:
                    deployment.ssh("probe", "sudo", "ip", "link", "set", device, "up")
                except (OSError, subprocess.SubprocessError) as error:
                    errors.append(error)
            if errors:
                raise ExceptionGroup("Unable to restore fast parents", errors)
        result["restored_routes"] = wait_paths(deployment, {"enp7s0.3404", "enp8s0.3404"})
        result["restored_return_routes"] = wait_return_routes(deployment, {"r1a", "r1b"})
        result["restored_packets"] = tenant_packets(deployment, hosts)
    finally:
        for proc in captures:
            if proc.poll() is None:
                proc.terminate()
                proc.communicate(timeout=15)
        (ROOT / "runtime/ovn/both-parents-fault.json").write_text(
            json.dumps(result, indent=2) + "\n"
        )
