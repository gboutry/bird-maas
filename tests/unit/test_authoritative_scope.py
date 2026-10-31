"""MAAS is the rack authority; absent identity never becomes authorization."""

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from hypothesis import given
from hypothesis import strategies as st
from maas_bird import discovery
from maas_bird.interfaces import decode, encode
from maas_bird.maas import InventoryError, application_scope
from maas_bird.models import Attachments, Card, Identity, Inventory, InventoryRequest, RackRequest
from maas_bird.resolver import resolve_rack
from ops import testing

from tests.factories import authoritative_inventory, rack_inputs


@pytest.fixture
def scope_records():
    machines = [
        {"system_id": "m1", "hardware_uuid": "u1", "tag_names": ["rack-a"]},
        {"system_id": "m2", "hardware_uuid": "u2", "tag_names": ["rack-a"]},
        {"system_id": "m3", "hardware_uuid": "u3", "tag_names": ["rack-b"]},
    ]
    racks = [
        {"id": rack, "peers": [{"id": "tor", "asn": 65001, "chassis_id": "aa"}]}
        for rack in ("a", "b")
    ]
    identities = [
        {"machine_id": f"m{n}", "local_asn": 4200000000 + n, "router_id": f"10.0.0.{n}"}
        for n in (1, 2, 3)
    ]
    return machines, racks, identities


def test_scope_requires_all_members_and_unique_maas_identity(scope_records):
    assert application_scope(*scope_records, ["u1", None]).state == "pending"
    assert application_scope(*scope_records, ["u1", "u3"]).state == "rejected"
    assert application_scope(*scope_records, ["u1", "U1"]).state == "rejected"
    assert application_scope(*scope_records, ["unknown"]).state == "rejected"
    machines, racks, identities = scope_records
    assert (
        application_scope(machines + [machines[0]], racks, identities, ["u1"]).state == "rejected"
    )
    assert application_scope(*scope_records, ["u3"], previous="a").state == "changed"


@given(order=st.permutations(("u1", "u2")), upper=st.booleans())
def test_member_order_and_uuid_case_never_change_scope(order, upper):
    # The generated permutations model independently arriving unit relation bags.
    # UUID case is not an authority boundary; unknown or mixed members are below.
    machines = [
        {"system_id": uid, "hardware_uuid": uid, "tag_names": ["rack-a"]} for uid in ("u1", "u2")
    ]
    requests = [uid.upper() if upper else uid for uid in order]
    result = application_scope(
        machines, [{"id": "a"}], [{"machine_id": uid} for uid in order], requests
    )
    assert result.rack == "a" and result.state == "ready"
    assert result.members == ("u1", "u2")


def provider_state(scope_records, requests, old=None, **kwargs):
    _, racks, identities = scope_records
    relation = testing.Relation(
        "inventory",
        # A consumer-written rack is deliberately ignored, even when it lies.
        remote_app_data=encode(RackRequest(rack="b")),
        remote_units_data={
            n: encode(InventoryRequest(system_uuid=uid)) for n, uid in enumerate(requests)
        },
        local_app_data=encode(old or Inventory()),
    )
    secret = testing.Secret({"api-key": "consumer:token:secret"})
    state = testing.State(
        leader=True,
        relations={relation},
        secrets={secret},
        config={
            "maas-url": "https://maas/MAAS/",
            "maas-credentials": secret.id,
            "rack-profiles": json.dumps(racks),
            "host-identities": json.dumps(identities),
        },
        **kwargs,
    )
    return state, relation


def test_provider_derives_scope_from_all_units_not_consumer_app(
    scope_records, charm_context, monkeypatch
):
    monkeypatch.setattr("maas_bird.maas.MaasClient.machines", lambda _: scope_records[0])
    ctx = charm_context("maas-inventory")
    state, relation = provider_state(scope_records, ["u1", "u2"])
    out = ctx.run(ctx.on.config_changed(), state)
    result = decode(Inventory, out.get_relation(relation.id).local_app_data)
    assert result.rack == "a" and result.scope_state == "ready"
    assert result.member_uuids == ("u1", "u2")
    assert result.hosts is not None
    assert {host.rack for host in result.hosts} == {"a"}


