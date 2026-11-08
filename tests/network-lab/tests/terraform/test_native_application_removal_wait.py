"""Exercise the application's real removal-wait command without removing an app."""

import json
import os
import re
import subprocess

from test_artifacts import ROOT
from test_native_applications import cli


def test_native_removal_wait_accepts_an_absent_application():
    source = (ROOT / "terraform/modules/juju-local-applications/main.tf").read_text()
    application_resource = source.split('resource "terraform_data" "application" {', 1)[1]
    application_resource = application_resource.split('resource "terraform_data" "deploy" {', 1)[0]
    assert 'query  = "forEach(applications, app => app.name != ${jsonencode(each.key)})"' in (
        application_resource
    )
    commands = re.findall(r'^\s*("\$JUJU" wait-for model .*)$', application_resource, re.MULTILINE)
    assert len(commands) == 1
    command = commands[0]
    assert '--query="len(applications) == 0 || ($QUERY)"' in command

    model = next(iter(cli("show-model").values()))
    state = json.loads(
        (ROOT / "runtime/terraform/lab/50-juju-machines/terraform.tfstate").read_text()
    )
    assert model["model-uuid"] == state["outputs"]["model_uuid"]["value"]
    status = cli("status")
    absent = "network-lab-removed-regression"
    assert absent not in status.get("applications", {})
    # Execute only the production wait command; the destroy provisioner also removes apps.
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", "exec " + command],
        env={
            **os.environ,
            "JUJU": "/snap/juju/current/bin/juju",
            "JUJU_DATA": str(ROOT / "runtime/terraform/juju-client"),
            "MODEL": "nlab-tf:network-lab",
            "QUERY": f"forEach(applications, app => app.name != {json.dumps(absent)})",
        },
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
