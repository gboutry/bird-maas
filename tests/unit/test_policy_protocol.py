"""Netplan's static tag is explicit intent, never permission to adopt weaker guards."""

import json
import subprocess

import pytest
from maas_bird import authority, external_policy, rack_runtime
from maas_bird.rack_runtime import MigrationRequired, RackService
from maas_bird.resolver import resolve_rack
from maas_bird.runtime import RuntimeErrorBase

from tests.factories import rack_inputs
from tests.rack_host_model import ProvisionedHost


def netplan_plan():
    inputs = rack_inputs()
    inputs["config"]["policy-protocol"] = 4
    return inputs, resolve_rack(**inputs)


def test_default_tag_stays_196_and_netplan_requires_explicit_four():
    assert resolve_rack(**rack_inputs()).policy_protocol == 196
    _, plan = netplan_plan()
    assert plan.policy_protocol == 4
    expected = external_policy.expected_policy(plan)
    assert expected["protocol"] == 4
    assert {rule["protocol"] for rule in expected["rules"]} == {4}


@pytest.mark.parametrize("value", [True, 4.0, "4", 0, 12, 197, 256])
def test_no_implicit_or_arbitrary_policy_tag(value):
    inputs = rack_inputs()
    inputs["config"]["policy-protocol"] = value
    with pytest.raises(ValueError, match="policy-protocol"):
        resolve_rack(**inputs)


@pytest.mark.parametrize("fault", [None, "rule-tag", "guard-tag", "tos", "priority"])
def test_netplan_tag_keeps_exact_native_selector_checks(fault):
    _, plan = netplan_plan()
    host = ProvisionedHost(plan)
    host.service = object()
    tables = {context.table for context in plan.contexts}
    for row in (*host.kernel.rules, *host.kernel.routes):
        if row["table"] in tables:
            row["protocol"] = 4
    rule = next(
        row
        for row in host.kernel.rules
        if (
            row["table"] == plan.contexts[0].table
            and row["priority"] == plan.contexts[0].rule_priority
            and row["protocol"] == 4
        )
    )
    guard = next(
        row
        for row in host.kernel.routes
        if (
            row["table"] == plan.contexts[0].table
            and row.get("type") == "unreachable"
            and row["protocol"] == 4
        )
    )
    if fault == "rule-tag":
        rule["protocol"] = 196
    elif fault == "guard-tag":
        guard["protocol"] = 196
    elif fault == "tos":
        guard["tos"] = "0x10"
    elif fault == "priority":
        rule["priority"] = plan.contexts[0].rule_priority - 1
    if fault is None:
        external_policy.validate(external_policy.expected_policy(plan), host.execute)
    else:
        with pytest.raises(external_policy.PolicyError):
            external_policy.validate(external_policy.expected_policy(plan), host.execute)
    assert all("add" not in argv and "del" not in argv for argv in host.calls)


def test_protocol_change_cannot_migrate_an_accepted_layout(rack_environment):
    inputs, old, passwords, service, host = rack_environment
    service.apply_plan(old, passwords)
    before = host.kernel.snapshot()
    config = inputs["config"] | {"policy-protocol": 4}
    proposed = resolve_rack(**(inputs | {"config": config}))
    floor = authority.restrict(old, config, inputs["inventory"], [])
    assert floor.policy_protocol == 196
    with pytest.raises(MigrationRequired):
        service.apply_plan(proposed, passwords)
    assert service.state() == old
    assert host.kernel.snapshot() == before


def test_cold_admission_rejects_foreign_guard_even_if_it_looks_unreachable(monkeypatch):
    _, plan = netplan_plan()
    table = plan.contexts[0].table
    rows = [
        {
            "type": "unreachable",
            "dst": "default",
            "table": table,
            "protocol": protocol,
            "metric": 42760,
        }
        for protocol in (4, 196)
    ]

    def inspect(argv, *, check=True):
        if argv in (
            ["systemctl", "is-active", "bird.service"],
            ["systemctl", "is-enabled", "bird.service"],
        ):
            assert check is False
            return subprocess.CompletedProcess(argv, 3, "", "")
        assert argv == ["ip", "-4", "-j", "-N", "route", "show", "table", "all"]
        return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")

    monkeypatch.setattr(rack_runtime, "run", inspect)
    with pytest.raises(RuntimeErrorBase, match="pre-existing routes"):
        RackService._check_unclaimed_network(plan)
