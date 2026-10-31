"""Each actual Juju unit gets its own MAAS authority across rack boundaries."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import jsonschema
import pytest
import scenario as testing
from hypothesis import given
from hypothesis import strategies as st
from maas_bird import discovery
from maas_bird.interfaces import encode
from maas_bird.inventory import read_response, response_data
from maas_bird.maas import InventoryError, unit_inventory
from maas_bird.models import (
    Attachments,
    Card,
    Identity,
    InventoryRequest,
    UnitInventory,
)
from maas_bird.rack import ScopeError, attachments_for_host
from maas_bird.resolver import resolve_rack

from tests.factories import inventory_response, rack_inputs


@pytest.fixture
def records():
    machines = [
        {"system_id": f"m{n}", "hardware_uuid": f"u{n}", "tag_names": [f"rack-{rack}"]}
        for n, rack in [(1, "a"), (2, "a"), (3, "b")]
    ]
    racks = [
        {"id": rack, "peers": [{"id": "tor", "asn": 65001, "chassis_id": rack}]}
        for rack in ("a", "b")
    ]
    identities = [
        {"machine_id": f"m{n}", "local_asn": 4200000000 + n, "router_id": f"10.0.0.{n}"}
        for n in (1, 2, 3)
    ]
    return machines, racks, identities


def resolved(records, uid="u1", previous=None, name="bird/0", **flags):
    return unit_inventory(
        *records,
        InventoryRequest(system_uuid=uid),
        previous or UnitInventory(unit_name=name),
        name,
        available=flags.pop("available", True),
        fresh=flags.pop("fresh", True),
        **flags,
    )


def state_for(records, requests, old=()):
    _, racks, identities = records
    data = {}
    for document in old:
        data.update(response_data(document))
    relation = testing.Relation(
        "inventory",
        local_app_data=data,
        remote_app_data={"rack": '"operator-cannot-select-this"'},
        remote_units_data={
            n: encode(InventoryRequest(system_uuid=uid)) for n, uid in enumerate(requests)
        },
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
    )
    return state, relation


def response(out, relation, unit=0):
    return read_response(out.get_relation(relation.id).local_app_data, f"remote/{unit}")


def test_different_racks_share_one_application(records, charm_context, monkeypatch):
    monkeypatch.setattr("maas_bird.maas.MaasClient.machines", lambda _: records[0])
    state, relation = state_for(records, ["u1", "u3"])
    ctx = charm_context("maas-inventory")
    out = ctx.run(ctx.on.config_changed(), state)
    assert response(out, relation, 0).rack == "a"
    assert response(out, relation, 1).rack == "b"
    assert response(out, relation, 0).host.system_uuid == "u1"
    assert response(out, relation, 1).host.system_uuid == "u3"
    assert all(response(out, relation, n).scope_state == "ready" for n in (0, 1))
    assert set(out.get_relation(relation.id).local_app_data) == {"unit-0", "unit-1"}


@pytest.mark.parametrize("bad", [None, "unknown"])
def test_unknown_unit_does_not_block_another_rack(records, charm_context, monkeypatch, bad):
    monkeypatch.setattr("maas_bird.maas.MaasClient.machines", lambda _: records[0])
    state, relation = state_for(records, [bad, "u3"])
    ctx = charm_context("maas-inventory")
    out = ctx.run(ctx.on.config_changed(), state)
    assert response(out, relation, 0).scope_state == ("pending" if bad is None else "conflict")
    assert response(out, relation, 0).host is None
    assert response(out, relation, 1).scope_state == "ready"


def test_duplicate_claims_only_reject_conflicting_units(records, charm_context, monkeypatch):
    monkeypatch.setattr("maas_bird.maas.MaasClient.machines", lambda _: records[0])
    state, relation = state_for(records, ["u1", "u1", "u3"])
    ctx = charm_context("maas-inventory")
    out = ctx.run(ctx.on.config_changed(), state)
    assert all(response(out, relation, n).scope_state == "conflict" for n in (0, 1))
    assert response(out, relation, 2).scope_state == "ready"


def test_duplicate_claims_across_relations_reject_only_claimants(
    records, charm_context, monkeypatch
):
    monkeypatch.setattr("maas_bird.maas.MaasClient.machines", lambda _: records[0])
    state, first = state_for(records, ["u1", "u3"])
    second = testing.Relation(
        "inventory", remote_units_data={0: encode(InventoryRequest(system_uuid="u1"))}
    )
    ctx = charm_context("maas-inventory")
    out = ctx.run(ctx.on.config_changed(), replace(state, relations={first, second}))
    assert response(out, first).scope_state == "conflict"
    assert response(out, second).scope_state == "conflict"
    assert response(out, first, 1).scope_state == "ready"


def test_rack_move_revokes_only_that_unit_and_survives_leader_loss(
    records, charm_context, monkeypatch
):
    original = resolved(records, name="remote/0")
    moved = [
        dict(row, tag_names=["rack-b"]) if row["system_id"] == "m1" else row for row in records[0]
    ]
    monkeypatch.setattr("maas_bird.maas.MaasClient.machines", lambda _: moved)
    state, relation = state_for(records, ["u1", "u3"], [original])
    ctx = charm_context("maas-inventory")
    out = ctx.run(ctx.on.config_changed(), state)
    revoked = response(out, relation)
    assert revoked.scope_state == "revoked" and revoked.rack == "a" and revoked.host is None
    assert response(out, relation, 1).scope_state == "ready"
    # No StoredState is carried to this fresh leader; the provider-owned bag persists.
    new_state, new_relation = state_for(records, ["u1", "u3"], [revoked])
    out2 = ctx.run(ctx.on.leader_elected(), new_state)
    assert response(out2, new_relation) == revoked
    assert response(out2, new_relation, 1).scope_state == "ready"


def test_uuid_change_is_sticky_even_with_same_rack(records):
    previous = resolved(records)
    changed = resolved(records, "u2", previous)
    assert changed.scope_state == "revoked" and changed.system_uuid == "u1"
    assert resolved(records, "u1", changed) == changed


def test_outage_cannot_bind_new_unit_or_grant_new_peer(records, charm_context, monkeypatch):
    monkeypatch.setattr(
        "maas_bird.maas.MaasClient.machines", Mock(side_effect=InventoryError("offline"))
    )
    state, relation = state_for(records, ["u1", "u3"], [resolved(records, name="remote/0")])
    ctx = charm_context("maas-inventory")
    out = ctx.run(ctx.on.config_changed(), state)
    assert response(out, relation).scope_state == "pending"
    assert response(out, relation).host == resolved(records).host
    assert (
        response(out, relation, 1).scope_state == "pending"
        and response(out, relation, 1).host is None
    )


def test_outage_still_delivers_known_policy_removal(records):
    previous = resolved(records)
    machines, racks, _ = records
    removed = resolved((machines, racks, []), previous=previous, available=False)
    assert removed.rack == "a" and removed.host is None
    assert removed.scope_state == "pending"


def test_missing_request_does_not_freeze_known_maas_removal(records):
    previous = resolved(records)
    machines, racks, identities = records
    removed = resolved((machines[1:], racks, identities), None, previous)
    assert removed.scope_state == "conflict" and removed.host is None


def test_new_unit_bypasses_positive_relation_cache(records, charm_context, monkeypatch):
    fetch = Mock(return_value=records[0])
    monkeypatch.setattr("maas_bird.maas.MaasClient.machines", fetch)
    state, relation = state_for(records, ["u1"])
    ctx = charm_context("maas-inventory")
    first = ctx.run(ctx.on.relation_changed(relation), state)
    changed = replace(
        first.get_relation(relation.id),
        remote_units_data={
            0: encode(InventoryRequest(system_uuid="u1")),
            1: encode(InventoryRequest(system_uuid="u3")),
        },
    )
    out = ctx.run(ctx.on.relation_changed(changed), replace(first, relations={changed}))
    assert fetch.call_count == 2
    assert (
        response(out, changed, 1).scope_state == "ready" and response(out, changed, 1).rack == "b"
    )


def test_bad_global_policy_cannot_erase_revoked_unit(records, charm_context, monkeypatch):
    monkeypatch.setattr("maas_bird.maas.MaasClient.machines", lambda _: records[0])
    revoked = resolved(records, "u2", resolved(records, name="remote/0"), name="remote/0")
    state, relation = state_for(records, ["u1"], [revoked])
    state = replace(state, config=dict(state.config) | {"rack-profiles": "not-json"})
    ctx = charm_context("maas-inventory")
    out = ctx.run(ctx.on.config_changed(), state)
    assert response(out, relation) == revoked


def test_response_is_bound_to_actual_juju_unit(records):
    data = response_data(resolved(records, name="bird/1"))
    data["unit-0"] = data["unit-1"]
    with pytest.raises(ScopeError, match="another Juju unit"):
        read_response(data, "bird/0")
    assert read_response(data, "bird/2").scope_state == "pending"


@given(
    racks=st.lists(st.sampled_from(["a", "b", "c"]), min_size=1, max_size=20),
    reverse=st.booleans(),
)
def test_unit_scope_is_independent_of_other_unit_racks_and_order(racks, reverse):
    # Generate mixed-rack applications. Each response must depend on that unit's
    # native UUID alone; iteration order and other units cannot select its rack.
    machines = [
        {"system_id": f"m{i}", "hardware_uuid": f"u{i}", "tag_names": [f"rack-{rack}"]}
        for i, rack in enumerate(racks)
    ]
    profiles = [
        {"id": rack, "peers": [{"id": "tor", "asn": 65001, "chassis_id": rack}]}
        for rack in set(racks)
    ]
    identities = [
        {"machine_id": f"m{i}", "local_asn": 4200000000 + i, "router_id": f"10.0.0.{i + 1}"}
        for i in range(len(racks))
    ]
    records = (list(reversed(machines)) if reverse else machines, profiles, identities)
    for i, rack in enumerate(racks):
        document = resolved(records, f"U{i}", name=f"bird/{i}")
        assert document.rack == rack and document.system_uuid == f"u{i}"
        assert document.scope_state == "ready"
        assert read_response(response_data(document), f"bird/{i}") == document
        assert len(next(iter(response_data(document).values())).encode()) < 48 * 1024


def test_dpu_observations_are_rack_neutral(charm_context, monkeypatch):
    identity = Identity(scheme="devlink-serial", value="card1")
    monkeypatch.setattr(discovery, "cards", lambda: {"pf0": identity})
    relation = testing.Relation("attachments", remote_app_data={"rack": '"irrelevant"'})
    ctx = charm_context("dpu-attachments")
    out = ctx.run(ctx.on.update_status(), testing.State(relations={relation}))
    bag = out.get_relation(relation.id).local_unit_data
    assert "rack" not in bag
    document = attachments_for_host(bag, {identity})
    assert document == Attachments(cards=(Card(identity=identity, attachments=()),))


def test_bird_identity_change_invalidates_before_waiting(charm_context, monkeypatch):
    inputs = rack_inputs()
    service = Mock()
    service.state.return_value = resolve_rack(**inputs)
    monkeypatch.setattr("maas_bird.rack_runtime.RackService", lambda **_: service)
    monkeypatch.setattr(discovery, "host_uuid", lambda: "replacement-hardware")
    relation = testing.Relation(
        "inventory", remote_app_data=inventory_response(inputs["inventory"])
    )
    ctx = charm_context("bird")
    out = ctx.run(
        ctx.on.update_status(), testing.State(config={"enabled": True}, relations={relation})
    )
    service.invalidate.assert_called_once()
    service.stop.assert_called_once()
    service.apply_plan.assert_not_called()
    assert isinstance(out.unit_status, testing.BlockedStatus)


def test_exported_schemas_describe_actual_unit_databag(records):
    root = Path(__file__).resolve().parents[2]
    document = resolved(records)
    wire = response_data(document)
    provider = json.loads((root / "docs/schemas/machine-inventory-provider.json").read_text())
    payload = json.loads((root / "docs/schemas/machine-inventory-unit-response.json").read_text())
    jsonschema.validate(wire, provider)
    jsonschema.validate(json.loads(wire["unit-0"]), payload)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"rack": '"a"'}, provider)
    invalid = document.model_dump(mode="json") | {"unit_name": "not-a-unit"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, payload)


def test_sixty_racks_do_not_share_one_oversized_response_field():
    # This verifies wire representation, not Juju event latency or scale capacity.
    # The complete application bag can grow while every addressed response stays
    # independently bounded and consumers decode only their own field.
    machines, profiles, identities, wire = [], [], [], {}
    for rack in range(60):
        profiles.append(
            {
                "id": f"r{rack}",
                "peers": [
                    {"id": "tor1", "asn": 65001, "chassis_id": f"tor1-r{rack}"},
                ],
            }
        )
        for offset in range(8):
            n = rack * 8 + offset
            machines.append(
                {"system_id": f"m{n}", "hardware_uuid": f"u{n}", "tag_names": [f"rack-r{rack}"]}
            )
            identities.append(
                {
                    "machine_id": f"m{n}",
                    "local_asn": 4200000000 + n,
                    "router_id": f"10.255.{rack}.{offset + 1}",
                }
            )
    for n in range(480):
        document = resolved((machines, profiles, identities), f"u{n}", name=f"bird/{n}")
        assert document.rack == f"r{n // 8}" and document.scope_state == "ready"
        wire.update(response_data(document))
    assert len(wire) == 480
    assert sum(len(value.encode()) for value in wire.values()) > 48 * 1024
    assert max(len(value.encode()) for value in wire.values()) < 48 * 1024
