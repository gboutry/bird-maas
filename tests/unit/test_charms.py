import json
from unittest.mock import Mock

from maas_bird import discovery
from maas_bird.interfaces import decode, encode
from maas_bird.models import Attachments, Card, Identity, Inventory, RackRequest
from maas_bird.resolver import resolve_rack
from ops import testing

from tests.factories import rack_inputs


def test_all_charms_are_principals(charm_context):
    for name in ["bird", "maas-inventory", "dpu-attachments"]:
        ctx = charm_context(name)
        assert not ctx._charm_spec.meta.get("subordinate", False)


def test_bird_disabled_does_not_install_or_start(charm_context, monkeypatch):
    service = Mock()
    service.state.return_value = None
    service.observations.return_value = {}
    service.health.return_value = {
        "configured": 2,
        "established": 2,
        "missing_routes": 0,
        "fallback_routes": 0,
    }
    monkeypatch.setattr("maas_bird.rack_runtime.RackService", lambda **kwargs: service)
    ctx = charm_context("bird")
    out = ctx.run(ctx.on.start(), testing.State())
    assert isinstance(out.unit_status, testing.BlockedStatus)
    service.apply.assert_not_called()


def test_bird_requires_inventory(charm_context, monkeypatch):
    monkeypatch.setattr("maas_bird.discovery.host_uuid", lambda: "host-uuid")
    ctx = charm_context("bird")
    out = ctx.run(ctx.on.start(), testing.State(config={"enabled": True, "rack": "a"}))
    assert out.unit_status == testing.WaitingStatus("Waiting for one rack inventory relation")


def test_dpu_publishes_unique_card_identity(charm_context, monkeypatch):
    identity = Identity(scheme="devlink-serial", value="card1")
    monkeypatch.setattr("maas_bird.discovery.cards", lambda: {"pf0": identity, "pf1": identity})
    ctx = charm_context("dpu-attachments")
    relation = testing.Relation("attachments")
    out = ctx.run(
        ctx.on.relation_joined(relation), testing.State(config={"rack": "a"}, relations={relation})
    )
    data = out.get_relation(relation.id).local_unit_data
    assert decode(Attachments, data) == Attachments(
        rack="a", cards=(Card(identity=identity, attachments=()),)
    )


def test_dpu_failure_keeps_previous_observation(charm_context, monkeypatch):
    def fail():
        raise discovery.DiscoveryError("not available")

    monkeypatch.setattr("maas_bird.discovery.cards", fail)
    ctx = charm_context("dpu-attachments")
    relation = testing.Relation("attachments", local_unit_data={"rack": '"a"', "cards": "[]"})
    out = ctx.run(
        ctx.on.update_status(), testing.State(config={"rack": "a"}, relations={relation})
    )
    assert out.get_relation(relation.id).local_unit_data == {"rack": '"a"', "cards": "[]"}
    assert isinstance(out.unit_status, testing.WaitingStatus)


