"""Restrictive updates form a subset of the last accepted authorization.

Generate removals, not arbitrary malformed documents. These properties exercise
actual immutable plan transforms independently of BIRD health or configuration.
"""

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from maas_bird.authority import restrict
from maas_bird.models import Inventory
from maas_bird.resolver import AuthorizationWithdrawn, resolve_rack

from tests.factories import rack_inputs


@settings(max_examples=40)
@given(kept=st.sets(st.integers(0, 3), max_size=4))
def test_restriction_never_adds_authorization_and_is_idempotent(kept):
    inputs = rack_inputs()
    previous = resolve_rack(**inputs)
    prefixes = list(previous.prefixes)
    policies = json.loads(inputs["config"]["routing-domains"])
    documents = []
    for doc in inputs["advertisements"]:
        records = tuple(
            ad.model_copy(update={"ready": prefixes.index(ad.prefix) in kept})
            for ad in doc.advertisements
        )
        documents.append(doc.model_copy(update={"advertisements": records}))
    # Independently narrow one operator allowlist; workload readiness must never
    # override the operator, and the transform must preserve accepted topology.
    policies[0]["export_allowlist"] = [str(prefixes[0])]
    config = inputs["config"] | {"routing-domains": json.dumps(policies)}
    result = restrict(previous, config, inputs["inventory"], documents)
    assert set(result.prefixes) <= set(previous.prefixes)
    assert result.host == previous.host
    assert restrict(result, config, inputs["inventory"], documents) == result
    assert all(prefixes.index(p) in kept for p in result.prefixes)
    assert set(result.domains[0].prefixes) <= {prefixes[0]}


def test_unknown_snapshot_is_not_a_withdrawal_but_empty_is():
    inputs = rack_inputs()
    previous = resolve_rack(**inputs)
    doc = inputs["advertisements"][0]
    unknown = doc.model_copy(update={"advertisements": None})
    assert restrict(previous, inputs["config"], inputs["inventory"], [unknown]) == previous
    empty = doc.model_copy(update={"advertisements": ()})
    changed = restrict(previous, inputs["config"], inputs["inventory"], [empty])
    assert changed.domains[0].prefixes == ()
    assert changed.domains[1] == previous.domains[1]


def test_protected_prefix_narrowing_removes_previously_imported_routes():
    inputs = rack_inputs()
    previous = resolve_rack(**inputs)
    destination = previous.domains[0].imports[0]
    config = inputs["config"] | {"protected-prefixes": str(destination)}
    restricted = restrict(previous, config, inputs["inventory"], [])
    assert destination not in restricted.domains[0].imports
    assert restricted.domains[1] == previous.domains[1]


def test_same_asn_does_not_authorize_a_removed_tor():
    inputs = rack_inputs()
    previous = resolve_rack(**inputs)
    host = inputs["inventory"].hosts[0]
    # Both TORs can legitimately share an ASN. Chassis and peer identity must
    # still distinguish the removed adjacency; an ASN-only check is insufficient.
    first, second = host.peers
    first = first.model_copy(update={"asn": second.asn})
    allowed = host.model_copy(update={"peers": (first,)})
    inventory = Inventory(rack="a", hosts=(allowed,))
    result = restrict(previous, inputs["config"], inventory, [])
    assert all(s.peer_id == first.id for s in result.sessions)
    assert not any(s.peer_id == second.id for s in result.sessions)


def test_explicit_inventory_removal_revokes_host():
    inputs = rack_inputs()
    with pytest.raises(AuthorizationWithdrawn):
        restrict(resolve_rack(**inputs), inputs["config"], Inventory(rack="a", hosts=()), [])


@pytest.mark.parametrize(
    "field,value", [("vlan", 1234), ("peer_link_local", "fe80::1234"), ("ready", False)]
)
def test_changed_attachment_binding_revokes_old_session_before_new_grant(field, value):
    """An unchanged producer ID must not hide a changed physical binding."""
    inputs = rack_inputs()
    previous = resolve_rack(**inputs)
    doc = inputs["attachments"][0]
    card = doc.cards[0]
    attachment = card.attachments[0]
    changed = attachment.model_copy(update={field: value})
    doc = doc.model_copy(
        update={
            "cards": (card.model_copy(update={"attachments": (changed, *card.attachments[1:])}),)
        }
    )
    result = restrict(previous, inputs["config"], inputs["inventory"], [], attachments=[doc])
    assert attachment.id not in {s.id for s in result.sessions}
    assert set(result.prefixes) == set(previous.prefixes)
    assert set(result.sessions) < set(previous.sessions)


def test_removing_static_context_binding_withdraws_origin_before_migration():
    inputs = rack_inputs()
    previous = resolve_rack(**inputs)
    policies = json.loads(inputs["config"]["routing-domains"])
    old = previous.domains[0].contexts[0]
    policies[0]["contexts"] = [c for c in policies[0]["contexts"] if c["id"] != old.id]
    config = inputs["config"] | {"routing-domains": json.dumps(policies)}
    result = restrict(previous, config, inputs["inventory"], [])
    assert not set(old.exports) & set(result.prefixes)
    assert result.domains[1] == previous.domains[1]
    assert result.contexts == tuple(
        c.model_copy(update={"exports": ()}) if c == old else c for c in previous.contexts
    )


@given(order=st.permutations((0, 1)))
def test_duplicate_card_publishers_cannot_override_a_complete_withdrawal(order):
    """Delivery order cannot turn one producer's empty snapshot into an old grant.

    Two complete reports for one physical card are ambiguous. Their shared
    authority can only shrink; a stale duplicate must not defeat revocation.
    """
    inputs = rack_inputs()
    previous = resolve_rack(**inputs)
    original = inputs["attachments"][0]
    card = original.cards[0]
    withdrawn = original.model_copy(
        update={"cards": (card.model_copy(update={"attachments": ()}),)}
    )
    reports = [withdrawn, original]
    result = restrict(
        previous,
        inputs["config"],
        inputs["inventory"],
        [],
        attachments=[reports[index] for index in order],
    )
    assert result.sessions == tuple(s for s in previous.sessions if s.identity != card.identity)
    assert result.prefixes == previous.prefixes
