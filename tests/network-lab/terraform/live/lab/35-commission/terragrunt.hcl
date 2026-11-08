locals { repository = get_repo_root() }
include "root" { path = find_in_parent_folders("root.hcl") }
terraform { source = "${local.repository}/tests/network-lab/terraform/modules/maas-machines" }
dependency "maas" { config_path = "../20-maas" }
dependency "power" { config_path = "../26-lxd-power" }
dependency "machines" { config_path = "../30-machines" }
dependencies { paths = ["../25-maas-config"] }
inputs = {
  api_url = dependency.maas.outputs.api_url
  api_key_path = dependency.maas.outputs.api_key_path
  ca_certificate_path = dependency.maas.outputs.ca_certificate_path
  machines = dependency.machines.outputs.machines
  power_parameters = dependency.power.outputs.power_parameters
}
