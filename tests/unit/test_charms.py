from unittest.mock import Mock

from maas_bird import discovery
from maas_bird.interfaces import decode, encode
from maas_bird.models import Attachments, Card, Identity, Inventory
from ops import testing


def test_all_charms_are_principals(charm_context):
    for name in ["bird", "maas-inventory", "dpu-attachments"]:
        ctx = charm_context(name)
        assert not ctx._charm_spec.meta.get("subordinate", False)


def test_bird_disabled_does_not_install_or_start(charm_context, monkeypatch):
    service = Mock()
    monkeypatch.setattr("maas_bird.runtime.BirdService", lambda **kwargs: service)
    ctx = charm_context("bird")
    out = ctx.run(ctx.on.start(), testing.State())
    assert isinstance(out.unit_status, testing.BlockedStatus)
    service.apply.assert_not_called()


def test_bird_requires_inventory(charm_context, monkeypatch):
    monkeypatch.setattr("maas_bird.discovery.host_uuid", lambda: "host-uuid")
    ctx = charm_context("bird")
    out = ctx.run(ctx.on.start(), testing.State(config={"enabled": True}))
    assert out.unit_status == testing.WaitingStatus("Waiting for MAAS inventory relation")


def test_dpu_publishes_unique_card_identity(charm_context, monkeypatch):
    identity = Identity(scheme="devlink-serial", value="card1")
    monkeypatch.setattr("maas_bird.discovery.cards", lambda: {"pf0": identity, "pf1": identity})
    ctx = charm_context("dpu-attachments")
    relation = testing.Relation("attachments")
    out = ctx.run(ctx.on.relation_joined(relation), testing.State(relations={relation}))
    data = out.get_relation(relation.id).local_unit_data
    assert decode(Attachments, data) == Attachments(cards=(Card(identity=identity),))


def test_dpu_failure_keeps_previous_observation(charm_context, monkeypatch):
    def fail():
        raise discovery.DiscoveryError("not available")

    monkeypatch.setattr("maas_bird.discovery.cards", fail)
    ctx = charm_context("dpu-attachments")
    relation = testing.Relation("attachments", local_unit_data={"cards": "[]"})
    out = ctx.run(ctx.on.update_status(), testing.State(relations={relation}))
    assert out.get_relation(relation.id).local_unit_data == {"cards": "[]"}
    assert isinstance(out.unit_status, testing.WaitingStatus)


def test_maas_leader_publishes_and_does_not_leak_secret(charm_context, monkeypatch):
    monkeypatch.setattr("maas_bird.maas.MaasClient.machines", lambda _: [])
    ctx = charm_context("maas-inventory")
    relation = testing.Relation("inventory")
    secret = testing.Secret({"api-key": "consumer:token:secret"})
    out = ctx.run(
        ctx.on.config_changed(),
        testing.State(
            leader=True,
            relations={relation},
            secrets={secret},
            config={"maas-url": "https://maas/MAAS/", "maas-credentials": secret.id},
        ),
    )
    assert out.get_relation(relation.id).local_app_data == encode(Inventory(hosts=()))
    assert "consumer" not in str(out.get_relation(relation.id).local_app_data)


def test_maas_nonleader_does_not_fetch(charm_context, monkeypatch):
    fetch = Mock(side_effect=AssertionError("must not fetch"))
    monkeypatch.setattr("maas_bird.maas.MaasClient.machines", fetch)
    ctx = charm_context("maas-inventory")
    ctx.run(ctx.on.update_status(), testing.State(leader=False))
    fetch.assert_not_called()


def test_maas_missing_secret_is_blocked(charm_context):
    ctx = charm_context("maas-inventory")
    out = ctx.run(ctx.on.start(), testing.State(leader=True))
    assert isinstance(out.unit_status, testing.BlockedStatus)


