"""Explicit node origins preserve native admission and cannot inherit stale authority."""

import json
from unittest.mock import Mock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from maas_bird import authority, discovery
from maas_bird.interfaces import encode
from maas_bird.policy_objects import PolicyError
from maas_bird.rack_runtime import MigrationRequired
from maas_bird.resolver import resolve_rack
from ops import testing

from tests.factories import rack_inputs, ten_vf_inputs


def node_inputs():
    inputs = ten_vf_inputs()
    inputs["config"]["advertisement-mode"] = "node-addresses"
    inputs["advertisements"] = []
    return inputs


def test_node_origins_need_no_manufactured_workload_snapshot():
    inputs = node_inputs()
    plan = resolve_rack(**inputs)
    assert plan.advertisement_mode == "node-addresses"
    assert len(plan.prefixes) == 5
    assert all(c.exports == c.sources for c in plan.contexts)
    inputs["config"]["advertisement-mode"] = "workload"
    assert resolve_rack(**inputs).prefixes == ()


def test_unknown_mode_cannot_silently_enable_node_origins():
    inputs = node_inputs()
    inputs["config"]["advertisement-mode"] = "always-ready"
    with pytest.raises(ValueError, match="advertisement-mode"):
        resolve_rack(**inputs)


@settings(max_examples=20)
@given(ready=st.booleans(), empty=st.booleans())
def test_node_authority_is_independent_of_workload_producer_health(ready, empty):
    """A workload publisher can neither grant nor revoke a node-owned address.

    Vary complete empty, ready and not-ready snapshots from otherwise valid
    producers. Node mode is operator intent, not a way to fabricate their health.
    """
    inputs = node_inputs()
    expected = resolve_rack(**inputs)
    docs = rack_inputs()["advertisements"]
    inputs["advertisements"] = [
        d.model_copy(
            update={
                "advertisements": ()
                if empty
                else tuple(a.model_copy(update={"ready": ready}) for a in d.advertisements or ())
            }
        )
        for d in docs
    ]
    assert resolve_rack(**inputs) == expected
    floor = authority.restrict(expected, inputs["config"], inputs["inventory"], docs)
    assert floor == expected


@pytest.mark.parametrize(
    "before,after", [("node-addresses", "workload"), ("workload", "node-addresses")]
)
def test_changing_origin_authority_first_commits_zero_exports(before, after):
    inputs = ten_vf_inputs()
    inputs["config"]["advertisement-mode"] = before
    previous = resolve_rack(**inputs)
    inputs["config"]["advertisement-mode"] = after
    floor = authority.restrict(
        previous, inputs["config"], inputs["inventory"], inputs["advertisements"]
    )
    assert floor.advertisement_mode == after
    assert floor.prefixes == ()
    assert all(not c.exports for c in floor.contexts)
    assert (
        authority.restrict(floor, inputs["config"], inputs["inventory"], inputs["advertisements"])
        == floor
    )


@pytest.mark.parametrize("missing_address", [False, True])
def test_node_mode_hook_still_checks_native_addresses_before_applying(
    charm_context, monkeypatch, missing_address
):
    inputs = node_inputs()
    monkeypatch.setattr(discovery, "host_uuid", lambda: "host-uuid")
    monkeypatch.setattr(discovery, "links", lambda: inputs["local_links"])
    sources = [
        str(c)
        for d in json.loads(inputs["config"]["routing-domains"])
        for ctx in d["contexts"]
        for c in ctx["sources"]
    ]
    if missing_address:
        sources.pop()
    address_response = json.dumps(
        [{"addr_info": [{"local": p.split("/")[0], "prefixlen": 32} for p in sources]}]
    )
    monkeypatch.setattr(discovery, "command", lambda _: address_response)
    service = Mock()
    service.state.return_value = None
    service.observations.return_value = {}
    service.health.return_value = {
        "established": 10,
        "configured": 10,
        "missing_routes": 0,
        "fallback_routes": 0,
    }
    monkeypatch.setattr("maas_bird.rack_runtime.RackService", lambda **_: service)
    secret = testing.Secret({s.id: "test-only" for s in resolve_rack(**inputs).sessions})
    relations = {
        testing.Relation("inventory", remote_app_data=encode(inputs["inventory"])),
        testing.Relation(
            "attachments",
            remote_units_data={n: encode(d) for n, d in enumerate(inputs["attachments"])},
        ),
    }
    ctx = charm_context("bird")
    out = ctx.run(
        ctx.on.config_changed(),
        testing.State(
            config=inputs["config"] | {"enabled": True, "bgp-credentials": secret.id},
            secrets={secret},
            relations=relations,
        ),
    )
    if missing_address:
        service.apply_plan.assert_not_called()
        assert isinstance(out.unit_status, testing.WaitingStatus)
    else:
        service.apply_plan.assert_called_once()
        assert service.apply_plan.call_args.args[0].advertisement_mode == "node-addresses"
        assert out.unit_status == testing.ActiveStatus(
            "Rack a: 10/10 BGP/BFD Up; node addresses, service health not checked"
        )


def test_node_mode_cannot_activate_without_the_existing_source_guard(rack_environment):
    inputs, _, passwords, service, host = rack_environment
    inputs["config"]["advertisement-mode"] = "node-addresses"
    inputs["advertisements"] = []
    plan = resolve_rack(**inputs)
    host.kernel.routes = [r for r in host.kernel.routes if r["table"] != plan.contexts[0].table]
    with pytest.raises(MigrationRequired, match="unreachable-default guard") as error:
        service.apply_plan(plan, passwords)
    assert isinstance(error.value.__cause__, PolicyError)
    assert service.state() is None
    assert not host.active