def test_maas_leader_publishes_and_does_not_leak_secret(charm_context, monkeypatch):
    monkeypatch.setattr("maas_bird.maas.MaasClient.machines", lambda _: [])
    ctx = charm_context("maas-inventory")
    relation = testing.Relation("inventory", remote_app_data=encode(RackRequest(rack="a")))
    secret = testing.Secret({"api-key": "consumer:token:secret"})
    out = ctx.run(
        ctx.on.config_changed(),
        testing.State(
            leader=True,
            relations={relation},
            secrets={secret},
            config={
                "maas-url": "https://maas/MAAS/",
                "maas-credentials": secret.id,
                "rack-profiles": '[{"id":"a","peers":[]}]',
            },
        ),
    )
    assert out.get_relation(relation.id).local_app_data == encode(Inventory(rack="a", hosts=()))
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
    inputs = rack_inputs(inputs)
    monkeypatch.setattr("maas_bird.discovery.host_uuid", lambda: "host-uuid")
    monkeypatch.setattr("maas_bird.discovery.links", lambda: inputs["local_links"])
    monkeypatch.setattr(
        "maas_bird.discovery.command",
        lambda _: (
            '[{"addr_info": [{"local":"10.60.11.11","prefixlen":32},'
            '{"local":"10.60.11.12","prefixlen":32}]}]'
        ),
    )
    service = Mock()
    service.state.return_value = None
    service.observations.return_value = {}
    service.health.return_value = {
        "configured": 2,
        "established": 2,
        "missing_routes": 0,
        "fallback_routes": 0,
    }
    monkeypatch.setattr("maas_bird.rack_runtime.RackService", lambda **kwargs: service)
    ctx = charm_context("bird")
    inv = testing.Relation("inventory", remote_app_data=encode(inputs["inventory"]))
    attachments = testing.Relation(
        "attachments",
        remote_units_data={n: encode(doc) for n, doc in enumerate(inputs["attachments"])},
    )
    ads = testing.Relation(
        "advertisements",
        remote_units_data={n: encode(doc) for n, doc in enumerate(inputs["advertisements"])},
    )
    secret = testing.Secret({"path1": "first-secret", "path2": "second-secret"})
    config = inputs["config"] | {"enabled": True, "bgp-credentials": secret.id}
    out = ctx.run(
        ctx.on.config_changed(),
        testing.State(config=config, secrets={secret}, relations={inv, attachments, ads}),
    )
    assert out.unit_status == testing.ActiveStatus("Rack a: 2/2 BGP/BFD Up")
    assert len(service.apply_plan.call_args.args[0].sessions) == 2
    assert len(service.apply_plan.call_args.args[0].contexts) == 2
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
            config={
                "rack": "a",
                "attachments": json.dumps(
                    [
                        {
                            "serial": "card1",
                            "id": "local",
                            "host_function": {"controller": 0, "pf": 0, "vf": 63},
                        },
                        {"serial": "other-card"},
                    ]
                ),
            }
        ),
    )
    observe.assert_called_once_with(
        [{"serial": "card1", "id": "local", "host_function": {"controller": 0, "pf": 0, "vf": 63}}]
    )


def test_removing_a_trusted_relation_stops_owned_routing(charm_context, monkeypatch):
    service = Mock()
    service.state.return_value = None
    service.observations.return_value = {}
    service.health.return_value = {
        "configured": 2,
        "established": 2,
        "missing_routes": 0,
        "fallback_routes": 0,
    }
    monkeypatch.setattr("maas_bird.rack_runtime.RackService", lambda **kwargs: service)
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
    assert out.unit_status == testing.BlockedStatus(
        "The authoritative inventory relation was removed"
    )


def test_service_address_must_be_an_actual_loopback_host_route(inputs, charm_context, monkeypatch):
    inputs = rack_inputs(inputs)
    monkeypatch.setattr("maas_bird.discovery.host_uuid", lambda: "host-uuid")
    monkeypatch.setattr("maas_bird.discovery.links", lambda: inputs["local_links"])
    monkeypatch.setattr(
        "maas_bird.discovery.command",
        lambda _: '[{"addr_info": [{"local":"10.60.11.11","prefixlen":24}]}]',
    )
    service = Mock()
    service.state.return_value = None
    service.observations.return_value = {}
    service.health.return_value = {
        "configured": 2,
        "established": 2,
        "missing_routes": 0,
        "fallback_routes": 0,
    }
    monkeypatch.setattr("maas_bird.rack_runtime.RackService", lambda **kwargs: service)
    ctx = charm_context("bird")
    inv = testing.Relation("inventory", remote_app_data=encode(inputs["inventory"]))
    attachments = testing.Relation(
        "attachments",
        remote_units_data={n: encode(doc) for n, doc in enumerate(inputs["attachments"])},
    )
    ads = testing.Relation(
        "advertisements",
        remote_units_data={n: encode(doc) for n, doc in enumerate(inputs["advertisements"])},
    )
    secret = testing.Secret({"path1": "one", "path2": "two"})
    out = ctx.run(
        ctx.on.config_changed(),
        testing.State(
            config=inputs["config"] | {"enabled": True, "bgp-credentials": secret.id},
            secrets={secret},
            relations={inv, attachments, ads},
        ),
    )
    service.apply.assert_not_called()
    assert out.unit_status == testing.WaitingStatus(
        "Provision every declared source VIP as a /32 on lo"
    )


