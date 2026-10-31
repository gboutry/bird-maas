"""Exercise the real BGP/BFD join with strict native-output doubles.

These tests deliberately do not use rack_environment: that fixture replaces
health() with an always-healthy model for transaction tests. Neither BIRD nor a
kernel routing table runs here; namespace tests exercise those boundaries.
"""

import json
import subprocess

import pytest
from maas_bird import rack_runtime
from maas_bird.rack_runtime import RackService
from maas_bird.render import protocol_name
from maas_bird.resolver import resolve_rack

from tests.factories import rack_inputs


def protocol(name, interface, *, scoped_neighbor=False):
    neighbor = "fe80::1" + ("%" + interface if scoped_neighbor else "")
    explicit = "" if scoped_neighbor else "  Interface: " + interface + "\n"
    return f"""{name} BGP --- up 12:00:00 Established
  Neighbor address: {neighbor}
{explicit}  Neighbor capabilities
    Extended next hop
      IPv6 nexthop: ipv4
  Session: external
  Channel ipv4
    State: UP
"""


@pytest.fixture
def health_probe(tmp_path, monkeypatch):
    original = resolve_rack(**rack_inputs())
    plan = original.model_copy(update={"domains": (original.domains[0],)})
    domain = plan.domains[0]
    service = RackService(tmp_path, owner="test:bird/0")
    responses = {}
    for command in (("show", "protocols", "all"), ("show", "bfd", "sessions")):
        responses[command] = subprocess.CompletedProcess(command, 0, "", "")

    def control(*args, check=True):
        assert check is False
        assert args in responses, "Health must use only its two native read commands"
        return responses[args]

    def execute(args, check=True):
        assert args[:8] == ["ip", "-4", "-j", "-N", "route", "show", "table", args[-1]]
        contexts = [c for c in domain.contexts if c.table == int(args[-1])]
        assert len(contexts) == 1
        context = contexts[0]
        interface = next(s.interface for s in domain.sessions if s.numa_node == context.numa_node)
        routes = [{"dst": str(prefix), "dev": interface} for prefix in domain.imports]
        return subprocess.CompletedProcess(args, 0, json.dumps(routes), "")

    monkeypatch.setattr(service, "_control", control)
    monkeypatch.setattr(rack_runtime, "run", execute)
    return service, plan, responses


def outputs(plan, responses, *, scoped_neighbor=False, down=None, absent=None):
    sessions = plan.domains[0].sessions
    responses[("show", "protocols", "all")].stdout = "".join(
        protocol(
            protocol_name(plan.domains[0].domain, session.id),
            session.interface,
            scoped_neighbor=scoped_neighbor,
        )
        for session in sessions
    )
    responses[("show", "bfd", "sessions")].stdout = "\n".join(
        f"fe80::1 {s.interface} {'Down' if s.id == down else 'Up'} 12:00:00 0.300 0.900"
        for s in sessions
        if s.id != absent
    )


@pytest.mark.parametrize("scoped_neighbor", [False, True])
def test_same_link_local_peer_is_joined_to_its_own_interface(health_probe, scoped_neighbor):
    service, plan, responses = health_probe
    first, second = plan.sessions
    outputs(plan, responses, scoped_neighbor=scoped_neighbor, down=first.id)
    result = service.health(plan)
    assert result == {
        "configured": 2,
        "established": 1,
        "missing_routes": 0,
        "fallback_routes": 0,
        "up_sessions": [second.id],
    }


def test_unrelated_up_row_cannot_replace_missing_local_bfd(health_probe):
    service, plan, responses = health_probe
    first, second = plan.sessions
    outputs(plan, responses, absent=first.id)
    responses[("show", "bfd", "sessions")].stdout += "\nfe80::1 unrelated0 Up 0.300"
    result = service.health(plan)
    assert result["up_sessions"] == [second.id]
    assert result["established"] == 1


@pytest.mark.parametrize("failed", [("show", "protocols", "all"), ("show", "bfd", "sessions")])
def test_failed_cli_never_authorizes_healthy_looking_output(health_probe, failed):
    service, plan, responses = health_probe
    outputs(plan, responses)
    responses[failed].returncode = 1
    result = service.health(plan)
    assert result["configured"] == 2
    assert result["up_sessions"] == []
    assert result["established"] == 0


@pytest.mark.parametrize(
    ("old", "new"),
    [("Extended next hop", "Route refresh"), ("State: UP", "State: DOWN")],
)
def test_missing_capability_or_down_channel_is_not_counted_as_established(health_probe, old, new):
    service, plan, responses = health_probe
    outputs(plan, responses)
    responses[("show", "protocols", "all")].stdout = responses[
        ("show", "protocols", "all")
    ].stdout.replace(old, new)
    result = service.health(plan)
    assert result["up_sessions"] == []
    assert result["established"] == 0
