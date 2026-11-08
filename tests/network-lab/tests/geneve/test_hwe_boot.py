"""Inspect the actual HWE boot, persistent chassis identity and admitted ordering."""

import importlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("name", ("probe", "probe-alt", "probe-r2a", "probe-r2b"))
def test_native_hwe_boot_identity_endpoint_and_service_order(overlay, name):
    deployment, hosts = overlay
    upgrade = importlib.import_module("upgrade_hwe")
    expected = upgrade.installed_kernel(name)
    assert deployment.ssh(name, "uname", "-r") == expected
    assert (
        deployment.ssh(name, "sudo", "cat", "/etc/openvswitch/system-id.conf")
        == hosts[name]["uuid"]
    )
    actual = deployment.ssh(
        name, "sudo", "ovs-vsctl", "get", "Open_vSwitch", ".", "external_ids:system-id"
    )
    assert actual.strip('"') == hosts[name]["uuid"]
    assert deployment.ssh(
        name, "systemctl", "is-active", "bird", "ovn-controller", "network-lab-tenant"
    ).split() == ["active", "active", "active"]
    upgrade.service_order(name)
    records = sorted((ROOT / "runtime").glob(name + "-hwe-[0-9]*.json"))
    assert records, "No completed native HWE reboot was recorded"
    record = json.loads(records[-1].read_text())
    assert record["uuid"] == hosts[name]["uuid"] and record["completed"] >= record["started"]
    assert record["after_boot_id"] != record["before_boot_id"]
    assert (
        deployment.ssh(name, "cat", "/proc/sys/kernel/random/boot_id") == record["after_boot_id"]
    )
