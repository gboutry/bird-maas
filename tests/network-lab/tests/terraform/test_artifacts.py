"""Exercise local artifact replacement through the actual Terraform CLI."""

import json
import stat
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = ROOT / "terraform/bin/terraform"


def command(directory, *args, expected=0):
    result = subprocess.run(
        [str(TERRAFORM), f"-chdir={directory}", *args],
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == expected, result.stdout + result.stderr


def archive(path, version):
    with zipfile.ZipFile(path, "w") as bundle:
        bundle.writestr("metadata.yaml", "name: maas-inventory\n")
        bundle.writestr("version", version)


def test_source_archive_change_replaces_stable_copy(tmp_path):
    source = tmp_path / "inventory.charm"
    output = tmp_path / "bundle"
    archive(source, "version-one")
    module = ROOT / "terraform/modules/juju-workloads"
    (tmp_path / "main.tf").write_text(
        'module "workloads" {\n'
        f"  source = {json.dumps(str(module))}\n"
        f"  output_directory = {json.dumps(str(output))}\n"
        f"  charms = {{ maas-inventory = {json.dumps(str(source))} }}\n"
        '  applications = { inventory = { charm = "maas-inventory",'
        ' machine = "0", options = {} } }\n'
        "}\n"
    )
    command(tmp_path, "init", "-input=false", "-no-color")
    command(tmp_path, "apply", "-auto-approve", "-input=false", "-no-color")
    destination = output / "maas-inventory"
    assert destination.read_bytes() == source.read_bytes()
    first = destination.read_bytes()
    archive(source, "version-two")
    command(tmp_path, "apply", "-auto-approve", "-input=false", "-no-color")
    assert destination.read_bytes() != first
    assert destination.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE((output / "bundle.yaml").stat().st_mode) == 0o600
    command(tmp_path, "plan", "-detailed-exitcode", "-input=false", "-no-color")
