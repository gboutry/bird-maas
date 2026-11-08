"""Read native Juju application placement, relations and secret grants."""

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest
from maas_bird.runtime import write_atomic

ROOT = Path(__file__).resolve().parents[2]
HOSTS = {"probe": "r1a", "probe-alt": "r1b", "probe-r2a": "r2a", "probe-r2b": "r2b"}


def cli(*arguments):
    return json.loads(
        subprocess.check_output(
            [
                "juju",
                *arguments,
                *(
                    ["nlab-tf:network-lab"]
                    if arguments[0] == "show-model"
                    else ["--model=nlab-tf:network-lab"]
                ),
                "--format=json",
            ],
            env={**os.environ, "JUJU_DATA": str(ROOT / "runtime/terraform/juju-client")},
            text=True,
            timeout=60,
        )
    )


@pytest.fixture(scope="module")
def status():
    result = cli("status")
    model = next(iter(cli("show-model").values()))
    result["model"]["uuid"] = model["model-uuid"]
    path = ROOT / "runtime/terraform/lab/50-juju-machines/terraform.tfstate"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    state = json.loads(path.read_text())
    assert result["model"]["uuid"] == state["outputs"]["model_uuid"]["value"]
    expected = {"inventory"} | {
        f"{kind}-{suffix}" for kind in ("nic", "bird") for suffix in HOSTS.values()
    }
    assert set(result["applications"]) == expected
    assert len(result["machines"]) == 4
    assert result["applications"]["inventory"]["application-status"]["current"] == "active"
    return result


@pytest.mark.parametrize("host,suffix", HOSTS.items())
def test_placement_and_relations(status, host, suffix):
    state = json.loads(
        (ROOT / "runtime/terraform/lab/40-networking/terraform.tfstate").read_text()
    )
    identity = state["outputs"]["workers"]["value"][host]["system_id"]
    machine = next(
        key for key, value in status["machines"].items() if value["instance-id"] == identity
    )
    for kind in ("bird", "nic"):
        application = status["applications"][f"{kind}-{suffix}"]
        assert len(application["units"]) == 1
        assert next(iter(application["units"].values()))["machine"] == machine
    relations = status["applications"][f"bird-{suffix}"]["relations"]
    assert relations["inventory"] == [
        {"related-application": "inventory", "interface": "machine-inventory", "scope": "global"}
    ]
    assert relations["attachments"] == [
        {
            "related-application": f"nic-{suffix}",
            "interface": "host-attachments",
            "scope": "global",
        }
    ]


def test_secret_grants(status):
    state = json.loads((ROOT / "runtime/terraform/lab/60-secrets/terraform.tfstate").read_text())
    outputs = {key: value["value"] for key, value in state["outputs"].items()}
    grants = {outputs["maas_secret_uri"]: "inventory", outputs["maas_ca_secret_uri"]: "inventory"}
    grants.update({uri: f"bird-{HOSTS[host]}" for host, uri in outputs["bgp_secret_uris"].items()})
    for uri, application in grants.items():
        row = next(iter(cli("show-secret", uri).values()))
        assert row["access"] == [
            {
                "target": f"application-{application}",
                "scope": f"model-{status['model']['uuid']}",
                "role": "view",
            }
        ]


@pytest.mark.parametrize("host,suffix", HOSTS.items())
def test_native_bird_plan_admission(status, host, suffix):
    units = status["applications"][f"bird-{suffix}"]["units"]
    assert len(units) == 1
    unit = next(iter(units))
    result = cli("run", unit, "plan", "--wait=40s")
    directory = ROOT / "runtime/terraform/acceptance/bird-plans"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_atomic(
        directory / f"{host}-{time.time_ns()}.json", json.dumps(result, indent=2) + "\n", 0o600
    )
    assert set(result) == {unit}, result
    task = result[unit]
    assert task["status"] == "completed", task.get("message", task)
    assert json.loads(task["results"]["plan"]), task
