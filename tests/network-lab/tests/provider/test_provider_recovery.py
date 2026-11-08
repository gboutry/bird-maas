"""Exercise actual provider reconfiguration and the owned fabric VM's boot."""

import ipaddress
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from maas_bird.runtime import write_atomic

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "maas-bird-network-lab"
NODES = ("spine0", "spine1", "r1a", "r1b", "r2a", "r2b")
pytestmark = pytest.mark.integration


def owned():
    vm = json.loads(
        subprocess.check_output(
            ["lxc", "query", f"/1.0/instances/fabric?project={PROJECT}"], text=True, timeout=15
        )
    )
    assert vm["type"] == "virtual-machine"
    assert vm["config"]["user.maas-bird-owner"] == "network-lab"
    assert vm["config"]["limits.cpu"] == "2"
    assert vm["config"]["limits.memory"] == "8GiB"
    return vm["config"]["volatile.uuid"]


def snapshot(provider):
    model, guest = provider
    return {
        "boot_id": guest("cat", "/proc/sys/kernel/random/boot_id"),
        "containers": {
            node: guest(
                "docker", "inspect", "--format", "{{.Id}}", "clab-maas-bird-fabric-" + node
            )
            for node in NODES
        },
        "endpoints": {
            namespace: json.loads(
                guest(
                    "ip",
                    "netns",
                    "exec",
                    namespace,
                    "ip",
                    "-j",
                    "link",
                    "show",
                    "dev",
                    "provider0",
                )
            )[0]
            for namespace in model["endpoints"]
        },
    }


def healthy_packets(provider, timeout=180):
    model, guest = provider
    deadline = time.monotonic() + timeout
    while True:
        try:
            assert guest("systemctl", "is-active", "network-lab-fabric.service") == "active"
            assert guest("systemctl", "is-active", "network-lab-provider.service") == "active"
            packets = {}
            for namespace in model["endpoints"]:
                other = next(
                    config for name, config in model["endpoints"].items() if name != namespace
                )
                text = guest(
                    "ip",
                    "netns",
                    "exec",
                    namespace,
                    "ping",
                    "-n",
                    "-M",
                    "do",
                    "-s",
                    "1472",
                    "-c",
                    "2",
                    "-W",
                    "1",
                    str(ipaddress.ip_interface(other["address"]).ip),
                )
                assert "2 packets transmitted, 2 received, 0% packet loss" in text
                packets[namespace] = text
            return packets
        except (AssertionError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


def save(name, data):
    output = ROOT / "runtime/provider"
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_atomic(output / (name + ".json"), json.dumps(data, indent=2) + "\n", 0o600)


def test_plain_fabric_cannot_silently_remove_installed_provider(provider):
    owned()
    before = snapshot(provider)
    with tempfile.TemporaryDirectory(
        prefix="provider-negative-", dir=ROOT / "runtime"
    ) as temporary:
        source = Path(temporary) / "fabric"
        shutil.copytree(ROOT / "runtime/host-fabric", source)
        path = source / "public-plan.json"
        manifest = json.loads(path.read_text())
        del manifest["provider_intent"]
        manifest.pop("gateway_intent", None)
        write_atomic(path, json.dumps(manifest) + "\n", 0o600)
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/apply_host_fabric.py"),
                "--directory",
                str(source),
                "--apply",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode != 0
        assert "Remove the owned provider service" in result.stderr
        assert snapshot(provider) == before
        healthy_packets(provider)


def test_fabric_reconfigure_rebuilds_provider_links(provider):
    identity = owned()
    before = snapshot(provider)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/apply_host_fabric.py"),
            "--directory",
            str(ROOT / "runtime/host-fabric"),
            "--apply",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    receipt = {
        "uuid": identity,
        "before": before,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    try:
        assert result.returncode == 0
        receipt["packets"] = healthy_packets(provider)
        after = snapshot(provider)
        assert after["boot_id"] == before["boot_id"]
        assert all(after["containers"][n] != before["containers"][n] for n in NODES)
        assert all(
            after["endpoints"][n]["ifindex"] != before["endpoints"][n]["ifindex"]
            for n in before["endpoints"]
        )
        assert owned() == identity
        receipt["after"] = after
        receipt["passed"] = True
    finally:
        save("reconfigure-recovery", receipt)


def test_provider_rebuilds_after_owned_fabric_vm_boot(provider):
    identity = owned()
    before = snapshot(provider)
    receipt = {"uuid": identity, "before": before, "started": time.time()}
    try:
        subprocess.run(
            ["lxc", "restart", "--project", PROJECT, "fabric", "--timeout=60"],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        deadline = time.monotonic() + 180
        while True:
            try:
                probe = subprocess.run(
                    [
                        "lxc",
                        "exec",
                        "--project",
                        PROJECT,
                        "fabric",
                        "--",
                        "cat",
                        "/proc/sys/kernel/random/boot_id",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)
                continue
            if probe.returncode == 0 and probe.stdout.strip() != before["boot_id"]:
                receipt["new_boot_id"] = probe.stdout.strip()
                break
            assert time.monotonic() < deadline
            time.sleep(1)
        receipt["packets"] = healthy_packets(provider)
        after = snapshot(provider)
        assert after["boot_id"] == receipt["new_boot_id"]
        assert after["boot_id"] != before["boot_id"]
        assert all(after["containers"][n] != before["containers"][n] for n in NODES)
        assert owned() == identity
        receipt["after"] = after
        receipt["completed"] = time.time()
        receipt["passed"] = True
    finally:
        save("boot-recovery", receipt)
