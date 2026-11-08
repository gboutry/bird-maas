import copy
import json
from dataclasses import replace
from ipaddress import IPv6Address
from unittest.mock import Mock

import pytest
import scenario as testing
from maas_bird import discovery, nic
from maas_bird.interfaces import decode
from maas_bird.models import Attachments, Identity


@pytest.fixture
def observed(monkeypatch):
    identity = Identity(scheme="permanent-mac", value="02:00:00:00:00:01")
    link = discovery.LocalLink(
        "enp7s0.3404",
        identity,
        0,
        3,
        None,
        3404,
        IPv6Address("fe80::2"),
        parent_interface="enp7s0",
        link_up=True,
    )
    links = [link]
    neighbors = {
        "lldp": {
            "interface": {
                "enp7s0": {
                    "chassis": {"tor": {"id": {"type": "mac", "value": "02:00:00:aa:00:01"}}}
                }
            }
        }
    }
    config = [
        {
            "identity": identity.model_dump(),
            "function": {"controller": 0, "pf": 3},
            "id": "general-a",
            "domain": "general",
            "vlan": 3404,
            "chassis_id": "02:00:00:aa:00:01",
            "peer_link_local": "fe80::1",
            "uplink": "enp7s0",
        }
    ]
    monkeypatch.setattr(discovery, "links", lambda: links)
    calls = []

    def command(argv):
        calls.append(argv)
        assert argv == ["lldpcli", "-f", "json", "show", "neighbors"]
        return json.dumps(neighbors)

    monkeypatch.setattr(discovery, "command", command)
    return config, links, neighbors, calls


def attachment(config):
    return one_attachment(nic.observe(config))


def one_attachment(result):
    assert result.cards is not None
    assert len(result.cards) == 1
    assert result.cards[0].attachments is not None
    assert len(result.cards[0].attachments) == 1
    return result.cards[0].attachments[0]


def test_native_nic_uses_observed_pf_coordinates(observed):
    config, links, _, calls = observed
    item = attachment(config)
    assert item.ready is True
    assert item.function == item.observed_function
    assert item.function.vf is None
    assert links[0].numa_node is None
    assert len(calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"link_up": False},
        {"link_up": None},
        {"parent_interface": "enp8s0"},
        {"link_local": None},
        {"link_local": IPv6Address("2001:db8::1")},
        {"link_local": IPv6Address("fe80::2%enp7s0")},
        {"pf": 4},
        {"vlan": 3403},
    ],
)
def test_native_mismatch_cannot_grant_readiness(observed, change):
    config, links, _, _ = observed
    links[0] = replace(links[0], **change)
    assert attachment(config).ready is False


def test_wrong_identity_withdraws_instead_of_publishing_a_configured_card(observed):
    config, links, _, _ = observed
    links[0] = replace(
        links[0], identity=Identity(scheme="permanent-mac", value="02:00:00:00:00:02")
    )
    assert nic.observe(config) == Attachments(cards=())


def test_missing_parent_is_authoritative_withdrawal(observed):
    config, links, _, _ = observed
    links.clear()
    assert nic.observe(config) == Attachments(cards=())


@pytest.mark.parametrize("other_parent", ["enp7s0", "enp8s0"])
def test_ambiguous_native_matches_are_not_ready(observed, other_parent):
    config, links, _, _ = observed
    links.append(replace(links[0], interface="other.3404", parent_interface=other_parent))
    assert attachment(config).ready is False


@pytest.mark.parametrize("mode", ["missing", "wrong", "multiple"])
def test_lldp_must_match_exactly_one_approved_chassis(observed, mode):
    config, _, neighbors, _ = observed
    chassis = neighbors["lldp"]["interface"]["enp7s0"]["chassis"]
    if mode == "missing":
        chassis.clear()
    elif mode == "wrong":
        chassis["tor"]["id"]["value"] = "02:00:00:bb:00:01"
    else:
        chassis["other"] = {"id": {"type": "mac", "value": "02:00:00:bb:00:01"}}
    assert attachment(config).ready is False


def test_unknown_snapshot_is_not_an_empty_observation(observed):
    config, _, neighbors, _ = observed
    neighbors.clear()
    with pytest.raises(discovery.DiscoveryError):
        nic.observe(config)


def test_duplicate_policy_is_rejected(observed):
    config, _, _, _ = observed
    with pytest.raises(ValueError, match="Duplicate"):
        nic.observe(config + copy.deepcopy(config))


def test_explicit_empty_policy_withdraws_without_discovery(monkeypatch):
    read = Mock(side_effect=AssertionError("No observation needed to withdraw all intent"))
    monkeypatch.setattr(discovery, "links", read)
    assert nic.observe([]) == Attachments(cards=())
    read.assert_not_called()


def test_native_relation_publishes_then_withdraws_observed_absence(charm_context, observed):
    config, links, _, _ = observed
    ctx = charm_context("dpu-attachments")
    relation = testing.Relation("attachments")
    state = testing.State(
        config={"backend": "native-nic", "attachments": json.dumps(config)}, relations={relation}
    )
    ready = ctx.run(ctx.on.relation_joined(relation), state)
    assert isinstance(ready.unit_status, testing.ActiveStatus)
    assert (
        one_attachment(decode(Attachments, ready.get_relation(relation.id).local_unit_data)).ready
        is True
    )
    links.clear()
    withdrawn = ctx.run(ctx.on.update_status(), ready)
    assert decode(Attachments, withdrawn.get_relation(relation.id).local_unit_data) == Attachments(
        cards=()
    )


def test_native_discovery_failure_keeps_unknown_distinct(charm_context, observed, monkeypatch):
    config, _, _, _ = observed
    ctx = charm_context("dpu-attachments")
    relation = testing.Relation("attachments")
    state = testing.State(
        config={"backend": "native-nic", "attachments": json.dumps(config)}, relations={relation}
    )
    ready = ctx.run(ctx.on.relation_joined(relation), state)
    before = ready.get_relation(relation.id).local_unit_data.copy()

    def fail():
        raise discovery.DiscoveryError("Unavailable")

    monkeypatch.setattr(discovery, "links", fail)
    unknown = ctx.run(ctx.on.update_status(), ready)
    assert isinstance(unknown.unit_status, testing.WaitingStatus)
    assert unknown.get_relation(relation.id).local_unit_data == before


def test_dpu_backend_does_not_fall_back_to_native(charm_context, observed, monkeypatch):
    config, _, _, _ = observed
    monkeypatch.setattr(discovery, "cards", lambda: {})
    native = Mock(side_effect=AssertionError("Native backend needs explicit selection"))
    monkeypatch.setattr(nic, "observe", native)
    ctx = charm_context("dpu-attachments")
    relation = testing.Relation("attachments")
    out = ctx.run(
        ctx.on.relation_joined(relation),
        testing.State(config={"attachments": json.dumps(config)}, relations={relation}),
    )
    assert decode(Attachments, out.get_relation(relation.id).local_unit_data) == Attachments(
        cards=()
    )
    native.assert_not_called()
