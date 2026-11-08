"""Render actual OVN install commands; no APT command or guest provisioner runs."""

import json
import re
import shutil
import subprocess

from test_artifacts import ROOT, TERRAFORM, command


def test_ovn_install_commands_use_native_bounded_frontend_lock_wait(tmp_path):
    source = ROOT / "terraform/modules/ovn"
    ppa = (source / "ppa.tf").read_text().split('resource "terraform_data" "ppa" {', 1)[1]
    heredoc = re.search(r"command\s*= (<<-SCRIPT\n.*?\n    SCRIPT)", ppa, re.DOTALL)
    assert heredoc is not None
    package = (source / "main.tf").read_text().split('resource "terraform_data" "package" {', 1)[1]
    expression = re.search(r'command\s*= ("(?:\\.|[^"\\])*")', package)
    assert expression is not None
    fixture = tmp_path / "commands"
    fixture.mkdir()
    (fixture / "main.tf").write_text(
        f"locals {{\n ppa = {heredoc[1]}\n package = {expression[1]}\n}}\n"
    )
    command(fixture, "init", "-backend=false", "-input=false", "-no-color")
    command(fixture, "validate", "-no-color")
    result = subprocess.run(
        [str(TERRAFORM), f"-chdir={fixture}", "console"],
        input="jsonencode({ppa=local.ppa,package=local.package})\n",
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    rendered = json.loads(json.loads(result.stdout))
    for install in rendered.values():
        assert install.count("-o DPkg::Lock::Timeout=600") == 1
        assert "--no-install-recommends" in install
        subprocess.run(["sh", "-n"], input=install, text=True, check=True, timeout=10)
    assert (
        "apt-get -o DPkg::Lock::Timeout=600 --no-remove --no-install-recommends "
        'install -y ovn-common "ovn-$1"'
    ) in rendered["ppa"]
    for retained in (
        "apt-get -o APT::Update::Error-Mode=any update -qq",
        'dpkg --compare-versions "$candidate" ge 24.03.9',
        'test "$(dpkg-query -W -f=\\$\\{Version\\} "ovn-$1")" = "$candidate"',
        'test "$(dpkg-query -W -f=\\$\\{Version\\} ovn-common)" = "$candidate"',
        "systemctl restart ovn-central",
        "systemctl restart ovn-controller",
        "ovn-nbctl --timeout=30 show",
        "ovn-sbctl --timeout=30 show",
    ):
        assert retained in rendered["ppa"]
    assert (
        "apt-get update -qq; apt-get -o DPkg::Lock::Timeout=600 install -y" in rendered["package"]
    )
    module = tmp_path / "ovn"
    shutil.copytree(
        source, module, ignore=shutil.ignore_patterns(".terraform", ".terraform.lock.hcl")
    )
    command(module, "init", "-backend=false", "-input=false", "-no-color")
    command(module, "validate", "-no-color")
