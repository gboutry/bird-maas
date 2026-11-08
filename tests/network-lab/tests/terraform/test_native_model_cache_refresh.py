"""Refresh stale model metadata using real Juju and a private client copy only."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import textwrap
import uuid

import yaml
from test_artifacts import ROOT, TERRAFORM, command


def file_hashes(directory):
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_native_identity_guard_refreshes_stale_model_cache_without_accepting_wrong_uuid(tmp_path):
    tmp_path.chmod(0o700)
    original = ROOT / "runtime/terraform/juju-client"
    original_hashes = file_hashes(original)
    client = tmp_path / "private-client"
    shutil.copytree(original, client)
    client.chmod(0o700)
    state = json.loads(
        (ROOT / "runtime/terraform/lab/50-juju-machines/terraform.tfstate").read_text()
    )
    expected = state["outputs"]["model_uuid"]["value"]
    cached = yaml.safe_load((client / "models.yaml").read_text())
    model = cached["controllers"]["nlab-tf"]["models"]["admin/network-lab"]
    assert model["uuid"] == expected
    model["uuid"] = str(uuid.uuid4())
    assert model["uuid"] != expected
    (client / "models.yaml").write_text(yaml.safe_dump(cached))
    env = {
        **os.environ,
        "JUJU_DATA": str(client),
        "JUJU": "/snap/juju/current/bin/juju",
        "MODEL": "nlab-tf:network-lab",
        "UUID": expected,
    }
    try:
        stale = subprocess.run(
            [env["JUJU"], "show-model", env["MODEL"], "--format=json"],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert stale.returncode != 0, "The copied stale cache must reproduce model lookup failure"
        for name in ("juju-local-applications", "juju-workloads"):
            shutil.copytree(
                ROOT / "terraform/modules" / name,
                tmp_path / name,
                ignore=shutil.ignore_patterns(".terraform", ".terraform.lock.hcl"),
            )
        module = tmp_path / "juju-local-applications"
        (module / "inputs.tfvars.json").write_text(
            json.dumps(
                {
                    "model": env["MODEL"],
                    "model_uuid": expected,
                    "client_directory": str(client),
                    "output_directory": str(tmp_path / "bundle"),
                    "charms": {},
                    "applications": {},
                }
            )
        )
        command(module, "init", "-backend=false", "-input=false", "-no-color")
        command(module, "validate", "-no-color")
        rendered = subprocess.run(
            [str(TERRAFORM), f"-chdir={module}", "console", "-var-file=inputs.tfvars.json"],
            input="jsonencode(local.identity_check)\n",
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        )
        identity = json.loads(json.loads(rendered.stdout))
        assert 'models --controller "${MODEL%%:*}"' in identity
        guarded = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", identity],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert guarded.returncode == 0, guarded.stdout + guarded.stderr
        refreshed = yaml.safe_load((client / "models.yaml").read_text())
        assert refreshed["controllers"]["nlab-tf"]["models"]["admin/network-lab"]["uuid"] == (
            expected
        )
        wrong = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", identity],
            env={**env, "UUID": str(uuid.uuid4())},
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert wrong.returncode == 1, "The strict UUID predicate must reject a different target"
        assert wrong.stderr == ""
        source = (module / "main.tf").read_text()
        destroy = source.split('resource "terraform_data" "application" {', 1)[1].split(
            'resource "terraform_data" "deploy" {', 1
        )[0]
        assert '"$JUJU" models --controller "$${MODEL%%:*}" --format=json >/dev/null' in destroy
        assert destroy.index('"$JUJU" models ') < destroy.index('"$JUJU" show-model ')
        assert "UUID = self.input.uuid" in destroy
        destroy_command = re.search(
            r"command\s*= <<-SCRIPT\n(.*?)\n    SCRIPT", destroy, re.DOTALL
        )
        assert destroy_command is not None
        prefix = "\n".join(textwrap.dedent(destroy_command.group(1)).splitlines()[:2])
        assert prefix.replace("$${", "${") == identity.strip()
    finally:
        assert file_hashes(original) == original_hashes
