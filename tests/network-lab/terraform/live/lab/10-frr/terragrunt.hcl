locals {
  repository = get_repo_root()
}
include "root" {
  path = find_in_parent_folders("root.hcl")
}
terraform {
  source = "${local.repository}/tests/network-lab/terraform/modules/frr"
}
dependency "networks" {
  config_path = "../00-networks"
}
inputs = {
  cloud_init_timeout_seconds = 1200
  core_mtu                   = dependency.networks.outputs.core_mtu
  project                    = dependency.networks.outputs.project
  management_network         = dependency.networks.outputs.management_network
  management_cidr            = dependency.networks.outputs.management_cidr
  core_networks              = dependency.networks.outputs.core_networks
  fast_networks              = dependency.networks.outputs.fast_networks
}
