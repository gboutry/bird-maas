variable "lldp_forwarding_authorized" {
  type        = bool
  default     = false
  description = "Enable only after the exact host sudo policy has been reviewed and installed."
}

variable "ovs_vsctl_path" {
  type        = string
  default     = "/snap/bin/microovn.ovs-vsctl"
  description = "Reviewed host OVS client; the host sudo policy must allow this exact executable."
  validation {
    condition     = contains(["/snap/bin/microovn.ovs-vsctl", "/usr/bin/ovs-vsctl"], var.ovs_vsctl_path)
    error_message = "Select the MicroOVN snap or native distribution OVS client."
  }
}

resource "terraform_data" "lldp" {
  count            = var.lldp_forwarding_authorized ? 1 : 0
  triggers_replace = [var.ovs_vsctl_path]
  lifecycle {
    replace_triggered_by = [lxd_network.isolated]
    precondition {
      condition     = var.prefix == "nlab-tf"
      error_message = "The host permission is restricted to the nlab-tf carrier names."
    }
  }
  provisioner "local-exec" {
    command     = "sudo -n \"$OVS_VSCTL\" --timeout=5 -- set Bridge nlab-tf-r1-pf0 other_config:forward-bpdu=true -- set Bridge nlab-tf-r1-pf1 other_config:forward-bpdu=true -- set Bridge nlab-tf-r2-pf0 other_config:forward-bpdu=true -- set Bridge nlab-tf-r2-pf1 other_config:forward-bpdu=true"
    environment = { OVS_VSCTL = var.ovs_vsctl_path }
  }
}
