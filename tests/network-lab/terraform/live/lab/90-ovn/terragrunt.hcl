locals { repository = get_repo_root() }
include "root" { path = find_in_parent_folders("root.hcl") }
terraform { source = "${local.repository}/tests/network-lab/terraform/modules/ovn" }
dependency "networks" { config_path = "../00-networks" }
dependency "maas" { config_path = "../20-maas" }
dependency "networking" { config_path = "../40-networking" }
dependency "machines" { config_path = "../50-juju-machines" }
dependency "gateways" { config_path = "../85-gateways" }
dependency "applications" { config_path = "../70-applications" }
dependencies { paths = ["../80-integrations", "../65-frr-routing"] }
inputs = {
  central_cpu_cores = dependency.maas.outputs.cpu_cores
  use_dpu_support_ppa = true
  project             = dependency.networks.outputs.project
  workers             = dependency.networking.outputs.workers
  worker_generations = { for name, machine in dependency.machines.outputs.machines :
    name => "${dependency.machines.outputs.model_uuid}/${machine}"
  }
  gateways        = dependency.gateways.outputs.gateways
  central_address = cidrhost(dependency.networks.outputs.management_cidr, 10)
  enable_compute  = dependency.applications.outputs.enabled
}
