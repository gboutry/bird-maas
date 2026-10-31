"""Read-only validation of policy owned by the host network provisioning layer.

This module is installed as a stdlib-only systemd pre-start checker. It never
adds, deletes, restores or adopts a rule/route. Provisioning owns persistence and
boot ordering; BIRD may run only once the declared source guards are present.
"""

import argparse
import grp
import hashlib
import json
import os
import subprocess
from ipaddress import IPv4Interface
from pathlib import Path

from maas_bird.policy_objects import (
    GUARD_METRIC,
    PROTOCOL,
    PolicyError,
    guard_key,
    rule_key,
    rule_network,
)
from maas_bird.runtime import write_atomic


def expected_policy(plan):
    """One catch-all source selector per /32; no destination can fall into main."""
    return {
        "tables": sorted(c.table for c in plan.contexts),
        "sources": sorted(str(source) for c in plan.contexts for source in c.sources),
        "contexts": [
            {
                "table": context.table,
                "destinations": list(map(str, domain.imports)),
                "interfaces": [s.interface for s in domain.sessions],
            }
            for domain in plan.domains
            for context in domain.contexts
        ],
        "rules": [
            {
                "src": str(source),
                "dst": "0.0.0.0/0",
                "table": context.table,
                "priority": context.rule_priority + offset,
                "protocol": PROTOCOL,
            }
            for context in sorted(plan.contexts, key=lambda c: c.table)
            for offset, source in enumerate(sorted(context.sources))
        ],
    }


def validate(expected, execute, *, check_routes=True):
    rules = json.loads(execute(["ip", "-4", "-j", "-N", "rule", "show"]).stdout)
    routes = json.loads(execute(["ip", "-4", "-j", "-N", "route", "show", "table", "all"]).stdout)
    expected_keys = {rule_key(rule) for rule in expected["rules"]}
    if None in expected_keys or not expected_keys <= {rule_key(rule) for rule in rules}:
        raise PolicyError("Provision declared host source rules before enabling BIRD")
    addresses = json.loads(execute(["ip", "-4", "-j", "address", "show", "dev", "lo"]).stdout)
    local = {
        str(IPv4Interface(f"{address['local']}/{address['prefixlen']}").network)
        for interface in addresses
        for address in interface.get("addr_info", [])
    }
    if not set(expected["sources"]) <= local:
        raise PolicyError("Provision every declared source VIP as a /32 on lo")
    for table in expected["tables"]:
        if not any(guard_key(route) == (table, PROTOCOL, GUARD_METRIC) for route in routes):
            raise PolicyError("Provision an exact unreachable-default guard in every host context")
    for context in expected.get("contexts", []) if check_routes else ():
        for route in routes:
            if str(route.get("table")) != str(context["table"]):
                continue
            if guard_key(route) == (context["table"], PROTOCOL, GUARD_METRIC):
                continue
            devices = [hop.get("dev") for hop in route.get("nexthops", [route])]
            if (
                str(route.get("type", "unicast")) not in {"unicast", "1"}
                or str(route.get("protocol")) != "12"
                or route.get("dst") not in context["destinations"]
                or not devices
                or any(device not in context["interfaces"] for device in devices)
            ):
                raise PolicyError("Unexpected route in a dedicated host context")
    priorities = {rule["priority"] for rule in expected["rules"]}
    if any(
        rule.get("priority") in priorities and rule_key(rule) not in expected_keys
        for rule in rules
    ):
        raise PolicyError("A foreign selector occupies a reserved host policy priority")
    # Unknown overlapping selectors could bypass our guards. Reuse the ownership
    # preflight's selector analysis after removing the explicitly provisioned ones.
    from_rules = [rule for rule in rules if rule_key(rule) not in expected_keys]
    for rule in from_rules:
        priority = int(rule.get("priority", 0))
        if priority == 0:
            # Only the exact kernel local-address selector may precede guards.
            # A second priority-zero rule must not acquire an implicit bypass.
            if not (
                str(rule.get("table")) in {"255", "local"}
                and rule_network(rule, "src").prefixlen == 0
                and rule_network(rule, "dst").prefixlen == 0
                and all(key in {"priority", "src", "dst", "table", "protocol"} for key in rule)
            ):
                raise PolicyError("A priority-zero selector can bypass managed source guards")
            continue
        if priority < max(priorities):
            key = rule_key(rule)
            if key is None or any(_overlaps(rule, desired) for desired in expected["rules"]):
                raise PolicyError("An earlier policy selector can bypass managed source guards")


def _overlaps(rule, desired):
    return rule_network(rule, "src").overlaps(rule_network(desired, "src"))


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise PolicyError("Cannot inspect provisioned source policy")
    return result


def host_uuid():
    value = Path("/sys/class/dmi/id/product_uuid").read_text().strip().lower()
    if not value or value == "00000000-0000-0000-0000-000000000000":
        raise PolicyError("No usable physical host identity")
    return value


def startup_gate(directory):
    """A single atomic bundle is the only durable startup authority.

    A restrictive floor is safe to start before live acknowledgement: it cannot
    reintroduce a removed export. No PID leases or partially updated config/policy
    file pairs participate in boot admission.
    """
    path = directory / "state.json"
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise PolicyError("Startup state must be private and not a symlink")
    state = json.loads(path.read_text())
    if (
        state.get("version") != 2
        or state.get("owner") != (directory / "owner").read_text().strip()
    ):
        raise PolicyError("Unknown startup generation ownership")
    if state.get("phase") not in {"accepted", "authorization-floor"}:
        raise PolicyError("Unaccepted startup generation")
    if state.get("plan", {}).get("host", {}).get("system_uuid") != host_uuid():
        raise PolicyError("Routing belongs to another physical host")
    if hashlib.sha256(state["configuration"].encode()).hexdigest() != state.get("config_sha256"):
        raise PolicyError("Startup configuration integrity failure")
    return state


def materialize(directory, output):
    """Create a root-owned volatile BIRD config from the accepted atomic bundle."""
    state = startup_gate(directory)
    validate(state["policy"], command)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    group = grp.getgrnam("bird").gr_gid
    os.chown(output.parent, 0, group)
    output.parent.chmod(0o750)
    write_atomic(output, state["configuration"], 0o640, group)


def main():
    parser = argparse.ArgumentParser(description="Verify provisioned policy before BIRD")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, default=Path("/run/maas-bird-config/bird.conf"))
    args = parser.parse_args()
    materialize(args.directory, args.output)


if __name__ == "__main__":
    main()
