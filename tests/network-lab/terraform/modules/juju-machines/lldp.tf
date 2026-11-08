locals {
  lldp_defaults = { for name, worker in var.workers : name => "DAEMON_ARGS=\"-r -I ${worker.interfaces.pf0.name},${worker.interfaces.pf1.name} -k\"\n" }
}
removed {
  from = lxd_instance_file.lldp
  lifecycle { destroy = false }
}
removed {
  from = terraform_data.lldp
  lifecycle { destroy = false }
}
