locals {
  repository     = get_repo_root()
  api_extensions = jsondecode(run_cmd("--terragrunt-quiet", "lxc", "query", "/1.0")).api_extensions
}
include "root" { path = find_in_parent_folders("root.hcl") }
terraform { source = "${local.repository}/tests/network-lab/terraform/modules/lxd-machines" }
dependency "networks" { config_path = "../00-networks" }
dependency "management_access" { config_path = "../15-management-access" }
inputs = {
  juju_cores                 = 1
  management_access_networks = dependency.management_access.outputs.management_access_networks
  supports_boot_mode         = contains(local.api_extensions, "instance_boot_mode")
  project                    = dependency.networks.outputs.project
  management_network         = dependency.networks.outputs.management_network
  fast_networks              = dependency.networks.outputs.fast_networks
}
