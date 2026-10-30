import json
from ipaddress import IPv6Address

import pytest
from maas_bird import discovery
from maas_bird.models import Identity


def test_devlink_identity_preserves_card_serial(monkeypatch):
    sample = {
        "info": {
            "pci/0000:41:00.0": {"serial_number": "card-serial", "board.serial_number": "board"},
            "pci/0000:41:00.1": {"serial_number": "card-serial"},
            "auxiliary/mlx5_core.eth.0": {"driver": "mlx5_core.eth"},
        }
    }
    monkeypatch.setattr(discovery, "command", lambda _: json.dumps(sample))
    cards = discovery.cards()
    assert set(cards) == {"0000:41:00.0", "0000:41:00.1"}
    assert cards["0000:41:00.0"] == cards["0000:41:00.1"]


def test_hardware_uuid_is_normalized(tmp_path):
    path = tmp_path / "class/dmi/id"
    path.mkdir(parents=True)
    (path / "product_uuid").write_text("ABCD-EF\n")
    assert discovery.host_uuid(tmp_path) == "abcd-ef"
    (path / "product_uuid").write_text("00000000-0000-0000-0000-000000000000")
    with pytest.raises(discovery.DiscoveryError):
        discovery.host_uuid(tmp_path)


def test_vf_number_and_vlan_are_observed_not_guessed(tmp_path, monkeypatch):
    pci = tmp_path / "bus/pci/devices"
    pf = pci / "0000:41:00.0"
    vf = pci / "0000:41:08.2"
    pf.mkdir(parents=True)
    vf.mkdir()
    (pf / "virtfn63").symlink_to(vf)
    (vf / "physfn").symlink_to(pf)
    net = tmp_path / "class/net/ens1f0v63"
    net.mkdir(parents=True)
    (net / "device").symlink_to(vf)
    identity = Identity(scheme="devlink-serial", value="card1")
    monkeypatch.setattr(discovery, "cards", lambda: {pf.name: identity})
    addresses = [
        {"ifname": "ens1f0v63", "ifindex": 1, "addr_info": []},
        {
            "ifname": "fabric1",
            "ifindex": 2,
            "link_index": 1,
            "linkinfo": {"info_kind": "vlan", "info_data": {"id": 3414}},
            "addr_info": [{"family": "inet6", "scope": "link", "local": "fe80::11"}],
        },
    ]
    monkeypatch.setattr(discovery, "command", lambda _: json.dumps(addresses))
    links = discovery.links(tmp_path)
    vlan = next(link for link in links if link.interface == "fabric1")
    assert (vlan.identity, vlan.controller, vlan.pf, vlan.vf, vlan.vlan) == (
        identity,
        0,
        0,
        63,
        3414,
    )
    assert vlan.link_local == IPv6Address("fe80::11")


def test_permanent_mac_backend_needs_no_dpu(tmp_path, monkeypatch):
    pf = tmp_path / "bus/pci/devices/0000:41:00.0"
    (pf / "net/eno1").mkdir(parents=True)
    net = tmp_path / "class/net/eno1"
    net.mkdir(parents=True)
    (net / "device").symlink_to(pf)
    monkeypatch.setattr(discovery, "cards", lambda: {})
    addresses = [
        {
            "ifname": "eno1",
            "ifindex": 1,
            "addr_info": [{"family": "inet6", "scope": "link", "local": "fe80::11"}],
        }
    ]
    monkeypatch.setattr(
        discovery,
        "command",
        lambda args: (
            "Permanent address: 00:11:22:33:44:55"
            if args[0] == "ethtool"
            else json.dumps(addresses)
        ),
    )
    links = discovery.links(tmp_path)
    assert links[0].identity == Identity(scheme="permanent-mac", value="00:11:22:33:44:55")


def test_discovery_errors_do_not_emit_subprocess_output(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("private environment detail")

    monkeypatch.setattr(discovery.subprocess, "run", fail)
    with pytest.raises(discovery.DiscoveryError) as exc:
        discovery.command(["devlink", "-j", "dev", "info"])
    assert str(exc.value) == "Read-only command failed: devlink"


def test_unknown_numa_is_explicit_in_local_link():
    from maas_bird.discovery import LocalLink
    from maas_bird.models import Identity

    link = LocalLink(
        "eth0", Identity(scheme="permanent-mac", value="00:11:22:33:44:55"), 0, 0, None, None, None
    )
    assert link.numa_node is None
