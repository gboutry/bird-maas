"""Plan native access infrastructure and its full resource budget with mocked LXD."""

import json
import re
import shutil
import subprocess

import yaml
from test_artifacts import ROOT, TERRAFORM, command


def plan(module, inputs, tmp_path, expected_failure=False):
    directory = tmp_path / module
    shutil.copytree(
        ROOT / "terraform/modules" / module,
        directory,
        ignore=shutil.ignore_patterns(".terraform", ".terraform.lock.hcl"),
    )
    tests = directory / "tests"
    shutil.rmtree(tests, ignore_errors=True)
    tests.mkdir()
    (tests / "resources.tftest.hcl").write_text(
        'mock_provider "lxd" { override_during = plan }\n'
        + (
            'mock_provider "tls" { override_during = plan }\n'
            if module == "maas-controller"
            else ""
        )
        + (
            'run "resources" {\n  command = plan\n  expect_failures = [lxd_instance.access]\n}\n'
            if expected_failure
            else 'run "resources" { command = plan }\n'
        )
    )
    (directory / "inputs.tfvars.json").write_text(json.dumps(inputs))
    command(directory, "init", "-backend=false", "-input=false", "-no-color")
    command(directory, "validate", "-no-color")
    result = subprocess.run(
        [
            str(TERRAFORM),
            f"-chdir={directory}",
            "test",
            "-json",
            "-verbose",
            "-var-file=inputs.tfvars.json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    plans = [
        event["test_plan"]
        for line in result.stdout.splitlines()
        if (event := json.loads(line)).get("type") == "test_plan"
    ]
    if expected_failure:
        assert all(
            item["type"] != "lxd_instance"
            for partial in plans
            for item in partial.get("resource_changes", [])
        )
        return directory, []
    assert len(plans) == 1
    return directory, [item for item in plans[0]["resource_changes"] if item["mode"] == "managed"]


def resources(changes, kind):
    return [item["change"]["after"] for item in changes if item["type"] == kind]


def test_access_bridges_and_worker_attachments_preserve_management_separation(tmp_path):
    access = {"a": "lab-ma", "b": "lab-mb"}
    common = {"project": "lab", "management_network": "lab-mgmt"}
    directory, changes = plan("management-access", common | {"access_networks": access}, tmp_path)
    switches = resources(changes, "lxd_instance")
    assert {item["name"] for item in switches} == {"access-a", "access-b"}
    for item in switches:
        side = item["name"][-1]
        assert item["type"] == "container" and item["profiles"] == []
        assert item["config"]["limits.cpu"] == "1" and item["config"]["limits.memory"] == "768MiB"
        devices = {device["name"]: device for device in item["device"]}
        assert devices["uplink"]["properties"]["network"] == "lab-mgmt"
        assert devices["downlink"]["properties"]["network"] == access[side]
        network = yaml.safe_load(item["config"]["cloud-init.network-config"])
        assert network["bridges"]["br-access"]["interfaces"] == ["uplink", "downlink"]
        for interface in (*network["ethernets"].values(), network["bridges"]["br-access"]):
            assert not interface["dhcp4"] and not interface["dhcp6"]
            assert not interface["accept-ra"] and interface["link-local"] == []
            assert "addresses" not in interface and "routes" not in interface
        userdata = yaml.safe_load(item["config"]["cloud-init.user-data"])
        assert userdata == {"package_update": False, "package_upgrade": False}
    graph = subprocess.check_output(
        [str(TERRAFORM), f"-chdir={directory}", "graph", "-type=plan"], text=True, timeout=30
    )
    assert (
        '"[root] output.management_access_networks (expand)" -> '
        '"[root] terraform_data.ready (expand)"' in graph
    )
    assert (
        '"[root] terraform_data.ready (expand)" -> "[root] lxd_instance.access (expand)"' in graph
    )
    _, changes = plan(
        "lxd-machines",
        common
        | {
            "management_access_networks": access,
            "fast_networks": {
                f"r{rack}-pf{pf}": f"r{rack}-pf{pf}" for rack in (1, 2) for pf in (0, 1)
            },
            "juju_cores": 1,
        },
        tmp_path,
    )
    for item in resources(changes, "lxd_instance"):
        devices = {device["name"]: device for device in item["device"]}
        assert item["config"]["limits.cpu"] == "1"
        if item["name"] == "juju":
            assert (
                devices["mgmt0"]["properties"]["network"]
                == devices["mgmt1"]["properties"]["network"]
                == "lab-mgmt"
            )
        else:
            assert devices["mgmt0"]["properties"]["network"] == access["a"]
            assert devices["mgmt1"]["properties"]["network"] == access["b"]
            rack = 2 if item["name"].startswith("probe-r2") else 1
            for pf in (0, 1):
                assert devices[f"pf{pf}"]["properties"]["network"] == f"r{rack}-pf{pf}"
    native = (ROOT / "terraform/live/lab/30-machines/terragrunt.hcl").read_text()
    assert 'config_path = "../15-management-access"' in native
    assert "dependency.management_access.outputs.management_access_networks" in native
    assert re.search(r"juju_cores\s*=\s*1", native)


def test_native_access_profile_fits_cpu_and_memory_budget(tmp_path):
    fast = {f"r{rack}-pf{pf}": f"r{rack}-pf{pf}" for rack in (1, 2) for pf in (0, 1)}
    core = {
        f"r{rack}{side}-s{spine}": f"r{rack}{side}-s{spine}"
        for rack in (1, 2)
        for side in ("a", "b")
        for spine in (0, 1)
    }
    access = {"a": "lab-ma", "b": "lab-mb"}
    common = {
        "project": "lab",
        "management_network": "lab-mgmt",
        "management_cidr": "10.203.241.0/24",
    }
    configurations = {
        "management-access": {
            "project": "lab",
            "management_network": "lab-mgmt",
            "access_networks": access,
        },
        "lxd-machines": {
            "project": "lab",
            "management_network": "lab-mgmt",
            "management_access_networks": access,
            "fast_networks": fast,
            "juju_cores": 1,
        },
        "maas-controller": common
        | {
            "cpu_cores": 1,
            "admin_password": "mock-test-password-only-0123456789abcdef",
            "credential_directory": str(tmp_path / "credentials"),
        },
        "frr": common | {"fast_networks": fast, "core_networks": core},
        "gateways": common | {"fast_networks": fast, "passwords": {"r1": "mock", "r2": "mock"}},
    }
    instances = []
    for module, inputs in configurations.items():
        _, changes = plan(module, inputs, tmp_path)
        instances.extend(resources(changes, "lxd_instance"))
    assert len(instances) == 16
    assert sum(item["type"] == "container" for item in instances) == 8
    assert sum(int(item["config"]["limits.cpu"]) for item in instances) == 16
    sizes = {"768MiB": 768, "2GiB": 2048, "4GiB": 4096, "8GiB": 8192}
    assert sum(sizes[item["config"]["limits.memory"]] for item in instances) == 38 * 1024
    _, changes = plan("lxd-networks", {"enable_management_access": True}, tmp_path)
    networks = resources(changes, "lxd_network")
    assert len(networks) == 15
    names = [item["name"] for item in networks]
    assert len(set(names)) == 15
    assert all(
        not name.startswith(other + "-") for name in names for other in names if name != other
    )
    selected = [item for item in networks if item["name"].endswith(("-ma", "-mb"))]
    assert len(selected) == 2
    assert all(
        item["config"]["bridge.driver"] == "openvswitch"
        and item["config"]["ipv4.address"] == "none"
        and item["config"]["ipv6.address"] == "none"
        for item in selected
    )
    for stage in ("20-maas", "45-juju-controller"):
        native = (ROOT / "terraform/live/lab" / stage / "terragrunt.hcl").read_text()
        assert re.search(r"cpu_cores\s*=\s*1", native)
    native = (ROOT / "terraform/live/lab/00-networks/terragrunt.hcl").read_text()
    assert re.search(r"enable_management_access\s*=\s*true", native)
    project = resources(changes, "lxd_project")[0]
    assert (
        project["config"]["limits.cpu"] == "16" and project["config"]["limits.memory"] == "56GiB"
    )


def test_generic_inputs_keep_shared_management_and_original_juju_budget(tmp_path):
    _, changes = plan(
        "lxd-machines",
        {
            "project": "lab",
            "management_network": "lab-mgmt",
            "fast_networks": {
                f"r{rack}-pf{pf}": f"r{rack}-pf{pf}" for rack in (1, 2) for pf in (0, 1)
            },
        },
        tmp_path,
    )
    for item in resources(changes, "lxd_instance"):
        devices = {device["name"]: device for device in item["device"]}
        assert devices["mgmt0"]["properties"]["network"] == "lab-mgmt"
        assert devices["mgmt1"]["properties"]["network"] == "lab-mgmt"
        assert item["config"]["limits.cpu"] == ("2" if item["name"] == "juju" else "1")
    _, changes = plan("lxd-networks", {}, tmp_path)
    assert len(resources(changes, "lxd_network")) == 13
    source = (ROOT / "terraform/modules/management-access/main.tf").read_text()
    script = re.search(r"command = <<-SCRIPT\n(.*?)\n    SCRIPT", source, re.DOTALL)
    assert script is not None
    subprocess.run(["sh", "-n"], input=script.group(1), text=True, check=True, timeout=10)


def test_access_network_namespace_overlap_is_rejected(tmp_path):
    for index, (upstream, access) in enumerate(
        (
            ("lab-mgmt", {"a": "lab-mgmt-a", "b": "lab-mb"}),
            ("lab-mgmt", {"a": "lab-ma", "b": "lab-ma-child"}),
            ("lab-ma-child", {"a": "lab-ma", "b": "lab-mb"}),
            ("lab-mgmt", {"a": "lab-mgmt", "b": "lab-mb"}),
        )
    ):
        case = tmp_path / str(index)
        case.mkdir()
        plan(
            "management-access",
            {"project": "lab", "management_network": upstream, "access_networks": access},
            case,
            expected_failure=True,
        )
