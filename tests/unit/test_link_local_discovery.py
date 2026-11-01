"""Unnumbered adjacency discovery must not configure neighboring machines."""

import re

import pytest
from maas_bird.render import render_rack
from maas_bird.resolver import resolve_rack

from tests.factories import rack_inputs


def render(plan):
    return render_rack(plan, {session.id: "test-password" for session in plan.sessions})


def test_discovery_uses_only_exact_approved_transit_interfaces():
    plan = resolve_rack(**rack_inputs())
    text = render(plan)
    discovery = text.split("protocol radv fabric_discovery {\n", 1)[1].split("protocol bfd ", 1)[0]

    assert text.count("protocol radv ") == 1
    assert set(re.findall(r'interface "([^\"]+)"', discovery)) == {
        session.interface for session in plan.sessions
    }
    assert discovery.count("default lifetime 0;") == len(plan.sessions)
    assert discovery.count("prefix ::/0 { skip yes; };") == len(plan.sessions)
    assert discovery.count("current hop limit 0; link mtu 0;") == len(plan.sessions)
    assert discovery.count("reachable time 0; retrans timer 0;") == len(plan.sessions)
    assert discovery.count("managed no; other config no;") == len(plan.sessions)
    assert discovery.count("rdnss local yes; dnssl local yes;") == len(plan.sessions)
    assert "propagate routes no;" in discovery
    assert "export all" not in discovery
    assert 'interface "*"' not in discovery


def test_complete_session_withdrawal_does_not_enable_wildcard_discovery():
    plan = resolve_rack(**rack_inputs())
    withdrawn = plan.model_copy(
        update={
            "domains": tuple(domain.model_copy(update={"sessions": ()}) for domain in plan.domains)
        }
    )

    assert "protocol radv " not in render(withdrawn)


@pytest.mark.parametrize("interface", ["eth*", "eth?", "bad\nname", 'eth"0'])
def test_discovery_cannot_expand_an_untrusted_interface_pattern(interface):
    plan = resolve_rack(**rack_inputs())
    first = plan.domains[0]
    invalid = first.model_copy(
        update={"sessions": (first.sessions[0].model_copy(update={"interface": interface}),)}
    )
    plan = plan.model_copy(update={"domains": (invalid, *plan.domains[1:])})

    with pytest.raises(ValueError):
        render(plan)