@pytest.mark.parametrize("requests", [["u1", "u3"], ["u1", "unknown"], ["u1", "u1"]])
def test_provider_conflicts_publish_zero_authority(
    scope_records, charm_context, monkeypatch, requests
):
    monkeypatch.setattr("maas_bird.maas.MaasClient.machines", lambda _: scope_records[0])
    ctx = charm_context("maas-inventory")
    old = Inventory(rack="a", member_uuids=("u1",), scope_state="ready", hosts=())
    state, relation = provider_state(scope_records, requests, old)
    out = ctx.run(ctx.on.config_changed(), state)
    result = decode(Inventory, out.get_relation(relation.id).local_app_data)
    assert result.rack == "a" and result.scope_state == "conflict" and result.hosts == ()


def test_scope_move_tombstone_survives_leader_state_loss(
    scope_records, charm_context, monkeypatch
):
    monkeypatch.setattr("maas_bird.maas.MaasClient.machines", lambda _: scope_records[0])
    ctx = charm_context("maas-inventory")
    old = Inventory(rack="a", member_uuids=("u1",), scope_state="ready", hosts=())
    state, relation = provider_state(scope_records, ["u3"], old)
    out = ctx.run(ctx.on.config_changed(), state)
    revoked = decode(Inventory, out.get_relation(relation.id).local_app_data)
    assert revoked.scope_state == "revoked" and revoked.rack == "a" and revoked.hosts == ()
    # Only relation data is carried to a new leader; StoredState is intentionally absent.
    state2, relation2 = provider_state(scope_records, ["u1"], revoked)
    out2 = ctx.run(ctx.on.leader_elected(), state2)
    assert decode(Inventory, out2.get_relation(relation2.id).local_app_data) == revoked


def test_api_outage_cannot_bind_initial_scope(scope_records, charm_context, monkeypatch):
    monkeypatch.setattr(
        "maas_bird.maas.MaasClient.machines", Mock(side_effect=InventoryError("offline"))
    )
    ctx = charm_context("maas-inventory")
    state, relation = provider_state(scope_records, ["u1"])
    out = ctx.run(ctx.on.config_changed(), state)
    result = decode(Inventory, out.get_relation(relation.id).local_app_data)
    assert result.rack is None and not result.hosts and result.scope_state == "pending"


def test_pending_member_still_delivers_known_removal(scope_records, charm_context, monkeypatch):
    fetch = Mock(return_value=scope_records[0])
    monkeypatch.setattr("maas_bird.maas.MaasClient.machines", fetch)
    ctx = charm_context("maas-inventory")
    state, relation = provider_state(scope_records, ["u1", "u2"])
    first = ctx.run(ctx.on.config_changed(), state)
    old = decode(Inventory, first.get_relation(relation.id).local_app_data)
    fetch.return_value = scope_records[0][1:]
    state2, relation2 = provider_state(scope_records, ["u1", None], old)
    out = ctx.run(ctx.on.update_status(), state2)
    result = decode(Inventory, out.get_relation(relation2.id).local_app_data)
    assert result.rack == "a" and result.scope_state == "pending"
    assert result.hosts is not None
    assert {host.system_uuid for host in result.hosts} == {"u2"}


