"""Run Netplan's actual generator against isolated review roots, never apply.

This proves backend parsing and emitted static policy, not live boot/cutover.
The target-version run is also recorded separately in the review evidence.
"""

import configparser
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2] / "examples/rb3-rack-local"
PROVISIONING = ROOT / "provisioning"
NODES = (
    "n1",
    "n5",
    "n6",
    "n1-dpu-numa0",
    "n1-dpu-numa1",
    "n5-dpu-numa0",
    "n5-dpu-numa1",
    "n6-dpu-numa0",
    "n6-dpu-numa1",
)


@pytest.mark.parametrize("node", NODES)
def test_netplan_generates_the_reviewed_objects_without_applying(node, tmp_path):
    source = PROVISIONING / node / "99-zzzz-routed-fabric.yaml"
    network = yaml.safe_load(source.read_text())["network"]
    destination = tmp_path / "etc/netplan" / source.name
    destination.parent.mkdir(parents=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o600)
    subprocess.run(
        ["netplan", "generate", "--root-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    generated = tmp_path / "run/systemd/network"
    loopback = (generated / "10-netplan-lo.network").read_text()
    declared = network["ethernets"]["lo"]
    for address in declared["addresses"]:
        assert f"Address={address}\n" in loopback
    for route in declared["routes"]:
        expected = (
            "[Route]\nDestination=0.0.0.0/0\nType=unreachable\n"
            f"Metric=42760\nTable={route['table']}\n"
        )
        assert expected in loopback
    for rule in declared["routing-policy"]:
        expected = (
            f"[RoutingPolicyRule]\nFrom={rule['from']}\n"
            f"Table={rule['table']}\nPriority={rule['priority']}\n"
        )
        assert expected in loopback
    # PF VF creation/resets and management provisioning remain in the existing
    # source of truth. The review fragment must never take over those devices.
    assert not {"ens1f0np0", "ens2f0np0", "bondm", "br-bondm"} & set(network["ethernets"])
    assert "bonds" not in network and "bridges" not in network
    for name, settings in network.get("vlans", {}).items():
        config = configparser.ConfigParser()
        config.read(generated / f"10-netplan-{name}.netdev")
        assert config["NetDev"]["Kind"] == "vlan"
        assert int(config["VLAN"]["Id"]) == settings["id"]


def test_provisioning_and_charm_contexts_have_one_static_contract():
    config = yaml.safe_load((ROOT / "host-bird.yaml").read_text())["mbird-acceptance-rb3"]
    assert config["policy-protocol"] == 4
    assert config["enabled"] is False
    domains = json.loads(config["routing-domains"])
    for host in json.loads((ROOT / "hosts.json").read_text()):
        net = yaml.safe_load(
            (PROVISIONING / host["name"] / "99-zzzz-routed-fabric.yaml").read_text()
        )["network"]
        desired = {
            (c["table"], c["rule_priority"], s)
            for domain in domains
            for c in domain["contexts"]
            if c["machine_id"] == host["machine_id"]
            for s in c["sources"]
        }
        actual = {
            (r["table"], r["priority"], r["from"])
            for r in net["ethernets"]["lo"]["routing-policy"]
        }
        assert actual == desired
        assert {(v["id"], v["link"]) for v in net["vlans"].values()} == {
            (p["vlan"], p["host_parent"]) for p in host["paths"]
        }


@pytest.mark.parametrize("node", NODES[3:])
def test_dpu_provisioning_retains_existing_forwarding_ownership(node):
    intent = json.loads((PROVISIONING / node / "ovs-provisioning.json").read_text())
    assert intent["preserve_all_existing_ports"] is True
    assert intent["preserve_uplink_trunks"] is True
    assert intent["never_reset_vf_count"] is True
    assert intent["never_restart_ovs"] is True
    assert intent["retain"]["mode"] == "802.3ad"
    assert len(intent["ports"]) == 7
    assert {p["name"] for p in intent["ports"] if p["type"] == "doca"} == {
        f"pf0vf{vf}" for vf in range(55, 60)
    }
    assert len({p["tag"] for p in intent["ports"] if p["type"] == "internal"}) == 2
