"""Render both real provisioner command templates with native Terraform."""

import json
import re
import shutil
import subprocess

from test_artifacts import ROOT, TERRAFORM, command


def test_frr_bootstrap_deadline_reaches_both_provisioners(tmp_path):
    module = tmp_path / "frr"
    shutil.copytree(
        ROOT / "terraform/modules/frr",
        module,
        ignore=shutil.ignore_patterns(".terraform", ".terraform.lock.hcl"),
    )
    (module / "fixture.tfvars.json").write_text(
        json.dumps(
            {
                "project": "lab",
                "management_network": "management",
                "management_cidr": "10.203.241.0/24",
                "core_networks": {
                    f"{tor}-s{index}": f"{tor}-s{index}"
                    for tor in ("r1a", "r1b", "r2a", "r2b")
                    for index in (0, 1)
                },
                "fast_networks": {
                    f"r{rack}-pf{pf}": f"r{rack}-pf{pf}" for rack in (1, 2) for pf in (0, 1)
                },
            }
        )
    )
    ready_source = (
        (module / "lldp.tf")
        .read_text()
        .split('resource "terraform_data" "ready" {', 1)[1]
        .split('resource "lxd_instance_file"', 1)[0]
    )
    expression = re.search(r'command\s*= ("(?:\\.|[^"\\])*")', ready_source)
    assert expression is not None
    ready = json.loads(expression[1])
    mtu_source = (module / "mtu.tf").read_text()
    heredoc = re.search(r"command\s*= <<-SCRIPT\n(.*?)\n    SCRIPT", mtu_source, re.DOTALL)
    assert heredoc is not None
    for name, template in (("ready", ready), ("core_mtu", heredoc[1])):
        assert (
            template.count("timeout ${var.cloud_init_timeout_seconds} cloud-init status --wait")
            == 1
        )
        (module / f"{name}.tftpl").write_text(template)
    command(module, "init", "-backend=false", "-input=false", "-no-color")
    command(module, "validate", "-no-color")
    command(module, "test", "-var-file=fixture.tfvars.json", "-no-color")
    for override, expected in ((None, 300), (1200, 1200)):
        for name in ("ready", "core_mtu"):
            args = [str(TERRAFORM), f"-chdir={module}", "console", "-var-file=fixture.tfvars.json"]
            if override is not None:
                args.append(f"-var=cloud_init_timeout_seconds={override}")
            result = subprocess.run(
                args,
                input=(
                    f'jsonencode(templatefile("{name}.tftpl", '
                    "{var={cloud_init_timeout_seconds=var.cloud_init_timeout_seconds}}))\n"
                ),
                text=True,
                capture_output=True,
                check=True,
                timeout=30,
            )
            rendered = json.loads(json.loads(result.stdout))
            assert f"timeout {expected} cloud-init status --wait" in rendered
            subprocess.run(["sh", "-n"], input=rendered, text=True, check=True, timeout=10)