def test_bird_numa_context_event_uses_policy_runtime(numa_inputs, charm_context, monkeypatch):
    numa_inputs = rack_inputs(numa_inputs)
    monkeypatch.setattr("maas_bird.discovery.host_uuid", lambda: "host-uuid")
    monkeypatch.setattr("maas_bird.discovery.links", lambda: numa_inputs["local_links"])
    monkeypatch.setattr(
        "maas_bird.discovery.command",
        lambda _: (
            '[{"addr_info":[{"local":"10.60.11.11","prefixlen":32},'
            '{"local":"10.60.11.12","prefixlen":32}]}]'
        ),
    )
    service = Mock()
    service.state.return_value = None
    service.observations.return_value = {}
    service.health.return_value = {
        "configured": 2,
        "established": 2,
        "missing_routes": 0,
        "fallback_routes": 0,
    }
    monkeypatch.setattr("maas_bird.rack_runtime.RackService", lambda **kwargs: service)
    ctx = charm_context("bird")
    inv = testing.Relation("inventory", remote_app_data=encode(numa_inputs["inventory"]))
    attachments = testing.Relation(
        "attachments",
        remote_units_data={n: encode(doc) for n, doc in enumerate(numa_inputs["attachments"])},
    )
    ads = testing.Relation(
        "advertisements",
        remote_units_data={n: encode(doc) for n, doc in enumerate(numa_inputs["advertisements"])},
    )
    secret = testing.Secret({"path1": "first-secret", "path2": "second-secret"})
    config = numa_inputs["config"] | {"enabled": True, "bgp-credentials": secret.id}
    out = ctx.run(
        ctx.on.config_changed(),
        testing.State(config=config, secrets={secret}, relations={inv, attachments, ads}),
    )
    assert out.unit_status == testing.ActiveStatus("Rack a: 2/2 BGP/BFD Up")
    service.apply.assert_not_called()
    assert [c.table for c in service.apply_plan.call_args.args[0].contexts] == [3100, 3101]


def test_bird_multiple_domains_use_one_host_runtime(domain_inputs, charm_context, monkeypatch):
    domain_inputs = rack_inputs(domain_inputs)
    monkeypatch.setattr("maas_bird.discovery.host_uuid", lambda: "host-uuid")
    monkeypatch.setattr("maas_bird.discovery.links", lambda: domain_inputs["local_links"])
    addresses = [
        str(a.prefix.network_address)
        for doc in domain_inputs["advertisements"]
        for a in doc.advertisements
    ]

    monkeypatch.setattr(
        "maas_bird.discovery.command",
        lambda _: json.dumps(
            [{"addr_info": [{"local": address, "prefixlen": 32} for address in addresses]}]
        ),
    )
    service = Mock()
    service.state.return_value = None
    service.observations.return_value = {}
    service.health.return_value = {
        "configured": 4,
        "established": 4,
        "missing_routes": 0,
        "fallback_routes": 0,
    }
    monkeypatch.setattr("maas_bird.rack_runtime.RackService", lambda **kwargs: service)
    ctx = charm_context("bird")
    inv = testing.Relation("inventory", remote_app_data=encode(domain_inputs["inventory"]))
    attachments = testing.Relation(
        "attachments",
        remote_units_data={n: encode(doc) for n, doc in enumerate(domain_inputs["attachments"])},
    )
    ads = testing.Relation(
        "advertisements",
        remote_units_data={
            n: encode(doc) for n, doc in enumerate(domain_inputs["advertisements"])
        },
    )
    secret = testing.Secret(
        {"path1": "one", "path2": "two", "replica-path1": "three", "replica-path2": "four"}
    )
    config = domain_inputs["config"] | {"enabled": True, "bgp-credentials": secret.id}
    out = ctx.run(
        ctx.on.config_changed(),
        testing.State(config=config, secrets={secret}, relations={inv, attachments, ads}),
    )
    assert out.unit_status == testing.ActiveStatus("Rack a: 4/4 BGP/BFD Up")
    service.apply.assert_not_called()
    service.apply_plan.assert_called_once()
    plan = service.apply_plan.call_args.args[0]
    assert [domain.domain for domain in plan.domains] == ["storage-public", "storage-replica"]
    assert len(plan.contexts) == 4


