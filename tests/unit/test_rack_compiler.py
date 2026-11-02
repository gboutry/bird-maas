"""Properties of the supported rack compiler, independent of prototype oracles."""

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from maas_bird.render import render_rack
from maas_bird.resolver import InvalidIntent, resolve_rack

from tests.factories import ovs_inputs, rack_inputs, ten_vf_inputs


@pytest.mark.parametrize(
    "inputs_factory",
    [rack_inputs, ten_vf_inputs, ovs_inputs],
    ids=["storage-domains", "ten-vfs", "ovs-underlay"],
)
def test_every_bgp_session_has_one_second_initial_connect_delay(inputs_factory):
    """Retry timing does not override BIRD's separate startup connection delay."""
    plan = resolve_rack(**inputs_factory())
    assert plan.sessions
    config = render_rack(plan, {session.id: "example" for session in plan.sessions})

    assert config.count("protocol bgp ") == len(plan.sessions)
    assert config.count("connect delay time 1;") == len(plan.sessions)
    assert config.count("connect retry time 2;") == len(plan.sessions)


@settings(max_examples=24)
@given(order=st.permutations((0, 1)), reverse_links=st.booleans(), reverse_docs=st.booleans())
def test_reordering_observations_cannot_restart_sessions_or_change_policy(
    order, reverse_links, reverse_docs
):
    """Delivery order is not intent: names and complete generated policy stay stable.

    A changed protocol name can reset BGP during a harmless relation refresh.
    Compare the whole rendered configuration, including literal passwords, so a
    broad regex rename or normalization cannot accidentally rewrite a credential.
    """
    inputs = rack_inputs()
    expected = resolve_rack(**inputs)
    keys = {s.id: 'literal_uplink_0 " quote' for s in expected.sessions}
    policies = json.loads(inputs["config"]["routing-domains"])
    changed = inputs | {
        "config": inputs["config"] | {"routing-domains": json.dumps([policies[i] for i in order])},
        "local_links": list(reversed(inputs["local_links"]))
        if reverse_links
        else inputs["local_links"],
        "advertisements": list(reversed(inputs["advertisements"]))
        if reverse_docs
        else inputs["advertisements"],
    }
    assert resolve_rack(**changed) == expected
    assert render_rack(resolve_rack(**changed), keys) == render_rack(expected, keys)


@pytest.mark.parametrize(
    "fault", ["implicit-host", "shared-vip", "protected-source", "overlapping-export", "rule-slot"]
)
def test_invalid_rack_allocations_never_compile(fault):
    inputs = rack_inputs()
    policies = json.loads(inputs["config"]["routing-domains"])
    first = policies[0]["contexts"][0]
    if fault == "implicit-host":
        first.pop("machine_id")
    elif fault == "shared-vip":
        other = first | {"id": "another-host", "machine_id": "other-machine"}
        policies[0]["contexts"].append(other)
    elif fault == "protected-source":
        inputs["config"]["protected-prefixes"] = first["sources"][0]
    elif fault == "overlapping-export":
        policies[1]["export_allowlist"] = policies[0]["export_allowlist"]
    else:
        policies[1]["contexts"][0]["rule_priority"] = first["rule_priority"]
    inputs["config"]["routing-domains"] = json.dumps(policies)
    with pytest.raises(InvalidIntent):
        resolve_rack(**inputs)
