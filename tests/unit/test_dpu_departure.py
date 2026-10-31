"""Exercise the relation-set race observed during live DPU cleanup."""

import ops
import pytest
import scenario as testing
from maas_bird.interfaces import decode, publish
from maas_bird.models import Attachments, Card, Identity
from scenario.errors import UncaughtCharmError


def test_departed_relation_set_race_does_not_block_other_consumers(charm_context, monkeypatch):
    identity = Identity(scheme="devlink-serial", value="card1")
    monkeypatch.setattr("maas_bird.discovery.cards", lambda: {"pf0": identity})
    departing = testing.Relation("attachments", remote_units_data={0: {}})
    remaining = testing.Relation("attachments", remote_units_data={0: {}})
    attempted = []

    def relation_set_race(relation, unit, document):
        attempted.append(relation.id)
        if relation.id == departing.id:
            raise ops.RelationNotFoundError("relation removed before relation-set")
        publish(relation, unit, document)

    # Patch before loading the charm so its imported publication helper sees
    # the same error that Juju's relation-set returned on the live unit.
    monkeypatch.setattr("maas_bird.interfaces.publish", relation_set_race)
    ctx = charm_context("dpu-attachments")
    out = ctx.run(
        ctx.on.relation_departed(departing, departing_unit=0),
        testing.State(relations={departing, remaining}),
    )
    assert set(attempted) == {departing.id, remaining.id}
    assert decode(Attachments, out.get_relation(remaining.id).local_unit_data) == Attachments(
        cards=(Card(identity=identity, attachments=()),)
    )
    assert out.unit_status == testing.WaitingStatus(
        "Card identity published; attachments not ready"
    )


@pytest.mark.parametrize("failure", [ops.RelationNotFoundError, ops.ModelError])
def test_non_departure_publication_errors_remain_visible(charm_context, monkeypatch, failure):
    identity = Identity(scheme="devlink-serial", value="card1")
    monkeypatch.setattr("maas_bird.discovery.cards", lambda: {"pf0": identity})

    def fail_publication(*args):
        raise failure("publication failed")

    monkeypatch.setattr("maas_bird.interfaces.publish", fail_publication)
    ctx = charm_context("dpu-attachments")
    relation = testing.Relation("attachments")
    with pytest.raises(UncaughtCharmError) as error:
        ctx.run(ctx.on.update_status(), testing.State(relations={relation}))
    assert isinstance(error.value.__cause__, failure)


def test_departure_does_not_hide_an_unrelated_missing_relation(charm_context, monkeypatch):
    monkeypatch.setattr("maas_bird.discovery.cards", lambda: {})
    departing = testing.Relation("attachments", remote_units_data={0: {}})
    unrelated = testing.Relation("attachments", remote_units_data={0: {}})

    def fail_unrelated(relation, unit, document):
        if relation.id == unrelated.id:
            raise ops.RelationNotFoundError("unrelated relation unavailable")
        publish(relation, unit, document)

    monkeypatch.setattr("maas_bird.interfaces.publish", fail_unrelated)
    ctx = charm_context("dpu-attachments")
    with pytest.raises(UncaughtCharmError) as error:
        ctx.run(
            ctx.on.relation_departed(departing, departing_unit=0),
            testing.State(relations={departing, unrelated}),
        )
    assert isinstance(error.value.__cause__, ops.RelationNotFoundError)
