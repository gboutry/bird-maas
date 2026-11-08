"""Live native-provider Juju controller checks, including strict boot status."""

import json
import os
import stat
import subprocess
from pathlib import Path

PROJECT = "maas-bird-network-lab-tf"
CLIENT = Path(__file__).resolve().parents[2] / "runtime/terraform/juju-client"


def juju(*args):
    return json.loads(
        subprocess.check_output(
            ["/snap/juju/current/bin/juju", *args, "--format=json"],
            env={**os.environ, "JUJU_DATA": str(CLIENT)},
            text=True,
            timeout=60,
        )
    )


def guest(*args):
    return subprocess.check_output(
        ["lxc", "exec", "--project", PROJECT, "juju", "--", *args],
        text=True,
        timeout=30,
    )


def test_native_controller_api_and_model():
    details = juju("show-controller", "nlab-tf")["nlab-tf"]["details"]
    assert details["agent-version"] == "3.6.29"
    inventory = json.loads(
        subprocess.check_output(
            ["lxc", "exec", "--project", PROJECT, "maas", "--", "maas", "lab", "machines", "read"],
            text=True,
            timeout=60,
        )
    )
    machine = next(node for node in inventory if node["hostname"] == "juju")
    assert details["api-endpoints"]
    assert all(
        endpoint.rsplit(":", 1)[0].strip("[]") in machine["ip_addresses"]
        for endpoint in details["api-endpoints"]
    )
    models = juju("models", "--controller=nlab-tf")["models"]
    controller = [model for model in models if model["name"] == "admin/controller"]
    assert len(controller) == 1
    assert controller[0]["type"] == "maas"
    assert controller[0]["life"] == "alive"


def test_native_controller_guest_identity_and_service():
    node = json.loads(
        subprocess.check_output(
            ["lxc", "query", f"/1.0/instances/juju?project={PROJECT}"],
            text=True,
            timeout=30,
        )
    )
    assert node["config"]["user.maas-bird-owner"] == "network-lab"
    assert node["config"]["limits.cpu"] == "1"
    assert node["config"]["limits.memory"] == "4GiB"
    assert (
        guest("cat", "/sys/class/dmi/id/product_uuid").strip().lower()
        == node["config"]["volatile.uuid"]
    )
    assert guest("systemctl", "is-active", "jujud-machine-0").strip() == "active"
    release = guest("uname", "-r").strip()
    assert release.endswith("-generic")
    assert not release.startswith("6.8.")
    dependencies = guest("dpkg-query", "-W", "-f=${Depends}", "linux-image-generic-hwe-24.04")
    assert f"linux-image-{release}" in dependencies
    assert 'VERSION_ID="24.04"' in guest("cat", "/etc/os-release")


def test_private_native_client_configuration():
    assert stat.S_IMODE(CLIENT.stat().st_mode) == 0o700
    for name in ("controllers.yaml", "accounts.yaml"):
        assert stat.S_IMODE((CLIENT / name).stat().st_mode) == 0o600


def test_controller_trusts_maas_ca():
    result = json.loads(
        guest(
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "https://10.203.241.10:5443/MAAS/api/2.0/version/",
        )
    )
    assert result["version"].startswith("3.8.")


def test_controller_strict_cloud_init_success():
    completed = subprocess.run(
        [
            "lxc",
            "exec",
            "--project",
            PROJECT,
            "juju",
            "--",
            "cloud-init",
            "status",
            "--format=json",
        ],
        text=True,
        capture_output=True,
        timeout=30,
    )
    status = json.loads(completed.stdout)
    assert status["extended_status"] == "done", {
        "status": status["extended_status"],
        "warning_count": len(status.get("recoverable_errors", {}).get("WARNING", [])),
    }
    assert completed.returncode == 0
    assert status["errors"] == []
    assert status["recoverable_errors"] == {}
