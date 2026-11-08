locals {
  repository = get_repo_root()
  server = jsondecode(run_cmd("--terragrunt-quiet", "lxc", "query", "/1.0")).environment
}
include "root" { path = find_in_parent_folders("root.hcl") }
terraform { source = "${local.repository}/tests/network-lab/terraform/modules/lxd-power" }
dependency "networks" { config_path = "../00-networks" }
dependency "maas" { config_path = "../20-maas" }
inputs = {
  maas_cpu_cores = dependency.maas.outputs.cpu_cores
  project = dependency.networks.outputs.project
  management_cidr = dependency.networks.outputs.management_cidr
  lxd_server_certificate = local.server.certificate
  lxd_server_name = local.server.server_name
}
