locals { repository = get_repo_root() }
include "root" { path = find_in_parent_folders("root.hcl") }
terraform {
  source = "${local.repository}/tests/network-lab/terraform/modules/juju-controller"
  extra_arguments "verified_maas_ca" {
    commands = ["plan", "apply", "destroy"]
    env_vars = { SSL_CERT_FILE = dependency.maas.outputs.ca_certificate_path }
  }
}
dependency "maas" { config_path = "../20-maas" }
dependency "commission" { config_path = "../35-commission" }
inputs = {
  cpu_cores = 1
  api_url = dependency.maas.outputs.api_url
  api_key_path = dependency.maas.outputs.api_key_path
  ca_certificate_path = dependency.maas.outputs.ca_certificate_path
  controller_machine_id = dependency.commission.outputs.machine_ids.juju
  client_directory = "${local.repository}/tests/network-lab/runtime/terraform/juju-client"
}
