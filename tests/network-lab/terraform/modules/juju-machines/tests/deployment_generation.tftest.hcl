mock_provider "juju" {
  override_during = plan
  mock_resource "juju_machine" {
    defaults = { id = "deployment-one", machine_id = "7", hostname = "worker" }
  }
}

run "post_deployment_lifecycles_include_machine_identity" {
  command = plan
  assert {
    condition     = one([for item in yamldecode(local.userdata).write_files : item.content if item.path == "/etc/systemd/networkd.conf.d/90-network-lab.conf"]) == output.worker_configuration["probe-alt"].networkd && one([for item in yamldecode(local.userdata).write_files : item.content if item.path == "/etc/network-lab/lldp/probe-alt.default"]) == output.worker_configuration["probe-alt"].lldp
    error_message = "Bootstrap and day-two reconciliation must consume identical canonical contents."
  }
  assert {
    condition     = output.worker_configuration["probe-alt"].networkd == local.networkd_owner && output.worker_configuration["probe-alt"].lldp == local.lldp_defaults["probe-alt"] && output.worker_configuration["probe-alt"].uuid == var.workers["probe-alt"].system_uuid && output.worker_configuration["probe-alt"].project == var.project
    error_message = "Worker reconciliation must use the canonical bootstrap contents and exact identity."
  }
  assert {
    condition     = terraform_data.ready["probe-alt"].triggers_replace == "deployment-one" && output.worker_configuration["probe-alt"].generation == "deployment-one"
    error_message = "Readiness and exported worker configuration must use the actual Juju deployment identity."
  }
}

run "same_uuid_redeployment_updates_post_boot_identity" {
  command = plan
  override_resource {
    target          = juju_machine.worker["probe-alt"]
    override_during = plan
    values          = { id = "deployment-two", machine_id = "8", hostname = "worker" }
  }
  assert {
    condition     = terraform_data.ready["probe-alt"].triggers_replace == "deployment-two" && output.worker_configuration["probe-alt"].generation == "deployment-two"
    error_message = "Same-UUID redeployment must update the selected exported deployment identity."
  }
  assert {
    condition     = terraform_data.ready["probe"].triggers_replace == "deployment-one" && output.worker_configuration["probe"].generation == "deployment-one"
    error_message = "An unrelated worker must retain its lifecycle identity."
  }
}

run "management_bond_uses_upstream_arp_without_redeclaring_identity" {
  command = plan
  variables {
    management_cidr = "10.203.242.0/24"
    management_arp_monitoring = true
  }
  assert {
    condition     = local.netplan["probe-alt"].network.bonds.bond-mgmt.parameters == { mode = "active-backup", mii-monitor-interval = 0, arp-interval = "200ms", arp-ip-targets = ["10.203.242.1"], arp-validate = "active", fail-over-mac-policy = "none" } && keys(local.netplan["probe-alt"].network.bonds.bond-mgmt) == ["parameters"]
    error_message = "The MAAS bond keeps its identity while disabling inherited MII and validating upstream ARP replies."
  }
}

run "arp_runtime_reconciliation_follows_native_netplan_apply" {
  command = plan
  variables { management_arp_monitoring = true }
  assert {
    condition     = strcontains(local.select_profile, "netplan apply\ncase \"$profile\" in") && strcontains(local.select_profile, "ip link set dev bond-mgmt type bond miimon 0 arp_interval 200") && strcontains(local.select_profile, "expected_management_mac=02:00:00:00:01:04")
    error_message = "Enabled ARP monitoring must reconcile and verify the native existing bond after Netplan apply."
  }
}

run "generic_default_does_not_override_maas_bond_policy" {
  command = plan
  assert {
    condition     = var.management_arp_monitoring == false && !contains(keys(local.netplan["probe-alt"].network), "bonds") && !strcontains(local.select_profile, "arp_interval")
    error_message = "Generic callers must retain their MAAS bond policy unless ARP monitoring is explicitly enabled."
  }
}
