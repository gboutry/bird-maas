locals { repository = get_repo_root() }
include "root" { path = find_in_parent_folders("root.hcl") }
terraform { source = "${local.repository}/tests/network-lab/terraform/modules/maas-config" }
dependency "networks" { config_path = "../00-networks" }
dependency "maas" { config_path = "../20-maas" }
inputs = {
  api_url = dependency.maas.outputs.api_url
  api_key_path = dependency.maas.outputs.api_key_path
  ca_certificate_path = dependency.maas.outputs.ca_certificate_path
  project = dependency.networks.outputs.project
  management_cidr = dependency.networks.outputs.management_cidr
  roles_path = "${local.repository}/tests/network-lab/maas/network-model/logical-roles.json"
}
