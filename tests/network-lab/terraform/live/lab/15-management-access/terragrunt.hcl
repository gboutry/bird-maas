locals { repository = get_repo_root() }
include "root" { path = find_in_parent_folders("root.hcl") }
terraform { source = "${local.repository}/tests/network-lab/terraform/modules/management-access" }
dependency "networks" { config_path = "../00-networks" }
inputs = {
  project            = dependency.networks.outputs.project
  management_network = dependency.networks.outputs.management_network
  access_networks    = dependency.networks.outputs.management_access_networks
}
