locals { repository = get_repo_root() }
include "root" { path = find_in_parent_folders("root.hcl") }
terraform { source = "${local.repository}/tests/network-lab/terraform/modules/frr-routing" }
dependency "networks" { config_path = "../00-networks" }
dependency "frr" { config_path = "../10-frr" }
dependency "networking" { config_path = "../40-networking" }
dependency "gateways" { config_path = "../85-gateways" }
dependency "secrets" { config_path = "../60-secrets" }
inputs = {
  core_mtu          = dependency.networks.outputs.core_mtu
  project           = dependency.networks.outputs.project
  switch_uuids      = dependency.frr.outputs.switch_uuids
  workers           = dependency.networking.outputs.workers
  gateways          = dependency.gateways.outputs.gateways
  gateway_passwords = dependency.secrets.outputs.gateway_passwords
  bgp_passwords     = dependency.secrets.outputs.bgp_passwords
  roles_path        = "${local.repository}/tests/network-lab/maas/network-model/logical-roles.json"
}
