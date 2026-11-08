mock_provider "lxd" {
  override_during = plan
  mock_data "lxd_instance" {
    defaults = { name = "probe", config = { "volatile.uuid" = "instance-uuid", "user.maas-bird-owner" = "network-lab" } }
  }
}
run "configuration_uses_generation_and_canonical_contents" {
  command = plan
  assert {
    condition     = lxd_instance_file.networkd["probe"].content == var.workers["probe"].networkd && lxd_instance_file.lldp["probe"].content == var.workers["probe"].lldp && terraform_data.networkd["probe"].triggers_replace[2] == "deployment-one" && terraform_data.lldp["probe"].triggers_replace[2] == "deployment-one"
    error_message = "Both reconcilers must consume the canonical content and deployment generation."
  }
}
run "same_uuid_redeployment_changes_only_selected_generation" {
  command = plan
  variables {
    workers = {
      probe = { project = "lab", uuid = "instance-uuid", generation = "deployment-two", networkd = "networkd-content", lldp = "lldp-content" }
      probe-alt = { project = "lab", uuid = "instance-uuid", generation = "deployment-one", networkd = "networkd-content", lldp = "lldp-content" }
    }
  }
  assert {
    condition     = terraform_data.networkd["probe"].triggers_replace == ["instance-uuid", sha256("networkd-content"), "deployment-two"] && terraform_data.lldp["probe"].triggers_replace == ["instance-uuid", sha256("lldp-content"), "deployment-two"] && terraform_data.networkd["probe-alt"].triggers_replace[2] == "deployment-one" && terraform_data.lldp["probe-alt"].triggers_replace[2] == "deployment-one"
    error_message = "The selected reinstall must reach both reconcilers without changing another worker's generation."
  }
}
run "omitted_native_configuration_manages_no_workers" {
  command = plan
  variables { workers = {} }
  assert {
    condition     = length(lxd_instance_file.networkd) == 0 && length(lxd_instance_file.lldp) == 0 && length(terraform_data.networkd) == 0 && length(terraform_data.lldp) == 0
    error_message = "Empty configuration must manage no guest files or restart lifecycles."
  }
}
