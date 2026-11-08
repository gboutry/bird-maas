locals {
  repository = get_repo_root()
  lab        = "${local.repository}/tests/network-lab"
  output     = "${local.lab}/runtime/terraform-bundle-probe"
}

include "root" {
  path = find_in_parent_folders("root.hcl")
}

terraform {
  source = "${local.lab}/terraform/modules//juju-local-applications"

}

inputs = {
  model            = "network-lab:network-lab"
  model_uuid       = get_env("NETWORK_LAB_PROBE_MODEL_UUID", "59d52f22-9218-4b70-873d-f7066ce5e8b7")
  output_directory = local.output
  charms = {
    maas-inventory = get_env("NETWORK_LAB_PROBE_CHARM", "${local.lab}/runtime/charms/maas-inventory_amd64.charm")
  }
  applications = {
    tf-local-probe-inventory = {
      charm   = "maas-inventory"
      machine = "0"
      options = { inventory-cache-seconds = "32" }
    }
  }
}