def test_bird_complete_relations_apply(inputs, charm_context, monkeypatch):
    monkeypatch.setattr("maas_bird.discovery.host_uuid", lambda: "host-uuid")
    monkeypatch.setattr("maas_bird.discovery.links", lambda: inputs["local_links"])
    monkeypatch.setattr(
        "maas_bird.discovery.command",
        lambda _: '[{"addr_info": [{"local":"10.60.11.11","prefixlen":32}]}]',
    )
    service = Mock()
    monkeypatch.setattr("maas_bird.runtime.BirdService", lambda **kwargs: service)
    ctx = charm_context("bird")
    inv = testing.Relation("inventory", remote_app_data=encode(inputs["inventory"]))
    attachments = testing.Relation(
        "attachments",
        remote_units_data={n: encode(doc) for n, doc in enumerate(inputs["attachments"])},
    )
    ads = testing.Relation(
        "advertisements", remote_units_data={0: encode(inputs["advertisements"][0])}
    )
    secret = testing.Secret({"path1": "first-secret", "path2": "second-secret"})
    config = inputs["config"] | {"enabled": True, "bgp-credentials": secret.id}
    out = ctx.run(
        ctx.on.config_changed(),
        testing.State(config=config, secrets={secret}, relations={inv, attachments, ads}),
    )
    assert out.unit_status == testing.ActiveStatus("2 BGP sessions established")
    assert service.apply.call_args.args[1:] == (3100, 2)
    assert out.get_relation(inv.id).local_unit_data["system_uuid"] == '"host-uuid"'


def test_dpu_selects_own_mapping_from_shared_application_config(charm_context, monkeypatch):
    identity = Identity(scheme="devlink-serial", value="card1")
    monkeypatch.setattr("maas_bird.discovery.cards", lambda: {"pf0": identity})
    observe = Mock(return_value=Attachments(cards=(Card(identity=identity),)))
    monkeypatch.setattr("maas_bird.dpu.observe", observe)
    ctx = charm_context("dpu-attachments")
    ctx.run(
        ctx.on.config_changed(),
        testing.State(
            config={"attachments": '[{"serial":"card1","id":"local"},{"serial":"other-card"}]'}
        ),
    )
    observe.assert_called_once_with([{"serial": "card1", "id": "local"}])


def test_removing_a_trusted_relation_stops_owned_routing(charm_context, monkeypatch):
    service = Mock()
    monkeypatch.setattr("maas_bird.runtime.BirdService", lambda **kwargs: service)
    ctx = charm_context("bird")
    relation = testing.Relation("inventory")
    stored = testing.StoredState(
        name="_state",
        owner_path="BirdCharm",
        content={"dependencies": [f"relation:{relation.id}"]},
    )
    out = ctx.run(
        ctx.on.relation_broken(relation),
        testing.State(config={"enabled": True}, relations={relation}, stored_states={stored}),
    )
    service.stop.assert_called_once()
    service.apply.assert_not_called()
    assert out.unit_status == testing.BlockedStatus("A trusted routing producer was removed")


def test_service_address_must_be_an_actual_loopback_host_route(inputs, charm_context, monkeypatch):
    monkeypatch.setattr("maas_bird.discovery.host_uuid", lambda: "host-uuid")
    monkeypatch.setattr("maas_bird.discovery.links", lambda: inputs["local_links"])
    monkeypatch.setattr(
        "maas_bird.discovery.command",
        lambda _: '[{"addr_info": [{"local":"10.60.11.11","prefixlen":24}]}]',
    )
    service = Mock()
    monkeypatch.setattr("maas_bird.runtime.BirdService", lambda **kwargs: service)
    ctx = charm_context("bird")
    inv = testing.Relation("inventory", remote_app_data=encode(inputs["inventory"]))
    attachments = testing.Relation(
        "attachments",
        remote_units_data={n: encode(doc) for n, doc in enumerate(inputs["attachments"])},
    )
    ads = testing.Relation(
        "advertisements", remote_units_data={0: encode(inputs["advertisements"][0])}
    )
    out = ctx.run(
        ctx.on.config_changed(),
        testing.State(
            config=inputs["config"] | {"enabled": True}, relations={inv, attachments, ads}
        ),
    )
    service.apply.assert_not_called()
    assert out.unit_status == testing.WaitingStatus("Advertised service /32 is not present on lo")
