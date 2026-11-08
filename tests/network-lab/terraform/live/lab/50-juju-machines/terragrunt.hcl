locals { repository = get_repo_root() }
include "root" { path = find_in_parent_folders("root.hcl") }
terraform {
  source = "${local.repository}/tests/network-lab/terraform/modules/juju-machines"
  extra_arguments "cascade_safe_destroy" {
    commands  = ["destroy"]
    arguments = ["-refresh=false"]
  }
}
dependency "controller" { config_path = "../45-juju-controller" }
dependency "networking" { config_path = "../40-networking" }
dependency "maas" { config_path = "../20-maas" }
dependency "networks" { config_path = "../00-networks" }
inputs = {
  controller_addresses      = dependency.controller.outputs.api_addresses
  controller_ca             = dependency.controller.outputs.ca_certificate
  controller_username       = dependency.controller.outputs.username
  controller_password       = dependency.controller.outputs.password
  maas_ca_path              = dependency.maas.outputs.ca_certificate_path
  maas_api_key_path         = dependency.maas.outputs.api_key_path
  workers                   = dependency.networking.outputs.workers
  project                   = dependency.networks.outputs.project
  management_cidr           = dependency.networks.outputs.management_cidr
  management_arp_monitoring = true
  roles_path                = "${local.repository}/tests/network-lab/maas/network-model/logical-roles.json"
}
