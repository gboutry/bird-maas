"""Native local bundle artifacts and plan-only compatibility for constraints."""

import json
import shutil

import pytest
import yaml
from test_artifacts import ROOT, archive, command


@pytest.mark.parametrize("constraints", (None, "arch=amd64", ""))
def test_optional_constraints_are_emitted_only_when_supplied(tmp_path, constraints):
    source = tmp_path / "inventory.charm"
    archive(source, "version-one")
    output = tmp_path / "bundle"
    application = {"charm": "maas-inventory", "machine": "0", "options": {}}
    if constraints is not None:
        application["constraints"] = constraints
    module = ROOT / "terraform/modules/juju-workloads"
    (tmp_path / "main.tf").write_text(
        'variable "applications" { type = any }\n'
        'module "workloads" {\n'
        f"  source = {json.dumps(str(module))}\n"
        f"  output_directory = {json.dumps(str(output))}\n"
        f"  charms = {{ maas-inventory = {json.dumps(str(source))} }}\n"
        "  applications = var.applications\n"
        "}\n"
    )
    (tmp_path / "inputs.tfvars.json").write_text(
        json.dumps({"applications": {"inventory": application}})
    )
    command(tmp_path, "init", "-input=false", "-no-color")
    command(
        tmp_path,
        "apply",
        "-auto-approve",
        "-input=false",
        "-no-color",
        "-var-file=inputs.tfvars.json",
    )
    rendered = yaml.safe_load((output / "bundle.yaml").read_text())["applications"]["inventory"]
    if constraints is None:
        assert "constraints" not in rendered
    else:
        assert rendered["constraints"] == constraints
    assert rendered["to"] == ["0"] and rendered["num_units"] == 1
    command(
        tmp_path,
        "plan",
        "-detailed-exitcode",
        "-input=false",
        "-no-color",
        "-var-file=inputs.tfvars.json",
    )


def test_constraints_preserve_legacy_local_deployment_hash(tmp_path):
    for name in ("juju-local-applications", "juju-workloads"):
        shutil.copytree(
            ROOT / "terraform/modules" / name,
            tmp_path / name,
            ignore=shutil.ignore_patterns(".terraform", ".terraform.lock.hcl"),
        )
    module = tmp_path / "juju-local-applications"
    source = tmp_path / "inventory.charm"
    archive(source, "version-one")
    client = tmp_path / "client"
    client.mkdir(mode=0o700)
    (client / "controllers.yaml").write_text(
        yaml.safe_dump(
            {"controllers": {"nlab-tf": {"api-endpoints": ["127.0.0.1:1"], "ca-cert": "mock-ca"}}}
        )
    )
    (client / "accounts.yaml").write_text(
        yaml.safe_dump({"controllers": {"nlab-tf": {"user": "mock-user", "password": "mock"}}})
    )
    (module / "inputs.tfvars.json").write_text(
        json.dumps(
            {
                "model": "nlab-tf:network-lab",
                "model_uuid": "model-uuid",
                "client_directory": str(client),
                "output_directory": str(tmp_path / "bundle"),
                "charms": {"maas-inventory": str(source)},
                "applications": {},
            }
        )
    )
    command(module, "init", "-backend=false", "-input=false", "-no-color")
    command(module, "validate", "-no-color")
    command(module, "test", "-var-file=inputs.tfvars.json", "-no-color")
