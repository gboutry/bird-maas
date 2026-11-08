"""Render the real switch network provisioner without executing guest commands."""

import json
import re
import shutil
import subprocess

from test_artifacts import ROOT, TERRAFORM, command


def test_networkd_enable_precedes_restart_and_reconfigure_on_tor_and_spine(tmp_path):
    module = tmp_path / "frr-routing"
    shutil.copytree(
        ROOT / "terraform/modules/frr-routing",
        module,
        ignore=shutil.ignore_patterns(".terraform", ".terraform.lock.hcl"),
    )
    source = (module / "main.tf").read_text().split('resource "terraform_data" "network" {', 1)[1]
    heredoc = re.search(r"command\s*= <<-SCRIPT\n(.*?)\n    SCRIPT", source, re.DOTALL)
    assert heredoc is not None
    (module / "network-command.tftpl").write_text(heredoc[1])
    (module / "fixture.tfvars.json").write_text(
        json.dumps(
            {
                "project": "lab",
                "switch_uuids": {},
                "workers": {},
                "roles_path": str(ROOT / "maas/network-model/logical-roles.json"),
                "bgp_passwords": {},
            }
        )
    )
    command(module, "init", "-backend=false", "-input=false", "-no-color")
    command(module, "validate", "-no-color")
    for node, interfaces in (
        ("r1a", "access replication spine0 spine1 fast0 br-provider vx-provider"),
        ("spine0", "access replication r1a r1b r2a r2b"),
    ):
        result = subprocess.run(
            [str(TERRAFORM), f"-chdir={module}", "console", "-var-file=fixture.tfvars.json"],
            input=(
                'jsonencode(templatefile("network-command.tftpl", '
                f'{{each={{key="{node}",value=local.nodes["{node}"]}},'
                "local={tors=local.tors}}))\n"
            ),
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        rendered = json.loads(json.loads(result.stdout))
        statements = [line.strip() for line in rendered.splitlines()]
        assert statements == [
            'lxc exec --project "$PROJECT" "$INSTANCE" -- sh -ceu \'',
            "systemctl enable systemd-networkd",
            "systemctl restart systemd-networkd",
            f"networkctl reconfigure {interfaces}",
            "systemctl restart frr",
            "'",
        ]
        subprocess.run(["sh", "-n"], input=rendered, text=True, check=True, timeout=10)
