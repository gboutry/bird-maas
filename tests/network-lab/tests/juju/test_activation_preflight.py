"""Plain role activation must refuse an installed gateway fabric before mutation."""

import importlib
import json
import subprocess
import sys
from pathlib import Path

import jubilant
import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "script,arguments,diagnostic",
    [
        ("activate_roles.py", [], "cannot replace installed provider/gateway services"),
        ("bootstrap_fabric.py", ["--apply"], "Seed bootstrap refuses an installed fabric"),
    ],
)
def test_gateway_fabric_refuses_plain_activation_without_changing_apps(
    script, arguments, diagnostic
):
    host = importlib.import_module("host_acceptance")
    installed = host.root(
        "fabric", "systemctl", "is-active", "network-lab-provider", "network-lab-gateways"
    ).stdout.split()
    assert installed == ["active", "active"]
    juju = jubilant.Juju(model="network-lab:network-lab")
    apps = [prefix + suffix for prefix in ("bird-", "nic-") for suffix in host.WORKERS.values()]

    def configs():
        return {app: json.loads(juju.cli("config", app, "--format=json")) for app in apps}

    before = configs()
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *arguments],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert diagnostic in result.stderr
    assert configs() == before
    for name in host.WORKERS:
        host.routing_ready(name)
        host.overlay_ready(name)
