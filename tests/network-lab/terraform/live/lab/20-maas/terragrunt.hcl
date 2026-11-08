locals {
  repository = get_repo_root()
  private    = "${local.repository}/tests/network-lab/runtime/terraform/maas-credentials"
}
include "root" { path = find_in_parent_folders("root.hcl") }
terraform { source = "${local.repository}/tests/network-lab/terraform/modules/maas-controller" }
dependency "networks" { config_path = "../00-networks" }
inputs = {
  cpu_cores = 1
  project              = dependency.networks.outputs.project
  management_network   = dependency.networks.outputs.management_network
  management_cidr      = dependency.networks.outputs.management_cidr
  credential_directory = local.private
  admin_password       = trimspace(file("${local.private}/admin-password"))
}
