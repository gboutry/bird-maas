"""Read immutable instance identities without adding them to managed config."""

import json
import shutil
import subprocess

from test_artifacts import ROOT, TERRAFORM, command


def test_five_machine_metadata_reads_preserve_existing_vm_intent(tmp_path):
    module = tmp_path / "machines"
    shutil.copytree(
        ROOT / "terraform/modules/lxd-machines",
        module,
        ignore=shutil.ignore_patterns(".terraform", ".terraform.lock.hcl"),
    )
    workers = ("probe", "probe-alt", "probe-r2a", "probe-r2b")
    names = (*workers, "juju")
    identities = {
        name: f"00000000-0000-4000-8000-{index:012d}" for index, name in enumerate(names, 1)
    }
    (module / "inputs.tfvars.json").write_text(
        json.dumps(
            {
                "project": "owned-project",
                "management_network": "management",
                "management_access_networks": {"a": "access-a", "b": "access-b"},
                "fast_networks": {
                    f"r{rack}-pf{pf}": f"rack{rack}-pf{pf}" for rack in (1, 2) for pf in (0, 1)
                },
            }
        )
    )
    tests = module / "tests"
    tests.mkdir(exist_ok=True)
    overrides = "\n".join(
        f'''override_data {{
 override_during = plan
 target = data.lxd_instance.machine["{name}"]
 values = {{ config = {{ "volatile.uuid" = "{identity}" }} }}
}}'''
        for name, identity in identities.items()
    )
    (tests / "identities.tftest.hcl").write_text(
        'mock_provider "lxd" { override_during = plan }\n'
        + overrides
        + """
run "seed_mock_instances" {
 command = apply
}
run "default_cpu_and_immutable_metadata" {
 command = plan
 assert {
  condition = (
   length(lxd_instance.machine) == 5 && length(data.lxd_instance.machine) == 5 &&
   sum([for vm in lxd_instance.machine : tonumber(vm.config["limits.cpu"])]) == 6
  )
  error_message = "The five VMs must retain generic CPU allocations."
 }
 assert {
  condition = alltrue([for name, vm in lxd_instance.machine :
   data.lxd_instance.machine[name].name == vm.name &&
   data.lxd_instance.machine[name].project == vm.project &&
   vm.project == "owned-project" && vm.name == name &&
   !contains(keys(vm.config), "volatile.uuid") &&
   vm.running == false && vm.config["limits.memory"] == "4GiB"
  ])
  error_message = "Identity reads must bind to the managed name/project without managing UUIDs."
 }
"""
        + "\n".join(
            f''' assert {{
  condition = output.machines["{name}"].system_uuid == "{identity}"
  error_message = "{name} must expose its own immutable data-source UUID."
 }}'''
            for name, identity in identities.items()
        )
        + "\n"
        + "\n".join(
            f''' assert {{
  condition = (
   output.machines["{name}"].name == "{name}" &&
   output.machines["{name}"].pxe_mac == "02:00:00:fb:{index + 1:02x}:00" &&
   output.machines["{name}"].macs.mgmt0 == "02:00:00:fb:{index + 1:02x}:00" &&
   output.machines["{name}"].macs.mgmt1 == "02:00:00:fb:{index + 1:02x}:01" &&
   output.machines["{name}"].macs.pf0 == "02:00:00:fb:{index + 1:02x}:02" &&
   output.machines["{name}"].macs.pf1 == "02:00:00:fb:{index + 1:02x}:03" &&
   output.machines["{name}"].rack == {1 + index // 2} &&
   output.machines["{name}"].management_networks == {{mgmt0="access-a",mgmt1="access-b"}}
  )
  error_message = "{name} must retain MAC, rack and independent management metadata."
 }}'''
            for index, name in enumerate(workers)
        )
        + """
 assert {
  condition = (
   output.machines.juju.name == "juju" && output.machines.juju.rack == 0 &&
   output.machines.juju.pxe_mac == "02:00:00:fb:0a:00" &&
   output.machines.juju.macs == {mgmt0="02:00:00:fb:0a:00",mgmt1="02:00:00:fb:0a:01"} &&
   output.machines.juju.management_networks == {mgmt0="management",mgmt1="management"}
  )
  error_message = "Juju must retain its controller management and MAC metadata."
 }
}
run "native_cpu_override" {
 command = plan
 variables {
  juju_cores = 1
  management_access_networks = {}
 }
 assert {
  condition = (
   lxd_instance.machine["juju"].config["limits.cpu"] == "1" &&
   sum([for vm in lxd_instance.machine : tonumber(vm.config["limits.cpu"])]) == 5
  )
  error_message = "Immutable metadata must not alter the native CPU override."
 }
 assert {
  condition = alltrue([for vm in output.machines :
   vm.management_networks == {mgmt0="management",mgmt1="management"}
  ])
  error_message = "Omitted access carriers must retain the generic management network."
 }
}
"""
    )
    command(module, "init", "-backend=false", "-input=false", "-no-color")
    command(module, "validate", "-no-color")
    command(module, "test", "-var-file=inputs.tfvars.json", "-no-color")
    graph = subprocess.run(
        [str(TERRAFORM), f"-chdir={module}", "graph", "-type=plan"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    assert (
        '"[root] data.lxd_instance.machine (expand)" -> "[root] lxd_instance.machine (expand)"'
        in graph
    )
