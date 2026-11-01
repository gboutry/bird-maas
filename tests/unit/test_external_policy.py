"""Externally provisioned source-wide guards are checked, never repaired/adopted."""

import json
import subprocess

import pytest
from hypothesis import given
from hypothesis import strategies as st
from maas_bird import external_policy
from maas_bird.models import ContextIntent
from maas_bird.resolver import resolve_rack
from pydantic import ValidationError

from tests.factories import rack_inputs
from tests.rack_host_model import ProvisionedHost


def test_every_managed_source_is_guarded_for_all_destinations():
    plan = resolve_rack(**rack_inputs())
    expected = external_policy.expected_policy(plan)
    assert len(expected["rules"]) == sum(len(c.sources) for c in plan.contexts)
    assert all(rule["dst"] == "0.0.0.0/0" for rule in expected["rules"])
    assert len({rule["priority"] for rule in expected["rules"]}) == len(expected["rules"])


@pytest.mark.parametrize("fault", ["tos", "missing-rule", "early-rule", "foreign-route"])
def test_policy_rejection_never_mutates_the_network(fault):
    plan = resolve_rack(**rack_inputs())
    host = ProvisionedHost(plan)
    host.service = object()
    if fault == "tos":
        host.kernel.routes[-1]["tos"] = "0x10"
    elif fault == "missing-rule":
        host.kernel.rules.pop()
    elif fault == "early-rule":
        host.kernel.rules.append({"priority": 800, "src": "all", "table": 254})
    else:
        host.kernel.routes.append(
            {
                "table": plan.contexts[0].table,
                "protocol": 197,
                "dst": "203.0.113.0/24",
                "dev": "fabric1",
            }
        )
    before = host.kernel.snapshot()
    with pytest.raises(external_policy.PolicyError):
        external_policy.validate(external_policy.expected_policy(plan), host.execute)
    assert host.kernel.snapshot() == before
    assert all("add" not in args and "del" not in args for args in host.calls)


@pytest.mark.parametrize("fault", ["hash", "host", "owner", "phase", "permissions"])
def test_invalid_bundle_cannot_authorize_startup(rack_environment, fault):
    _, plan, passwords, service, _ = rack_environment
    service.apply_plan(plan, passwords)
    record = json.loads(service.manifest.read_text())
    if fault == "hash":
        record["configuration"] += "\n# altered after commit\n"
    elif fault == "host":
        record["plan"]["host"]["system_uuid"] = "another-host"
    elif fault == "owner":
        record["owner"] = "another-unit"
    elif fault == "phase":
        record["phase"] = "unconfirmed"
    else:
        service.manifest.chmod(0o640)
    service.manifest.write_text(json.dumps(record))
    with pytest.raises(external_policy.PolicyError):
        external_policy.startup_gate(service.directory)


def test_legacy_pid_lease_cannot_bypass_missing_bundle(tmp_path):
    # Neither a purported live PID nor an old lease is durable boot authority.
    (tmp_path / "activation.json").write_text(json.dumps({"pid": 1, "start_ticks": "0"}))
    with pytest.raises(OSError):
        external_policy.startup_gate(tmp_path)


def test_ip_read_failure_is_not_treated_as_absence():
    def failure(args):
        raise subprocess.CalledProcessError(1, args)

    with pytest.raises(subprocess.CalledProcessError):
        external_policy.validate({"rules": [], "tables": []}, failure)


def low_priority_plan():
    """Model a host whose existing marked traffic is selected at priority 75."""
    inputs = rack_inputs()
    domains = json.loads(inputs["config"]["routing-domains"])
    priority = 40
    for domain in domains:
        for context in domain["contexts"]:
            context["rule_priority"] = priority
            priority += 4
    inputs["config"]["routing-domains"] = json.dumps(domains)
    return resolve_rack(**inputs)


def test_source_guards_can_precede_existing_marked_host_policy():
    plan = low_priority_plan()
    host = ProvisionedHost(plan)
    host.service = object()
    host.kernel.rules.append({"priority": 75, "src": "all", "fwmark": "0x123", "table": 3190})
    before = host.kernel.snapshot()

    external_policy.validate(external_policy.expected_policy(plan), host.execute)

    assert host.kernel.snapshot() == before
    assert all(rule["priority"] < 75 for rule in external_policy.expected_policy(plan)["rules"])
    assert all("add" not in args and "del" not in args for args in host.calls)


def test_earlier_marked_rule_still_cannot_bypass_low_priority_guards():
    plan = low_priority_plan()
    host = ProvisionedHost(plan)
    host.service = object()
    host.kernel.rules.append({"priority": 20, "src": "all", "fwmark": "0x123", "table": 3190})
    before = host.kernel.snapshot()

    with pytest.raises(external_policy.PolicyError, match="earlier policy selector"):
        external_policy.validate(external_policy.expected_policy(plan), host.execute)

    assert host.kernel.snapshot() == before
    assert all("add" not in args and "del" not in args for args in host.calls)


@pytest.mark.parametrize("priority", [0, -1, 32001, True])
def test_invalid_source_rule_priority_is_rejected(priority):
    context = {
        "id": "numa0",
        "numa_node": 0,
        "table": 4510,
        "rule_priority": priority,
        "sources": ["198.18.248.11/32"],
    }
    with pytest.raises(ValidationError):
        ContextIntent.model_validate(context)


@given(priority=st.integers(min_value=1, max_value=999))
def test_all_positive_priorities_below_the_old_floor_are_preserved(priority):
    # Operators choose placement relative to existing host rules. The model must
    # preserve that placement rather than moving a source guard behind them.
    context = ContextIntent.model_validate(
        {
            "id": "numa0",
            "numa_node": 0,
            "table": 4510,
            "rule_priority": priority,
            "sources": ["198.18.248.11/32"],
        }
    )
    assert context.rule_priority == priority
