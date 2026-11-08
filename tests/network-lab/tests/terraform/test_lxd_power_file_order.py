"""Validate native directory-before-file dependencies without provisioning a guest."""

import json
import re
import shutil
import subprocess

from test_artifacts import ROOT, TERRAFORM, command


def test_proxy_directory_is_prepared_before_parallel_private_files(tmp_path):
    module = tmp_path / "lxd-power"
    shutil.copytree(
        ROOT / "terraform/modules/lxd-power",
        module,
        ignore=shutil.ignore_patterns(".terraform", ".terraform.lock.hcl"),
    )
    (module / "inputs.tfvars.json").write_text(
        json.dumps({"project": "lab", "management_cidr": "10.203.241.0/24"})
    )
    command(module, "init", "-backend=false", "-input=false", "-no-color")
    command(module, "validate", "-no-color")
    command(module, "test", "-var-file=inputs.tfvars.json", "-no-color")
    graph = subprocess.check_output(
        [str(TERRAFORM), f"-chdir={module}", "graph"], text=True, timeout=30
    )
    assert '"lxd_instance_file.proxy" -> "terraform_data.proxy_package";' in graph
    source = (module / "proxy.tf").read_text()
    package = source.split('resource "terraform_data" "proxy_package" {', 1)[1].split(
        "\nlocals {", 1
    )[0]
    script = re.search(r"command = <<-EOF\n(.*?)\n    EOF", package, re.DOTALL)
    assert script is not None
    command_text = script.group(1)
    assert command_text.count("install -d -m 0700 /etc/nginx/lxd-power") == 1
    assert command_text.index("apt-get install -y nginx") < command_text.index(
        "install -d -m 0700 /etc/nginx/lxd-power"
    )
    subprocess.run(["bash", "-n"], input=command_text, text=True, check=True, timeout=10)
