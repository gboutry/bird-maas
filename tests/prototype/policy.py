"""Plan adapter for the standalone, boot-safe policy journal."""

import json
from typing import Any

from maas_bird.models import MultiDomainPlan, Plan, routing_plans

from tests.prototype.policy_journal import (
    GUARD_METRIC as GUARD_METRIC,
)
from tests.prototype.policy_journal import (
    PROTOCOL as PROTOCOL,
)
from tests.prototype.policy_journal import (
    PolicyError as PolicyError,
)
from tests.prototype.policy_journal import (
    PolicyJournal,
)
from tests.prototype.policy_journal import (
    guard_key as guard_key,
)
from tests.prototype.policy_journal import (
    rule_key as rule_key,
)
from tests.prototype.policy_journal import (
    rule_network as rule_network,
)


def layout(plan: Plan | MultiDomainPlan) -> dict[str, Any]:
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


class PolicyRouting(PolicyJournal):
    def ensure(self, plan: Plan | MultiDomainPlan):
        self.ensure_layout(layout(plan))

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
