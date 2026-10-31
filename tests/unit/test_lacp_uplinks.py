"""Read-only physical-LLDP to OVS-LACP joins, including foreign-card rejection."""

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from maas_bird import discovery
from maas_bird.models import Identity

from tests.factories import ovs_port_binding, ovs_snapshot_text


@pytest.fixture
def lacp(tmp_path, monkeypatch):
    identity = Identity(scheme="devlink-serial", value="card-a")
    other = Identity(scheme="devlink-serial", value="card-b")
    net = tmp_path / "class/net"
    bond = net / "bond0"
    (bond / "bonding").mkdir(parents=True)
    (bond / "bonding/mode").write_text("802.3ad 4\n")
    (bond / "bonding/slaves").write_text("p0 p1\n")
    pf = tmp_path / "devices/0000:03:00.0"
    for member in ("p0", "p1"):
        device = pf / f"auxiliary/{member}"
        device.mkdir(parents=True)
        (net / member).mkdir()
        (net / member / "device").symlink_to(device)
        (net / member / "master").symlink_to(bond)
    calls = []
    binding = ovs_port_binding()
    binding = binding.model_copy(
        update={
            "uplink": binding.uplink.model_copy(update={"bond": "bond0", "members": ("p0", "p1")})
        }
    )

    def command(args):
        calls.append(args)
        if "list" in args:
            return ovs_snapshot_text(binding)
        raise discovery.DiscoveryError("Physical member is not an OVS interface")

    monkeypatch.setattr(discovery, "command", command)
    return tmp_path, identity, {pf.name: identity, "0000:04:00.0": other}, calls


def bridge(lacp):
    root, identity, serials, _ = lacp
    return discovery.ovs_uplink_bridge("p0", identity, serials, root, bond="bond0")


def test_lldp_member_resolves_only_its_verified_lacp_master(lacp):
    assert bridge(lacp) == "br-data"
    assert len(lacp[3]) == 1
    assert lacp[3][0].count("list") == 3


@pytest.mark.parametrize("fault", ["mode", "absent", "duplicate", "single", "foreign", "master"])
def test_structural_mismatch_never_queries_the_bond_bridge(lacp, fault):
    root, _, _, calls = lacp
    net = root / "class/net"
    if fault == "mode":
        (net / "bond0/bonding/mode").write_text("active-backup 1\n")
    elif fault == "absent":
        (net / "bond0/bonding/slaves").write_text("p1 p2\n")
    elif fault == "duplicate":
        (net / "bond0/bonding/slaves").write_text("p0 p1 p1\n")
    elif fault == "single":
        (net / "bond0/bonding/slaves").write_text("p0\n")
    elif fault == "foreign":
        foreign = root / "devices/0000:04:00.0/auxiliary/p1"
        foreign.mkdir(parents=True)
        (net / "p1/device").unlink()
        (net / "p1/device").symlink_to(foreign)
    else:
        (net / "p1/master").unlink()
        (net / "p1/master").symlink_to(net / "another-bond")
    assert bridge(lacp) is None
    assert calls == []


def test_lacp_read_failure_is_unknown_not_success(lacp):
    (lacp[0] / "class/net/bond0/bonding/mode").unlink()
    with pytest.raises(discovery.DiscoveryError, match="kernel LACP"):
        bridge(lacp)


def test_direct_ovs_uplink_keeps_existing_discovery_path(monkeypatch):
    calls = []

    def command(args):
        calls.append(args)
        return "br-direct\n"

    monkeypatch.setattr(discovery, "command", command)
    identity = Identity(scheme="devlink-serial", value="card-a")
    assert discovery.ovs_uplink_bridge("p0", identity, {}, Path("/absent")) == "br-direct"
    assert calls == [["ovs-vsctl", "iface-to-br", "p0"]]


@settings(max_examples=30)
@given(name=st.text(alphabet=["/", "\0", "\n", "é"], min_size=1, max_size=20))
def test_unsafe_uplink_names_never_reach_native_commands(name):
    """Generate hostile selector text; command construction must reject it first.

    The strategy deliberately includes path traversal, option-like, non-ASCII and
    control characters. This guards both subprocess arguments and sysfs joins.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(discovery, "command", lambda _: pytest.fail("native command reached"))
    with pytest.raises(ValueError, match="Unsafe OVS"):
        discovery.ovs_uplink_bridge(name, Identity(scheme="devlink-serial", value="a"), {})
