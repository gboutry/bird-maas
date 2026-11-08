locals { repository = get_repo_root() }
include "root" { path = find_in_parent_folders("root.hcl") }
terraform { source = "${local.repository}/tests/network-lab/terraform/modules/juju-secrets" }
dependency "controller" { config_path = "../45-juju-controller" }
dependency "model" { config_path = "../50-juju-machines" }
dependency "networking" { config_path = "../40-networking" }
dependency "maas" { config_path = "../20-maas" }
inputs = {
  controller_addresses = dependency.controller.outputs.api_addresses
  controller_ca        = dependency.controller.outputs.ca_certificate
  controller_username  = dependency.controller.outputs.username
  controller_password  = dependency.controller.outputs.password
  model_uuid           = dependency.model.outputs.model_uuid
  workers              = dependency.networking.outputs.workers
  maas_api_key_path    = dependency.maas.outputs.api_key_path
  maas_ca_path         = dependency.maas.outputs.ca_certificate_path
  roles_path           = "${local.repository}/tests/network-lab/maas/network-model/logical-roles.json"
}
