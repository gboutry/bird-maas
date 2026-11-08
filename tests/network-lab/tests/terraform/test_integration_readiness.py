"""Plan the post-relation barrier and render its actual first-class CLI command."""

import json
import os
import re
import shutil
import subprocess
import sys

import pytest
from test_artifacts import ROOT, TERRAFORM, command


def test_post_relation_readiness_defaults_guards_and_native_command(tmp_path):
    module = tmp_path / "integrations"
    shutil.copytree(
        ROOT / "terraform/modules/juju-integrations",
        module,
        ignore=shutil.ignore_patterns(".terraform", ".terraform.lock.hcl"),
    )
    workers = dict(
        zip(
            ("probe", "probe-alt", "probe-r2a", "probe-r2b"),
            ("r1a", "r1b", "r2a", "r2b"),
            strict=True,
        )
    )
    (module / "fixture.tfvars.json").write_text(
        json.dumps(
            {
                "controller_addresses": ["127.0.0.1:1"],
                "controller_ca": "fixture-ca",
                "controller_username": "fixture-user",
                "controller_password": "fixture-password",
                "model_uuid": "00000000-0000-4000-8000-000000000001",
                "worker_applications": workers,
                "secret_ids": {"maas": "maas-id", "ca": "ca-id", "bgp": workers},
            }
        )
    )
    (module / "tests").mkdir()
    (module / "tests/readiness.tftest.hcl").write_text(
        """mock_provider "juju" { override_during = plan }
run "default_api_only" {
 command = plan
 assert {
  condition = length(terraform_data.ready) == 0 && output.ready == null
  error_message = "Existing callers must not run a CLI readiness wait."
 }
}
run "native_ready" {
 command = plan
 variables {
  wait_for_ready = true
  model = "nlab-tf:network-lab"
  client_directory = "/private/native-client"
  deployment_generation = "actual-deploy-refresh-hash"
  worker_generations = { probe = "model/1" }
 }
 assert {
  condition = (
   length(terraform_data.ready) == 1 &&
   terraform_data.ready[0].triggers_replace.deployment_generation ==
    "actual-deploy-refresh-hash" &&
   terraform_data.ready[0].triggers_replace.workers.probe == "model/1"
  )
  error_message = "Readiness must retain actual application and worker generations."
 }
 assert {
  condition = (
   length(juju_access_secret.bgp) == 4 && length(juju_integration.inventory) == 4 &&
   length(juju_integration.attachments) == 4
  )
  error_message = "The fixture requires six grants and eight relations."
 }
}
run "reject_missing_generation" {
 command = plan
 variables {
  wait_for_ready = true
  model = "nlab-tf:network-lab"
 }
 expect_failures = [terraform_data.ready]
}
run "reject_blank_generation" {
 command = plan
 variables {
  wait_for_ready = true
  model = "nlab-tf:network-lab"
  deployment_generation = " "
 }
 expect_failures = [terraform_data.ready]
}
run "reject_empty_workers" {
 command = plan
 variables {
  wait_for_ready = true
  model = "nlab-tf:network-lab"
  deployment_generation = "actual-generation"
  worker_applications = {}
 }
 expect_failures = [terraform_data.ready]
}
"""
    )
    command(module, "init", "-backend=false", "-input=false", "-no-color")
    command(module, "validate", "-no-color")
    command(module, "test", "-var-file=fixture.tfvars.json", "-no-color")
    source = (module / "main.tf").read_text()
    ready = source.split('resource "terraform_data" "ready" {', 1)[1]
    expression = re.search(r"command\s*= <<-SCRIPT\n(.*?)\n    SCRIPT", ready, re.DOTALL)
    assert expression is not None
    (module / "ready.tftpl").write_text(expression[1])
    result = subprocess.run(
        [str(TERRAFORM), f"-chdir={module}", "console", "-var-file=fixture.tfvars.json"],
        input=(
            'jsonencode({command=templatefile("ready.tftpl", '
            "{local={identity_check=local.identity_check}}),query=local.ready_query})\n"
        ),
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    rendered = json.loads(json.loads(result.stdout))
    assert rendered["command"].count('"$JUJU" models --controller') == 3
    assert rendered["command"].count('[.[] | .["model-uuid"]] == [$expected]') == 3
    assert (
        '"$JUJU" wait-for model "$MODEL" --timeout="$attempt"s --summary=false --query="$QUERY"'
        in rendered["command"]
    )
    assert rendered["query"] == (
        'life == "alive" && status == "available" && len(applications) == 9 && len(units) == 9'
        ' && forEach(applications, app => app.status == "active")'
        ' && forEach(units, unit => unit.workload-status == "active"'
        ' && unit.agent-status == "idle")'
    )
    subprocess.run(["bash", "-n"], input=rendered["command"], text=True, check=True, timeout=10)
    graph = subprocess.run(
        [str(TERRAFORM), f"-chdir={module}", "graph", "-type=plan"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    for resource in (
        "juju_access_secret.maas",
        "juju_access_secret.ca",
        "juju_access_secret.bgp",
        "juju_integration.inventory",
        "juju_integration.attachments",
    ):
        assert f'"[root] terraform_data.ready (expand)" -> "[root] {resource} (expand)"' in graph
    native = (ROOT / "terraform/live/lab/80-integrations/terragrunt.hcl").read_text()
    assert 'try(dependency.applications.outputs.deployment_generation, "")' in native
    assert '"${dependency.model.outputs.model_uuid}/${machine}"' in native


def test_deployment_generation_hash_uses_native_deploy_and_refresh_ids(tmp_path):
    source = (ROOT / "terraform/modules/juju-local-applications/main.tf").read_text()
    output = source.split('output "deployment_generation" {', 1)[1]
    expression = output.split("value = ", 1)[1].rsplit("\n}", 1)[0].strip()
    expression = expression.replace("terraform_data.deploy.id", "var.deploy").replace(
        "terraform_data.refresh", "var.refresh"
    )
    (tmp_path / "main.tf").write_text(
        'variable "deploy" {\n type = string\n default = "deploy-id"\n}\n'
        'variable "refresh" {\n type = map(object({id=string}))\n'
        ' default = { bird = { id = "refresh-id" } }\n}\n'
        f"locals {{ generation = {expression} }}\n"
    )
    command(tmp_path, "init", "-backend=false", "-input=false", "-no-color")
    command(tmp_path, "validate", "-no-color")
    hashes = []
    for override in (None, "deploy=new-deploy-id", 'refresh={bird={id="new-refresh-id"}}'):
        args = [str(TERRAFORM), f"-chdir={tmp_path}", "console"]
        if override:
            args.append("-var=" + override)
        result = subprocess.run(
            args,
            input="local.generation\n",
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        hashes.append(json.loads(result.stdout))
    assert len(set(hashes)) == 3 and all(len(value) == 64 for value in hashes)
    assert (
        "module.applications.deployment_generation"
        in (ROOT / "terraform/modules/lab-applications/main.tf").read_text()
    )


def render_readiness_command(directory):
    source = (ROOT / "terraform/modules/juju-integrations/main.tf").read_text()
    locals_block = re.search(
        r'\nlocals \{(.*?)\n\}\nresource "terraform_data" "ready"', source, re.DOTALL
    )
    assert locals_block is not None
    ready = source.split('resource "terraform_data" "ready" {', 1)[1]
    expression = re.search(r"command\s*= (<<-SCRIPT\n.*?\n    SCRIPT)", ready, re.DOTALL)
    assert expression is not None
    directory.mkdir()
    (directory / "main.tf").write_text(
        'variable "client_directory" { default = "" }\n'
        'variable "worker_applications" {\n'
        ' default = {probe="r1a",probe-alt="r1b",probe-r2a="r2a",probe-r2b="r2b"}\n}\n'
        + "locals {"
        + locals_block[1]
        + f"\n barrier_command = {expression[1]}\n}}\n"
    )
    command(directory, "init", "-backend=false", "-input=false", "-no-color")
    result = subprocess.run(
        [str(TERRAFORM), f"-chdir={directory}", "console"],
        input="jsonencode({command=local.barrier_command,query=local.ready_query})\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(json.loads(result.stdout))


@pytest.mark.parametrize(
    ("scenario", "clock", "expected_waits", "success"),
    (
        ("stale_then_fresh", [0, 0, 10, 11, 11], 2, True),
        ("permanently_false", [0, 0, 597, 600], 2, False),
        ("wrong_uuid", [0], 0, False),
        ("late_positive", [0, 0, 600], 1, False),
        ("changed_after_failure", [0, 0], 1, False),
        ("changed_after_success", [0, 0, 1], 1, False),
    ),
)
def test_fresh_native_queries_share_one_budget_and_preserve_guards(
    tmp_path, scenario, clock, expected_waits, success
):
    rendered = render_readiness_command(tmp_path / "render")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    calls = tmp_path / "calls.jsonl"
    clock_index = tmp_path / "clock-index"
    clock_index.write_text("0")
    juju = binaries / "juju"
    juju.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, pathlib, sys
args = sys.argv[1:]
path = pathlib.Path(os.environ["CLI_CALLS"])
history = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
with path.open("a") as stream:
    stream.write(json.dumps({"args": args, "group": os.getpgrp()}) + "\\n")
mode = os.environ["SCENARIO"]
waits = sum(item["args"][:1] == ["wait-for"] for item in history)
if args[:1] == ["models"]:
    print("{}")
elif args[:1] == ["show-model"]:
    wrong = mode == "wrong_uuid" or (
        waits > 0 and mode in {"changed_after_failure", "changed_after_success"}
    )
    value = "different-uuid" if wrong else os.environ["UUID"]
    print(json.dumps({"network-lab": {"model-uuid": value}}))
elif args[:1] == ["wait-for"]:
    failed = mode in {"permanently_false", "changed_after_failure"} or (
        mode == "stale_then_fresh" and waits == 0
    )
    sys.exit(1 if failed else 0)
else:
    sys.exit(2)
"""
    )
    date = binaries / "date"
    date.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, pathlib, sys
assert sys.argv[1:] == ["+%s"]
values = json.loads(os.environ["CLOCK_VALUES"])
path = pathlib.Path(os.environ["CLOCK_INDEX"])
index = int(path.read_text())
path.write_text(str(index + 1))
print(values[min(index, len(values) - 1)])
"""
    )
    juju.chmod(0o700)
    date.chmod(0o700)
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", rendered["command"]],
        env={
            **os.environ,
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "JUJU": str(juju),
            "MODEL": "nlab-tf:network-lab",
            "UUID": "00000000-0000-4000-8000-000000000001",
            "QUERY": rendered["query"],
            "CLI_CALLS": str(calls),
            "SCENARIO": scenario,
            "CLOCK_VALUES": json.dumps(clock),
            "CLOCK_INDEX": str(clock_index),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) is success, result.stdout + result.stderr
    observed = [json.loads(line) for line in calls.read_text().splitlines()]
    waits = [item["args"] for item in observed if item["args"][:1] == ["wait-for"]]
    assert len(waits) == expected_waits
    for args in waits:
        assert args[:3] == ["wait-for", "model", "nlab-tf:network-lab"]
        assert args[4:] == ["--summary=false", "--query=" + rendered["query"]]
    if waits:
        assert waits[0][3] == "--timeout=10s"
    if scenario == "permanently_false":
        assert waits[1][3] == "--timeout=3s"
    assert all(item["group"] == os.getpgrp() for item in observed)
    assert "deadline=$(($(date +%s) + 600))" in rendered["command"]
