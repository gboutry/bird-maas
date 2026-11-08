"""Native plans, dependency graph and forget-only state handoff; no guest API calls."""

import json
import re
import shutil
import subprocess

from test_artifacts import ROOT, TERRAFORM, command


def test_worker_configuration_generation_and_application_ownership(tmp_path):
    for name in ("worker-config", "lab-applications", "juju-local-applications", "juju-workloads"):
        shutil.copytree(
            ROOT / "terraform/modules" / name,
            tmp_path / name,
            ignore=shutil.ignore_patterns(".terraform", ".terraform.lock.hcl"),
        )
    worker = tmp_path / "worker-config"
    (worker / "inputs.tfvars.json").write_text(
        json.dumps(
            {
                "workers": {
                    name: {
                        "project": "lab",
                        "uuid": "instance-uuid",
                        "generation": "deployment-one",
                        "networkd": "networkd-content",
                        "lldp": "lldp-content",
                    }
                    for name in ("probe", "probe-alt")
                }
            }
        )
    )
    command(worker, "init", "-backend=false", "-input=false", "-no-color")
    command(worker, "validate", "-no-color")
    command(worker, "test", "-var-file=inputs.tfvars.json", "-no-color")
    applications = tmp_path / "lab-applications"
    command(applications, "init", "-backend=false", "-input=false", "-no-color")
    command(applications, "validate", "-no-color")
    graph = subprocess.check_output(
        [str(TERRAFORM), f"-chdir={applications}", "graph"], text=True, timeout=30
    )
    edges = re.findall(r'"([^"\n]+)" -> "([^"\n]+)"', graph)
    adjacency = {}
    for source, destination in edges:
        adjacency.setdefault(source, set()).add(destination)
    for file in ("networkd", "lldp"):
        pending = [f"module.worker_config.lxd_instance_file.{file}"]
        reached = set()
        while pending:
            node = pending.pop()
            if node not in reached:
                reached.add(node)
                pending.extend(adjacency.get(node, ()))
        assert "module.applications.terraform_data.application" in reached, graph
    detailed_graph = subprocess.check_output(
        [str(TERRAFORM), f"-chdir={applications}", "graph", "-type=plan"],
        text=True,
        timeout=30,
    )
    for output in ("enabled", "worker_applications"):
        assert (
            f'"[root] output.{output} (expand)" -> "[root] module.worker_config (close)"'
            in detailed_graph
        )
    for restart in ("networkd", "lldp"):
        assert (
            '"[root] module.worker_config.output.workers (expand)" -> '
            f'"[root] module.worker_config.terraform_data.{restart} (expand)"' in detailed_graph
        )


def test_stage50_handoff_forgets_existing_records_without_deleting(tmp_path):
    provider = """terraform {
  required_version = ">= 1.7"
  required_providers { lxd = { source = "terraform-lxd/lxd", version = "3.1.1" } }
}
"""
    legacy, handoff = tmp_path / "legacy", tmp_path / "handoff"
    legacy.mkdir()
    handoff.mkdir()
    declarations = []
    for name, path in (
        ("networkd", "/etc/systemd/networkd.conf.d/90-network-lab.conf"),
        ("lldp", "/etc/default/lldpd"),
    ):
        declarations.append(
            f'resource "lxd_instance_file" "{name}" {{\n'
            '  for_each = toset(["probe", "probe-alt"])\n'
            "  instance = each.key\n"
            '  project = "lab"\n'
            f"  target_path = {json.dumps(path)}\n"
            '  content = "existing-content"\n'
            "}\n"
            f'resource "terraform_data" "{name}" {{\n'
            '  for_each = toset(["probe", "probe-alt"])\n'
            '  input = "existing-generation"\n'
            "}\n"
        )
    (legacy / "main.tf").write_text(provider + "".join(declarations))
    removed = []
    for name in ("networkd", "lldp"):
        source = (ROOT / f"terraform/modules/juju-machines/{name}.tf").read_text()
        removed.extend(re.findall(r"removed \{\n.*?\n\}", source, re.DOTALL))
    assert len(removed) == 4
    (handoff / "main.tf").write_text(provider + "\n".join(removed))
    (tmp_path / "main.tf").write_text(provider)
    (tmp_path / "ownership.tftest.hcl").write_text(
        """mock_provider "lxd" {}
run "existing_records" {
  command = apply
  state_key = "ownership"
  module { source = "./legacy" }
}
run "handoff_plan" {
  command = plan
  state_key = "ownership"
  module { source = "./handoff" }
}
run "handoff" {
  command = apply
  state_key = "ownership"
  module { source = "./handoff" }
}
"""
    )
    command(tmp_path, "init", "-backend=false", "-input=false", "-no-color")
    result = subprocess.run(
        [str(TERRAFORM), f"-chdir={tmp_path}", "test", "-verbose", "-no-color"],
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    plan = result.stdout.split('run "handoff_plan"... pass', 1)[1].split(
        'run "handoff"... pass', 1
    )[0]
    expected = {
        f'{kind}.{name}["{worker}"]'
        for kind in ("lxd_instance_file", "terraform_data")
        for name in ("networkd", "lldp")
        for worker in ("probe", "probe-alt")
    }
    forgotten = set(re.findall(r"# (\S+) will no longer be managed by Terraform", plan))
    assert forgotten == expected, plan
    assert "0 to add, 0 to change, 0 to destroy" in plan
    assert "The state file is empty. No resources are represented." in result.stdout