def test_discovery_outage_cannot_block_known_workload_withdrawal(charm_context, monkeypatch):
    inputs = rack_inputs()
    previous = resolve_rack(**inputs)
    monkeypatch.setattr(discovery, "host_uuid", lambda: "host-uuid")

    def fail_links():
        raise discovery.DiscoveryError("unavailable")

    monkeypatch.setattr(discovery, "links", fail_links)
    service = Mock()
    service.state.return_value = previous
    service.observations.return_value = {}
    service.credentials_changed.return_value = False
    monkeypatch.setattr("maas_bird.rack_runtime.RackService", lambda **kwargs: service)
    secret = testing.Secret({s.id: "test-only" for s in previous.sessions})
    inv = testing.Relation("inventory", remote_app_data=encode(inputs["inventory"]))
    doc = inputs["advertisements"][0].model_copy(update={"advertisements": ()})
    ads = testing.Relation("advertisements", remote_units_data={0: encode(doc)})
    ctx = charm_context("bird")
    out = ctx.run(
        ctx.on.relation_changed(ads, remote_unit=0),
        testing.State(
            config=inputs["config"] | {"enabled": True, "bgp-credentials": secret.id},
            secrets={secret},
            relations={inv, ads},
        ),
    )
    assert isinstance(out.unit_status, testing.WaitingStatus)
    service.apply_plan.assert_called_once()
    assert service.apply_plan.call_args.kwargs["restrictive"] is True
    restricted = service.apply_plan.call_args.args[0]
    assert restricted.domains[0].prefixes == ()
    assert restricted.domains[1] == previous.domains[1]


def test_disabled_bird_requests_real_inventory_without_activation(charm_context, monkeypatch):
    monkeypatch.setattr(discovery, "host_uuid", lambda: "observed-host-uuid")
    probe = Mock(side_effect=AssertionError("Disabled hooks must not probe attachments"))
    monkeypatch.setattr(discovery, "links", probe)
    service = Mock()
    monkeypatch.setattr("maas_bird.rack_runtime.RackService", lambda **kwargs: service)
    # Juju owns its address fields. Identity publication must preserve them and
    # add only the charm-owned UUID, rather than replace the complete unit bag.
    managed = {
        "private-address": "192.0.2.0",
        "ingress-address": "192.0.2.0",
        "egress-subnets": "192.0.2.0",
    }
    relation = testing.Relation("inventory", local_unit_data=managed.copy())
    ctx = charm_context("bird")
    out = ctx.run(
        ctx.on.relation_joined(relation, remote_unit=0),
        testing.State(
            leader=True,
            config={"rack": "pc8a-rb3", "enabled": False},
            relations={relation},
        ),
    )
    observed = out.get_relation(relation.id)
    assert observed.local_app_data == {"rack": '"pc8a-rb3"'}
    assert observed.local_unit_data["system_uuid"] == '"observed-host-uuid"'
    assert {k: v for k, v in observed.local_unit_data.items() if k != "system_uuid"} == managed
    assert isinstance(out.unit_status, testing.BlockedStatus)
    service.apply_plan.assert_not_called()
    probe.assert_not_called()
