"""Live acceptance for the native Terraform MAAS controller stage."""

import json
import stat
import subprocess
from pathlib import Path

PROJECT = "maas-bird-network-lab-tf"
PRIVATE = Path(__file__).resolve().parents[2] / "runtime/terraform/maas-credentials"
URL = "https://10.203.241.10:5443/MAAS/api/2.0/version/"


def guest(*args):
    return subprocess.check_output(
        ["lxc", "exec", "--project", PROJECT, "maas", "--", *args],
        text=True,
        timeout=60,
    )


def test_owned_controller_and_strict_bootstrap():
    node = json.loads(
        subprocess.check_output(
            ["lxc", "query", f"/1.0/instances/maas?project={PROJECT}"],
            text=True,
            timeout=30,
        )
    )
    assert node["type"] == "virtual-machine"
    assert node["config"]["user.maas-bird-owner"] == "network-lab"
    assert node["config"]["limits.cpu"] == "1"
    assert node["config"]["limits.memory"] == "8GiB"
    assert node["config"]["boot.autostart"] == "false"
    status = json.loads(guest("cloud-init", "status", "--format=json"))
    assert status["extended_status"] == "done", status
    assert status["errors"] == [], status
    assert status["recoverable_errors"] == {}, status


def test_verified_external_api_and_private_credentials():
    for name in ("api-key", "ca.crt", "admin-password"):
        assert stat.S_IMODE((PRIVATE / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(PRIVATE.stat().st_mode) == 0o700
    version = json.loads(
        subprocess.check_output(
            [
                "curl",
                "--fail",
                "--silent",
                "--show-error",
                "--cacert",
                str(PRIVATE / "ca.crt"),
                URL,
            ],
            text=True,
            timeout=30,
        )
    )
    assert version["version"].startswith("3.8."), version
    untrusted = subprocess.run(
        [
            "curl",
            "--silent",
            "--show-error",
            "--cacert",
            "/etc/ssl/certs/ca-certificates.crt",
            URL,
        ],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert untrusted.returncode == 60, untrusted.stderr


def test_native_api_inventory():
    subnets = json.loads(guest("maas", "lab", "subnets", "read"))
    assert len([subnet for subnet in subnets if subnet["cidr"] == "10.203.241.0/24"]) == 1
    racks = json.loads(guest("maas", "lab", "rack-controllers", "read"))
    assert len(racks) == 1
    assert racks[0]["hostname"] == "maas"
