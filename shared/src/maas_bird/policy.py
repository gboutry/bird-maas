"""Owned IPv4 source-policy rules and persistent fail-closed guards.

Only exact journaled objects are removed; never flush rules or routing tables.
The journal is written before mutations. An interrupted unacknowledged operation
that is present in the kernel is deliberately blocked for operator inspection.
"""

import json
import os
from ipaddress import IPv4Network
from pathlib import Path

from maas_bird.models import MultiDomainPlan, Plan, routing_plans

PROTOCOL = 196
GUARD_METRIC = 42760


class PolicyError(RuntimeError):
    pass


def layout(plan: Plan | MultiDomainPlan) -> dict:
    rules = []
    destinations = sorted({p for domain in routing_plans(plan) for p in domain.imports})
    for context in plan.contexts:
        offset = 0
        for source in sorted(context.sources):
            for destination in destinations:
                rules.append(
                    {
                        "src": str(source),
                        "dst": str(destination),
                        "table": context.table,
                        "priority": context.rule_priority + offset,
                        "protocol": PROTOCOL,
                    }
                )
                offset += 1
    result = {
        "tables": sorted(c.table for c in plan.contexts),
        "rules": sorted(rules, key=lambda rule: rule["priority"]),
    }
    if isinstance(plan, MultiDomainPlan):
        result["domains"] = [
            {
                "id": domain.domain,
                "contexts": [
                    {"id": c.id, "table": c.table, "sources": list(map(str, sorted(c.sources)))}
                    for c in domain.contexts
                ],
                "imports": list(map(str, sorted(domain.imports))),
            }
            for domain in plan.domains
        ]
    return result


def rule_network(rule: dict, field: str):
    value = rule.get(field, "all")
    if value == "all":
        return IPv4Network("0.0.0.0/0")
    # iproute2 versions may serialize a prefix as address plus a separate length.
    if "/" not in value and field + "len" in rule:
        value += "/" + str(rule[field + "len"])
    return IPv4Network(value)


