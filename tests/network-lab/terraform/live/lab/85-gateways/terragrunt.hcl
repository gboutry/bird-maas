locals { repository = get_repo_root() }
include "root" { path = find_in_parent_folders("root.hcl") }
terraform { source = "${local.repository}/tests/network-lab/terraform/modules/gateways" }
dependency "networks" { config_path = "../00-networks" }
dependencies { paths = ["../25-maas-config"] }
dependency "secrets" { config_path = "../60-secrets" }
inputs = {
  project            = dependency.networks.outputs.project
  management_network = dependency.networks.outputs.management_network
  management_cidr    = dependency.networks.outputs.management_cidr
  fast_networks      = dependency.networks.outputs.fast_networks
  passwords          = dependency.secrets.outputs.gateway_passwords
}
