"""Require the actual owned provider fabric and preserve native acceptance output."""

import importlib
import json
import subprocess
from pathlib import Path

import pytest
from maas_bird.runtime import write_atomic

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "maas-bird-network-lab"
NODES = ("spine0", "spine1", "r1a", "r1b", "r2a", "r2b")


@pytest.fixture(scope="module")
def provider(request):
    records = []

    def guest(*args):
        result = subprocess.run(
            ["lxc", "exec", "--project", PROJECT, "fabric", "--", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        records.append({"argv": args, "stdout": result.stdout, "stderr": result.stderr})
        return result.stdout.strip()

    instance = json.loads(
        subprocess.check_output(
            ["lxc", "query", f"/1.0/instances/fabric?project={PROJECT}"], text=True, timeout=30
        )
    )
    assert instance["type"] == "virtual-machine"
    assert instance["config"]["user.maas-bird-owner"] == "network-lab"
    assert instance["config"]["limits.cpu"] == "2"
    assert instance["config"]["limits.memory"] == "8GiB"
    model = json.loads((ROOT / "containerlab/provider.json").read_text())
    manifest = json.loads((ROOT / "runtime/host-fabric/public-plan.json").read_text())
    assert manifest["provider_intent"] == model
    assert guest("systemctl", "is-active", "network-lab-provider.service") == "active"
    for node in NODES:
        labels = json.loads(
            guest(
                "docker",
                "inspect",
                "--format",
                "{{json .Config.Labels}}",
                "clab-maas-bird-fabric-" + node,
            )
        )
        assert labels["containerlab"] == "maas-bird-fabric"
    try:
        yield model, guest
    finally:
        output = ROOT / "runtime/provider"
        output.mkdir(mode=0o700, exist_ok=True, parents=True)
        write_atomic(
            output / (request.node.path.stem + ".json"),
            json.dumps(
                {
                    "boundary": "Native provider L2 transport; no OVN gateway proof",
                    "model": model,
                    "native_commands": records,
                },
                indent=2,
            )
            + "\n",
            0o600,
        )


WORKERS = ("probe", "probe-alt", "probe-r2a", "probe-r2b")


@pytest.fixture(scope="module")
def gateways():
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(ROOT / "scripts"))
        lab = importlib.import_module("deploy_ovn")
        expectations = importlib.import_module("routing_expectations")
        identities = lab.owned()
        model = expectations.gateway_intent()
        assert model is not None and model["hardware_emulation"] is False
        endpoints = json.loads((ROOT / "runtime/gateways/ovn-intent.json").read_text())
        assert endpoints["model"] == model and set(endpoints["endpoints"]) == set(WORKERS)
        assert all(endpoints["endpoints"][n]["uuid"] == identities[n] for n in WORKERS)
        yield lab, model, endpoints["endpoints"], expectations