def rule_key(rule: dict):
    if set(rule) - {
        "src",
        "srclen",
        "dst",
        "dstlen",
        "table",
        "priority",
        "protocol",
        "flags",
    } or rule.get("flags"):
        return None
    try:
        return (
            str(rule_network(rule, "src")),
            str(rule_network(rule, "dst")),
            int(rule["table"]),
            int(rule["priority"]),
            int(rule.get("protocol", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def guard_key(route: dict):
    if (
        str(route.get("type")) not in {"unreachable", "7"}
        or route.get("dst", "default") != "default"
    ):
        return None
    try:
        return int(route["table"]), int(route["protocol"]), int(route["metric"])
    except (KeyError, TypeError, ValueError):
        return None


class PolicyRouting:
    def __init__(self, directory: Path, owner: str, execute):
        self.directory, self.owner, self.execute = directory, owner, execute
        self.path = directory / "policy.json"

    def _snapshot(self):
        rules = json.loads(self.execute(["ip", "-j", "-N", "rule", "show"]).stdout)
        routes = json.loads(
            self.execute(["ip", "-j", "-N", "route", "show", "table", "all"]).stdout
        )
        return rules, routes

    def _save(self, value):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _load(self):
        value = json.loads(self.path.read_text())
        if value.get("owner") != self.owner or value.get("version") != 1:
            raise PolicyError("Unknown policy routing ownership")
        return value

    def _command(self, operation, verb):
        kind, value = operation
        if kind == "guard":
            return [
                "ip",
                "route",
                verb,
                "unreachable",
                "default",
                "table",
                str(value),
                "proto",
                str(PROTOCOL),
                "metric",
                str(GUARD_METRIC),
            ]
        return [
            "ip",
            "rule",
            verb,
            "priority",
            str(value["priority"]),
            "from",
            value["src"],
            "to",
            value["dst"],
            "table",
            str(value["table"]),
            "protocol",
            str(PROTOCOL),
        ]

    def _present(self, operation, rules, routes):
        kind, value = operation
        if kind == "guard":
            return any(guard_key(route) == (value, PROTOCOL, GUARD_METRIC) for route in routes)
        return any(rule_key(rule) == rule_key(value) for rule in rules)

    def _verify_removable(self, operation, rules, routes):
        kind, value = operation
        if kind == "rule":
            if any(
                r.get("priority") == value["priority"] and rule_key(r) != rule_key(value)
                for r in rules
            ):
                raise PolicyError("Foreign rule replaced an owned priority; refusing removal")
        elif any(
            str(r.get("table")) == str(value)
            and r.get("dst", "default") == "default"
            and r.get("metric") == GUARD_METRIC
            and guard_key(r) != (value, PROTOCOL, GUARD_METRIC)
            for r in routes
        ):
            raise PolicyError("Foreign route replaced an owned guard; refusing removal")

    def _preflight(self, desired, rules, routes):
        if any(str(route.get("table")) in {str(t) for t in desired["tables"]} for route in routes):
            raise PolicyError("A context routing table is already populated")
        priorities = {rule["priority"] for rule in desired["rules"]}
        for rule in rules:
            key = rule_key(rule)
            default = key is not None and key[:4] in [
                ("0.0.0.0/0", "0.0.0.0/0", 255, 0),
                ("0.0.0.0/0", "0.0.0.0/0", 254, 32766),
                ("0.0.0.0/0", "0.0.0.0/0", 253, 32767),
            ]
            if rule.get("priority") in priorities or str(rule.get("table")) in {
                str(t) for t in desired["tables"]
            }:
                raise PolicyError("A context rule priority or table is already in use")
            if default:
                continue
            src = rule_network(rule, "src")
            dst = rule_network(rule, "dst")
            if any(
                src.overlaps(IPv4Network(r["src"])) and dst.overlaps(IPv4Network(r["dst"]))
                for r in desired["rules"]
            ):
                raise PolicyError("A foreign policy rule overlaps the requested managed traffic")

    def ensure(self, plan: Plan | MultiDomainPlan):
        desired = layout(plan)
        rules, routes = self._snapshot()
        if self.path.exists():
            state = self._load()
            if state["layout"] != desired:
                raise PolicyError("Policy selectors/tables are immutable until decommissioned")
            if state.get("pending") and self._present(state["pending"], rules, routes):
                raise PolicyError("Interrupted unacknowledged policy operation needs inspection")
            if state["phase"] != "active":
                self.remove()
                rules, routes = self._snapshot()
            else:
                if not all(self._present(op, rules, routes) for op in state["done"]):
                    raise PolicyError("Owned routing policy drift detected")
                for op in state["done"]:
                    self._verify_removable(op, rules, routes)
                owned = {rule_key(rule) for rule in desired["rules"]}
                self._preflight(
                    desired, [rule for rule in rules if rule_key(rule) not in owned], []
                )
                return
        self._preflight(desired, rules, routes)
        state = {
            "version": 1,
            "owner": self.owner,
            "layout": desired,
            "phase": "installing",
            "pending": None,
            "done": [],
        }
        self._save(state)
        operations = [("guard", table) for table in desired["tables"]]
        operations += [("rule", rule) for rule in desired["rules"]]
        try:
            for operation in operations:
                state["pending"] = operation
                self._save(state)
                self.execute(self._command(operation, "add"))
                state["done"].append(operation)
                state["pending"] = None
                self._save(state)
            state["phase"] = "active"
            self._save(state)
        except Exception:
            # Roll back only acknowledged objects; an uncertain operation is not adopted.
            rules, routes = self._snapshot()
            uncertain = state["pending"] and self._present(state["pending"], rules, routes)
            if not uncertain:
                state["pending"] = None
            self._save(state)
            if not uncertain:
                self.remove()
            raise

    def remove(self):
        if not self.path.exists():
            return
        state = self._load()
        rules, routes = self._snapshot()
        if state.get("pending") and self._present(state["pending"], rules, routes):
            raise PolicyError("Unacknowledged policy operation cannot be removed automatically")
        for operation in state["done"]:
            self._verify_removable(operation, rules, routes)
        for operation in reversed(state["done"]):
            rules, routes = self._snapshot()
            if self._present(operation, rules, routes):
                self.execute(self._command(operation, "del"))
            state["done"].remove(operation)
            state["pending"] = None
            self._save(state)
        self.path.unlink()

    def verify_routes(self, plan: Plan | MultiDomainPlan):
        """Acceptance requires each destination to use a verified NUMA-local attachment."""
        for domain in routing_plans(plan):
            for context in domain.contexts:
                routes = json.loads(
                    self.execute(
                        ["ip", "-j", "-N", "route", "show", "table", str(context.table)]
                    ).stdout
                )
                if isinstance(plan, MultiDomainPlan) and any(
                    str(route.get("type", "unicast")) in {"unicast", "1"}
                    and route.get("dst") not in {str(prefix) for prefix in domain.imports}
                    for route in routes
                ):
                    return False
                local = {s.interface for s in domain.sessions if s.numa_node == context.numa_node}
                for destination in domain.imports:
                    if not any(
                        route.get("dst") == str(destination)
                        and route.get("dev") in local
                        and str(route.get("type", "unicast")) in {"unicast", "1"}
                        for route in routes
                    ):
                        return False
        return True