def test_dpu_scope_withdraws_before_fallible_discovery(charm_context, monkeypatch):
    identity = Identity(scheme="devlink-serial", value="card1")
    old = Attachments(rack="a", cards=(Card(identity=identity),))
    bad = testing.Relation(
        "attachments", remote_app_data=encode(RackRequest(rack="b")), local_unit_data=encode(old)
    )
    good = testing.Relation(
        "attachments", remote_app_data=encode(RackRequest(rack="a")), local_unit_data=encode(old)
    )
    probe = Mock(side_effect=discovery.DiscoveryError("unavailable"))
    monkeypatch.setattr(discovery, "cards", probe)
    ctx = charm_context("dpu-attachments")
    out = ctx.run(ctx.on.update_status(), testing.State(relations={bad, good}))
    withdrawn = decode(Attachments, out.get_relation(bad.id).local_unit_data)
    assert withdrawn.rack == "a" and withdrawn.cards == (Card(identity=identity, attachments=()),)
    assert decode(Attachments, out.get_relation(good.id).local_unit_data) == old
    probe.assert_called_once()


def test_duplicate_claim_across_applications_rejects_both(
    scope_records, charm_context, monkeypatch
):
    monkeypatch.setattr("maas_bird.maas.MaasClient.machines", lambda _: scope_records[0])
    state, first = provider_state(scope_records, ["u1"])
    second = testing.Relation(
        "inventory", remote_units_data={0: encode(InventoryRequest(system_uuid="u1"))}
    )
    state = replace(state, relations={first, second})
    ctx = charm_context("maas-inventory")
    out = ctx.run(ctx.on.config_changed(), state)
    for relation in (first, second):
        result = decode(Inventory, out.get_relation(relation.id).local_app_data)
        assert result.scope_state == "conflict" and result.hosts == ()


def test_existing_bird_identity_change_invalidates_before_waiting(charm_context, monkeypatch):
    inputs = rack_inputs()
    service = Mock()
    service.state.return_value = resolve_rack(**inputs)
    monkeypatch.setattr("maas_bird.rack_runtime.RackService", lambda **_: service)
    monkeypatch.setattr(discovery, "host_uuid", lambda: "replacement-hardware")
    relation = testing.Relation(
        "inventory", remote_app_data=encode(authoritative_inventory(inputs["inventory"]))
    )
    ctx = charm_context("bird")
    out = ctx.run(
        ctx.on.update_status(), testing.State(config={"enabled": True}, relations={relation})
    )
    service.invalidate.assert_called_once()
    service.stop.assert_called_once()
    service.apply_plan.assert_not_called()
    assert isinstance(out.unit_status, testing.BlockedStatus)


def test_invalid_policy_cannot_erase_durable_scope_revocation(
    scope_records, charm_context, monkeypatch
):
    monkeypatch.setattr("maas_bird.maas.MaasClient.machines", lambda _: scope_records[0])
    revoked = Inventory(rack="a", member_uuids=("u1",), scope_state="revoked", hosts=())
    state, relation = provider_state(scope_records, ["u3"], revoked)
    state = replace(state, config=dict(state.config) | {"rack-profiles": "not-json"})
    ctx = charm_context("maas-inventory")
    out = ctx.run(ctx.on.config_changed(), state)
    assert decode(Inventory, out.get_relation(relation.id).local_app_data) == revoked


def test_changed_membership_bypasses_positive_relation_cache(
    scope_records, charm_context, monkeypatch
):
    fetch = Mock(return_value=scope_records[0])
    monkeypatch.setattr("maas_bird.maas.MaasClient.machines", fetch)
    state, relation = provider_state(scope_records, ["u1"])
    ctx = charm_context("maas-inventory")
    first = ctx.run(ctx.on.relation_changed(relation), state)
    assert fetch.call_count == 1
    changed = replace(
        first.get_relation(relation.id),
        remote_units_data={
            0: encode(InventoryRequest(system_uuid="u1")),
            1: encode(InventoryRequest(system_uuid="u2")),
        },
    )
    out = ctx.run(ctx.on.relation_changed(changed), replace(first, relations={changed}))
    assert fetch.call_count == 2
    result = decode(Inventory, out.get_relation(relation.id).local_app_data)
    assert result.scope_state == "ready" and result.member_uuids == ("u1", "u2")
