"""Check image-dependent MAAS defaults against the real Terraform module graph."""

import json
import shutil
import subprocess

from test_artifacts import ROOT, TERRAFORM, command


def test_image_defaults_follow_completed_image_import(tmp_path):
    module = tmp_path / "maas-config"
    shutil.copytree(
        ROOT / "terraform/modules/maas-config",
        module,
        ignore=shutil.ignore_patterns(".terraform", ".terraform.lock.hcl"),
    )
    key = module / "mock-api-key"
    key.write_text("mock-key")
    (module / "fixture.tfvars.json").write_text(
        json.dumps(
            {
                "api_url": "https://127.0.0.1:1/MAAS",
                "api_key_path": str(key),
                "ca_certificate_path": str(module / "unused-mock-ca"),
                "project": "maas-bird-network-lab-tf",
                "management_cidr": "10.203.241.0/24",
                "roles_path": str(ROOT / "maas/network-model/logical-roles.json"),
            }
        )
    )
    tests = module / "tests"
    tests.mkdir()
    (tests / "boot_defaults.tftest.hcl").write_text(
        """mock_provider "maas" {
  override_during = plan
  mock_data "maas_boot_source" { defaults = { id = "1" } }
  mock_data "maas_subnet" { defaults = { id = "1", fabric = "0", vid = 0 } }
  mock_data "maas_fabric" { defaults = { id = "0" } }
  mock_data "maas_vlan" { defaults = { id = "5001" } }
}
override_resource {
  target = maas_subnet.management
  override_during = plan
  values = { id = "1" }
}
run "cold_boot_defaults" {
  command = plan
  assert {
    condition = {
      for key, configuration in maas_configuration.image_defaults : key => configuration.value
    } == {
      default_osystem = "ubuntu"
      default_distro_series = "noble"
      commissioning_distro_series = "noble"
    }
    error_message = "Image-dependent defaults must retain their exact values."
  }
  assert {
    condition = toset(keys(maas_configuration.defaults)) == toset([
      "enable_analytics", "release_notifications"
    ]) && maas_configuration.minimum_kernel.value == "hwe-24.04"
    error_message = "Only image-independent settings may precede image import."
  }
}
"""
    )
    command(module, "init", "-backend=false", "-input=false", "-no-color")
    command(module, "validate", "-no-color")
    command(module, "test", "-var-file=fixture.tfvars.json", "-no-color")
    graph = subprocess.check_output(
        [str(TERRAFORM), f"-chdir={module}", "graph", "-type=plan"],
        text=True,
        timeout=30,
    )
    assert (
        '"[root] maas_configuration.image_defaults (expand)" -> '
        '"[root] maas_boot_source_selection.noble (expand)"'
    ) in graph
    assert (
        '"[root] maas_boot_source_selection.noble (expand)" -> '
        '"[root] maas_configuration.defaults (expand)"'
    ) in graph
    assert (
        '"[root] maas_configuration.minimum_kernel (expand)" -> '
        '"[root] maas_boot_source_selection.noble (expand)"'
    ) in graph
