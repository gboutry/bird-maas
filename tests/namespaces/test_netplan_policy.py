"""Actual Linux acceptance of Netplan/networkd protocol-4 source guards.

The outer runner creates private user/mount/network namespaces first. This test
creates one further owned netns and never invokes Netplan against a live host.
Native Netplan rendering is a separate verification boundary.
"""

import json
import os
import subprocess

from maas_bird import external_policy
from maas_bird.resolver import resolve_rack

from tests.factories import rack_inputs


def test_protocol_four_guards_are_native_fail_closed():
    assert os.environ.get("MAAS_BIRD_ISOLATED_TESTS") == "1", "Use scripts/test_namespaces.py"
    name = "mb-netplan-policy"

    def ip(*args, check=True):
        return subprocess.run(
            ["ip", *args], check=check, capture_output=True, text=True, timeout=10
        )

    ip("netns", "add", name)
    try:
        ip("-n", name, "link", "set", "lo", "up")
        inputs = rack_inputs()
        inputs["config"]["policy-protocol"] = 4
        plan = resolve_rack(**inputs)
        for context in plan.contexts:
            ip(
                "-n",
                name,
                "-4",
                "route",
                "add",
                "unreachable",
                "default",
                "table",
                str(context.table),
                "proto",
                "4",
                "metric",
                "42760",
            )
            for offset, source in enumerate(sorted(context.sources)):
                ip("-n", name, "-4", "addr", "add", str(source), "dev", "lo")
                ip(
                    "-n",
                    name,
                    "-4",
                    "rule",
                    "add",
                    "pref",
                    str(context.rule_priority + offset),
                    "from",
                    str(source),
                    "lookup",
                    str(context.table),
                    "protocol",
                    "4",
                )

        def inspect(argv):
            assert "show" in argv and "add" not in argv and "del" not in argv
            return ip("netns", "exec", name, *argv)

        external_policy.validate(external_policy.expected_policy(plan), inspect)
        rows = json.loads(ip("-n", name, "-4", "-j", "-N", "rule", "show").stdout)
        assert {
            int(row["protocol"])
            for row in rows
            if row.get("priority") in {context.rule_priority for context in plan.contexts}
        } == {4}
        source = str(plan.contexts[0].sources[0].network_address)
        denied = ip("-n", name, "-4", "route", "get", "203.0.113.1", "from", source, check=False)
        assert denied.returncode != 0
    finally:
        ip("netns", "del", name)
