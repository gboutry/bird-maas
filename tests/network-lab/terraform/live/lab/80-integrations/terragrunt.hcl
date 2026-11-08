locals { repository = get_repo_root() }
include "root" { path = find_in_parent_folders("root.hcl") }
terraform { source = "${local.repository}/tests/network-lab/terraform/modules/juju-integrations" }
dependency "controller" { config_path = "../45-juju-controller" }
dependency "model" { config_path = "../50-juju-machines" }
dependency "applications" { config_path = "../70-applications" }
dependency "secrets" { config_path = "../60-secrets" }
inputs = {
  controller_addresses  = dependency.controller.outputs.api_addresses
  controller_ca         = dependency.controller.outputs.ca_certificate
  controller_username   = dependency.controller.outputs.username
  controller_password   = dependency.controller.outputs.password
  model_uuid            = dependency.model.outputs.model_uuid
  worker_applications   = dependency.applications.outputs.worker_applications
  secret_ids            = dependency.secrets.outputs.secret_ids
  wait_for_ready        = dependency.applications.outputs.enabled
  model                 = "nlab-tf:network-lab"
  client_directory      = dependency.controller.outputs.client_directory
  deployment_generation = try(dependency.applications.outputs.deployment_generation, "")
  worker_generations = { for name, machine in dependency.model.outputs.machines :
    name => "${dependency.model.outputs.model_uuid}/${machine}"
  }
}
