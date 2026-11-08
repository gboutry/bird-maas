"""Plan actual modules with mocked providers; no guest provisioner executes."""

import json
import shutil
import subprocess

import pytest
import yaml
from test_artifacts import ROOT, TERRAFORM, command


@pytest.mark.parametrize("module_name", ("ovn", "juju-machines"))
def test_same_uuid_deployment_generation_reaches_guest_lifecycles(tmp_path, module_name):
    module = tmp_path / module_name
    shutil.copytree(
        ROOT / "terraform/modules" / module_name,
        module,
        ignore=shutil.ignore_patterns(".terraform", ".terraform.lock.hcl"),
    )
    workers = {
        name: {
            "system_id": name,
            "system_uuid": "instance-uuid",
            "rack": 1,
            "management_ip": f"10.203.241.{40 + 10 * index}",
            "interfaces": {
                "mgmt0": {"name": "enp5s0", "mac": f"02:00:00:00:0{index}:04"},
                "mgmt1": {"name": "enp6s0", "mac": f"02:00:00:00:0{index}:05"},
                **{
                    f"pf{pf}": {"name": f"enp{7 + pf}s0", "mac": f"02:00:00:00:0{index}:0{pf}"}
                    for pf in (0, 1)
                },
            },
            "vlans": {
                f"gp{pf}": {
                    "name": f"enp{7 + pf}s0.3404",
                    "role": "general",
                    "nic": f"pf{pf}",
                    "vid": 3404,
                }
                for pf in (0, 1)
            },
        }
        for index, name in enumerate(("probe", "probe-alt"))
    }
    variables = {"project": "maas-bird-network-lab-tf", "workers": workers}
    if module_name == "ovn":
        variables.update(
            central_address="10.203.241.10",
            enable_compute=True,
            use_dpu_support_ppa=True,
            gateways={
                f"r{rack}": {
                    "name": f"gateway-r{rack}",
                    "system_uuid": "instance-uuid",
                    "address": f"10.205.251.{rack}",
                }
                for rack in (1, 2)
            },
        )
    else:
        ca, key = module / "mock-ca", module / "mock-api-key"
        ca.write_text("mock-ca")
        key.write_text("mock-api-key")
        variables.update(
            controller_addresses=["127.0.0.1:1"],
            controller_ca="mock-ca",
            controller_username="mock-user",
            controller_password="mock-password",
            maas_ca_path=str(ca),
            maas_api_key_path=str(key),
            roles_path=str(ROOT / "maas/network-model/logical-roles.json"),
        )
    (module / "fixture.tfvars.json").write_text(json.dumps(variables))
    command(module, "init", "-backend=false", "-input=false", "-no-color")
    command(module, "validate", "-no-color")
    command(module, "test", "-var-file=fixture.tfvars.json", "-no-color")
    if module_name == "juju-machines":
        graph = subprocess.check_output(
            [str(TERRAFORM), f"-chdir={module}", "graph", "-type=plan"],
            text=True,
            timeout=30,
        )
        assert (
            '"[root] output.worker_configuration (expand)" -> '
            '"[root] terraform_data.ready (expand)"'
        ) in graph
        rendered = subprocess.run(
            [
                str(TERRAFORM),
                f"-chdir={module}",
                "console",
                "-var-file=fixture.tfvars.json",
                "-var=management_arp_monitoring=true",
            ],
            input='jsonencode(local.netplan["probe-alt"])\n',
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        supplement = json.loads(json.loads(rendered.stdout))
        parser_root = tmp_path / "netplan-root"
        configs = parser_root / "etc/netplan"
        configs.mkdir(parents=True)
        inherited = {
            "network": {
                "version": 2,
                "ethernets": {"mgmt0": {}, "mgmt1": {}, "enp7s0": {}, "enp8s0": {}},
                "bonds": {
                    "bond-mgmt": {
                        "interfaces": ["mgmt0", "mgmt1"],
                        "macaddress": "02:00:00:00:00:01",
                        "addresses": ["10.203.241.50/24"],
                        "parameters": {"mode": "active-backup", "mii-monitor-interval": 100},
                    }
                },
            }
        }
        for filename, document in (
            ("50-cloud-init.yaml", inherited),
            ("99-network-lab-roles.yaml", supplement),
        ):
            target = configs / filename
            target.write_text(yaml.safe_dump(document))
            target.chmod(0o600)
        subprocess.run(
            ["netplan", "generate", "--root-dir", str(parser_root)],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        generated = (parser_root / "run/systemd/network/10-netplan-bond-mgmt.netdev").read_text()
        for expected in (
            "MACAddress=02:00:00:00:00:01",
            "MIIMonitorSec=0ms",
            "ARPIntervalSec=200ms",
            "ARPIPTargets=10.203.241.1",
            "ARPValidate=active",
            "FailOverMACPolicy=none",
        ):
            assert expected in generated
        merged = yaml.safe_load(
            subprocess.check_output(
                ["netplan", "get", "--root-dir", str(parser_root)], text=True, timeout=30
            )
        )["network"]["bonds"]["bond-mgmt"]
        assert sorted(merged["interfaces"]) == ["mgmt0", "mgmt1"]
        assert merged["addresses"] == ["10.203.241.50/24"]
        assert merged["macaddress"] == "02:00:00:00:00:01"


def test_worker_destroy_refresh_flag_is_command_scoped(tmp_path):
    source = (ROOT / "terraform/live/lab/50-juju-machines/terragrunt.hcl").read_text()
    terraform_block = (
        "terraform {" + source.split("terraform {", 1)[1].split('dependency "controller"', 1)[0]
    )
    (tmp_path / "terragrunt.hcl").write_text(
        f"locals {{ repository = {json.dumps(str(ROOT.parents[1]))} }}\n" + terraform_block
    )
    result = subprocess.run(
        ["terragrunt", "render", "--json"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    rendered = json.loads(result.stdout)
    assert rendered["terraform"]["extra_arguments"]["cascade_safe_destroy"]["commands"] == [
        "destroy"
    ]
    assert rendered["terraform"]["extra_arguments"]["cascade_safe_destroy"]["arguments"] == [
        "-refresh=false"
    ]
