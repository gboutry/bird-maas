locals {
  repository = get_repo_root()
}

include "root" {
  path = find_in_parent_folders("root.hcl")
}

terraform {
  source = "${local.repository}/tests/network-lab/terraform/modules/lxd-networks"
}

inputs = {
  enable_management_access   = true
  lldp_forwarding_authorized = true
}
