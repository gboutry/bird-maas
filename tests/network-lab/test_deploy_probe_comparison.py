"""Preserve default recipes while composing the explicit /31 + loopback variant."""

import base64
import copy
import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def deployment(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent / "scripts"))
    return importlib.import_module("deploy_probe")


def machine(deploy, *, numbered=True):
    interfaces = []
    for side, parent in enumerate(("enp7s0", "enp8s0")):
        fabric = side + 1
        interfaces.append({"name": parent, "id": side, "vlan": {"fabric_id": fabric}})
        for role in deploy.roles():
            general = numbered and role["id"] == "general"
            interfaces.append(
                {
                    "name": f"{parent}.{role['vlan']}",
                    "id": 10000 * fabric + role["vlan"],
                    "type": "vlan",
                    "parents": [parent],
                    "vlan": {
                        "id": 10000 * fabric + role["vlan"],
                        "vid": role["vlan"],
                        "fabric_id": fabric,
                    },
                    "links": [{"id": role["vlan"], "mode": "static", "ip_address": "10.205.2.4"}]
                    if general
                    else [{"id": role["vlan"], "mode": "link_up"}],
                }
            )
    return {"status_name": "Ready", "interface_set": interfaces}


def test_defaults_and_composed_userdata_remain_distinct(deployment, tmp_path):
    assert deployment.deployment_user_data("probe-alt", tmp_path, "interface32") == []
    assert deployment.deployment_user_data("probe-alt", tmp_path, "point31") == []
    original = deployment.deployment_user_data("probe-alt", tmp_path, "loopback32")[0]
    original_config = yaml.safe_load(base64.b64decode(original.split("=", 1)[1]))
    original_network = yaml.safe_load(original_config["write_files"][0]["content"])["network"]
    assert original_network["ethernets"]["lo"]["addresses"] == [
        deployment.HOSTS["probe-alt"]["loopback"]
    ]
    for model, flag in (("roles", False), ("point31", True)):
        result = deployment.deployment_user_data("probe-alt", tmp_path, model, role_loopbacks=flag)
        config = yaml.safe_load(base64.b64decode(result[0].split("=", 1)[1]))
        fragment = yaml.safe_load(config["write_files"][0]["content"])["network"]
        assert fragment == deployment.netplan("probe-alt")["network"]
        assert len(fragment["ethernets"]["lo"]["addresses"]) == 5
        assert all(
            "addresses" not in fragment["vlans"][f"{parent}.3404"]
            for parent in ("enp7s0", "enp8s0")
        )
    with pytest.raises(ValueError, match="point31"):
        deployment.deployment_user_data("probe-alt", tmp_path, "interface32", role_loopbacks=True)


def test_configuring_roles_preserves_only_the_numbered_general_links(deployment, monkeypatch):
    current = machine(deployment)
    original = copy.deepcopy(current)
    calls = []

    def maas(*args):
        calls.append(args)
        if args[:2] == ("machine", "read"):
            return current
        if args[:2] == ("vlans", "read"):
            return [
                i["vlan"]
                for i in current["interface_set"]
                if i.get("type") == "vlan" and i["vlan"]["fabric_id"] == int(args[2])
            ]
        return None

    monkeypatch.setattr(deployment, "maas", maas)
    deployment.configure_roles("owned", preserve_general=True)
    general_ids = {str(i["id"]) for i in current["interface_set"] if i["name"].endswith(".3404")}
    changes = [args for args in calls if args[0] == "interface"]
    assert len(changes) == 16
    assert all(args[3] not in general_ids for args in changes)
    assert current == original
    deployment.validate_roles(current, preserve_general=True)
    with pytest.raises(RuntimeError, match="Role interface intent"):
        deployment.validate_roles(current)
    invalid = machine(deployment, numbered=False)
    with pytest.raises(RuntimeError, match="static address"):
        deployment.validate_roles(invalid, preserve_general=True)


def test_role_loopbacks_reject_pure_interface32_before_any_action(
    deployment, monkeypatch, tmp_path
):
    monkeypatch.setattr(deployment, "ROOT", tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["deploy_probe.py", "--model", "interface32", "--role-loopbacks"]
    )

    def forbidden(*args):
        raise AssertionError("Argument rejection must precede MAAS/LXD actions")

    monkeypatch.setattr(deployment, "maas", forbidden)
    monkeypatch.setattr(deployment, "command", forbidden)
    with pytest.raises(SystemExit) as error:
        deployment.main()
    assert error.value.code == 2
    assert not list(tmp_path.iterdir())


def test_native_netplan_merge_keeps_fast31_and_all_loopback_sources(deployment, tmp_path):
    executable = shutil.which("netplan")
    assert executable is not None, "This explicit native generator test requires installed Netplan"
    directory = tmp_path / "root/etc/netplan"
    directory.mkdir(parents=True)
    target = "probe-alt"
    base = {
        "network": {
            "version": 2,
            "ethernets": {parent: {} for parent in ("enp7s0", "enp8s0")},
            "vlans": {
                f"{parent}.3404": {
                    "id": 3404,
                    "link": parent,
                    "addresses": [deployment.fast_address(target, "point31", side)],
                }
                for side, parent in enumerate(("enp7s0", "enp8s0"))
            },
        }
    }
    (directory / "50-maas.yaml").write_text(yaml.safe_dump(base))
    option = deployment.deployment_user_data(target, tmp_path, "point31", role_loopbacks=True)[0]
    config = yaml.safe_load(base64.b64decode(option.split("=", 1)[1]))
    (directory / "99-roles.yaml").write_text(config["write_files"][0]["content"])
    for path in directory.iterdir():
        path.chmod(0o600)
    result = subprocess.run(
        [executable, "generate", "--root-dir", str(tmp_path / "root")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = tmp_path / "root/run/systemd/network"
    for side, parent in enumerate(("enp7s0", "enp8s0")):
        generated = (output / f"10-netplan-{parent}.3404.network").read_text()
        assert "Address=" + deployment.fast_address(target, "point31", side) in generated
        assert "LinkLocalAddressing=ipv6" in generated
    loopback = (output / "10-netplan-lo.network").read_text()
    assert all(
        "Address=" + address in loopback
        for address in deployment.netplan(target)["network"]["ethernets"]["lo"]["addresses"]
    )
    assert all(f"Table={role['table']}" in loopback for role in deployment.roles())
