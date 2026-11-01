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
        "bfd_timer_issues": [f"{second.interface}: BFD TX 300ms, requested 50ms"],
        "bfd_timer_mismatches": [f"{second.interface}: BFD TX 300ms, requested 50ms"],
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


def test_effective_100ms_interval_matches_requested_tx_without_inferring_peer_multiplier(
    health_probe,
):
    service, plan, responses = health_probe
    domain = plan.domains[0].model_copy(update={"bfd_tx": 100, "bfd_rx": 100})
    plan = plan.model_copy(update={"domains": (domain,)})
    outputs(plan, responses)
    # Timeout includes the peer's multiplier. A different timeout alone cannot
    # prove the local multiplier/RX configuration is wrong from this CLI table.
    responses[("show", "bfd", "sessions")].stdout = responses[
        ("show", "bfd", "sessions")
    ].stdout.replace("0.300 0.900", "0.100 0.500")
    result = service.health(plan)
    assert result["established"] == 2
    assert result["bfd_timer_issues"] == []
    assert result["bfd_timer_mismatches"] == []


def test_up_sessions_with_slower_effective_timers_remain_usable_but_report_mismatch(health_probe):
    service, plan, responses = health_probe
    domain = plan.domains[0].model_copy(update={"bfd_tx": 100, "bfd_rx": 100})
    plan = plan.model_copy(update={"domains": (domain,)})
    outputs(plan, responses)
    result = service.health(plan)
    assert result["established"] == 2
    assert result["up_sessions"] == [s.id for s in plan.sessions]
    assert result["bfd_timer_mismatches"] == [
        f"{s.interface}: BFD TX 300ms, requested 100ms" for s in plan.sessions
    ]
    assert result["bfd_timer_issues"] == [
        f"{s.interface}: BFD TX 300ms, requested 100ms" for s in plan.sessions
    ]


@pytest.mark.parametrize(
    "timers",
    ["", "0.100", "NaN 0.300", "inf 0.300", "-0.100 0.300", "0 0.300", "0.100 0"],
)
def test_up_session_with_unreadable_timers_is_not_silently_healthy(health_probe, timers):
    service, plan, responses = health_probe
    outputs(plan, responses)
    responses[("show", "bfd", "sessions")].stdout = responses[
        ("show", "bfd", "sessions")
    ].stdout.replace("0.300 0.900", timers)
    result = service.health(plan)
    assert result["established"] == 2  # Transport state is distinct from timer compliance.
    assert result["bfd_timer_mismatches"] == []
    assert result["bfd_timer_issues"] == [
        f"{s.interface}: effective BFD timers unavailable" for s in plan.sessions
    ]


def test_unrelated_correct_timer_cannot_hide_scoped_peer_mismatch(health_probe):
    service, plan, responses = health_probe
    outputs(plan, responses)
    responses[("show", "bfd", "sessions")].stdout += "\nfe80::1 unrelated0 Up 12:00:00 0.050 0.150"
    assert service.health(plan)["bfd_timer_issues"] == [
        f"{s.interface}: BFD TX 300ms, requested 50ms" for s in plan.sessions
    ]


def test_each_domain_uses_its_own_requested_interval():
    plan = resolve_rack(**rack_inputs())
    assert len({d.bfd_tx for d in plan.domains}) == 2
    rows = [
        f"{s.peer} {s.interface} Up 12:00:00 {d.bfd_tx / 1000:.3f} 0.900"
        for d in plan.domains
        for s in d.sessions
    ]
    assert rack_runtime._bfd_timer_health(plan, "\n".join(rows)) == {
        "issues": [],
        "mismatches": [],
    }
    # Change only replication's observed interval; public's stricter request
    # must not be overwritten by the second routing domain's settings.
    domain = plan.domains[1]
    session = domain.sessions[0]
    rows[-len(domain.sessions)] = f"{session.peer} {session.interface} Up 12:00:00 0.100 0.900"
    message = f"{session.interface}: BFD TX 100ms, requested 300ms"
    assert rack_runtime._bfd_timer_health(plan, "\n".join(rows)) == {
        "issues": [message],
        "mismatches": [message],
    }
