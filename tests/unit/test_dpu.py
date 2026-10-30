import pytest
from maas_bird import discovery, dpu
from maas_bird.models import Identity


@pytest.fixture
def observed(monkeypatch):
    monkeypatch.setattr(
        discovery,
        "cards",
        lambda: {"0000:03:00.0": Identity(scheme="devlink-serial", value="card1")},
    )
    ports = {
        "port": {
            "pci/0000:03:00.0/65599": {
                "controller": 0,
                "pfnum": 0,
                "vfnum": 63,
                "netdev": "pf0vf63",
            }
        }
    }
    lldp = {
        "lldp": {
            "interface": {
                "p0": {"chassis": {"tor1": {"id": {"type": "mac", "value": "aa:aa:aa:aa:aa:01"}}}}
            }
        }
    }
    monkeypatch.setattr(discovery, "dpu_observations", lambda: (ports, lldp))
    calls = []

    def command(args):
        calls.append(args)
        if args[-1] == "trunks":
            return "[3414]"
        return "br-data\n"

    monkeypatch.setattr(discovery, "command", command)
    config = [
        {
            "serial": "card1",
            "id": "path1",
            "function": {"controller": 0, "pf": 0, "vf": 63},
            "domain": "storage-public",
            "vlan": 3414,
            "chassis_id": "aa:aa:aa:aa:aa:01",
            "peer_link_local": "fe80::1",
            "uplink": "p0",
        }
    ]
    return config, ports, lldp, calls


def test_actual_observations_gate_readiness(observed):
    config, ports, lldp, calls = observed
    assert dpu.observe(config).cards[0].attachments[0].ready is True
    assert calls
    assert all(args[:2] in [["ovs-vsctl", "iface-to-br"], ["ovs-vsctl", "get"]] for args in calls)
    lldp["lldp"]["interface"]["p0"]["chassis"]["tor1"]["id"]["value"] = "unapproved"
    assert dpu.observe(config).cards[0].attachments[0].ready is False


def test_absent_card_rejected(observed):
    config, *_ = observed
    config[0]["serial"] = "different-host"
    with pytest.raises(ValueError, match="not present"):
        dpu.observe(config)


def test_wrong_vf_is_not_ready(observed):
    config, ports, *_ = observed
    ports["port"]["pci/0000:03:00.0/65599"]["vfnum"] = 62
    assert dpu.observe(config).cards[0].attachments[0].ready is False


def test_missing_explicit_trunk_membership_is_not_ready(observed, monkeypatch):
    config, *_ = observed
    monkeypatch.setattr(
        discovery, "command", lambda args: "[]" if args[-1] == "trunks" else "br-data"
    )
    assert dpu.observe(config).cards[0].attachments[0].ready is False
