"""Monotonic restrictions of an accepted host plan, independent of peer health.

A malformed new topology is not authorization to add anything. Valid operator
constraints and complete local workload snapshots can still remove exports,
imports or adjacencies from the last accepted plan before topology resolution.
"""

import json
from ipaddress import IPv4Network

from maas_bird.models import Advertisements, Inventory, MultiDomainPlan, RoutingDomain
from maas_bird.resolver import AuthorizationWithdrawn


def restrict(
    previous: MultiDomainPlan,
    config: dict,
    inventory: Inventory,
    documents,
    passwords=None,
    attachments=(),
):
    host = previous.host
    if inventory.hosts is not None:
        matches = [h for h in inventory.hosts if h.system_uuid == host.system_uuid]
        if not matches:
            raise AuthorizationWithdrawn("Host routing identity withdrawn from inventory")
        if len(matches) != 1 or matches[0].rack != config["rack"]:
            raise AuthorizationWithdrawn("Host inventory no longer authorizes this rack")
        host = matches[0]
    domains = [RoutingDomain.model_validate(d) for d in json.loads(config["routing-domains"])]
    by_id = {d.id: d for d in domains}
    if len(by_id) != len(domains):
        raise ValueError("Duplicate routing domains")
    protected = [
        IPv4Network(p.strip()) for p in config["protected-prefixes"].split(",") if p.strip()
    ]
    peers = {(p.asn, p.id, (p.chassis_id or "").lower()) for p in host.peers or ()}
    bindings = _attachment_bindings(attachments)
    restricted = []
    for old in previous.domains:
        policy = by_id.get(old.domain)
        allowed = policy.export_allowlist if policy else ()
        permitted_imports = set(policy.import_prefixes) if policy else set()
        declared = (
            {c.id: c for c in policy.contexts if c.machine_id in (None, host.machine_id)}
            if policy
            else {}
        )
        sources = {
            source
            for context in old.contexts
            if context.id in declared
            and (context.numa_node, context.table, context.rule_priority)
            == (
                declared[context.id].numa_node,
                declared[context.id].table,
                declared[context.id].rule_priority,
            )
            for source in context.sources
            if source in declared[context.id].sources
        }
        prefixes = {
            p for p in old.prefixes if p in sources and any(p.subnet_of(a) for a in allowed)
        }
        for doc in documents:
            if doc.domain == old.domain and doc.advertisements is not None:
                ready = {ad.prefix for ad in doc.advertisements if ad.ready is True}
                prefixes &= ready
        imports = tuple(
            p
            for p in old.imports
            if p in permitted_imports and not any(p.overlaps(a) for a in protected)
        )
        sessions = tuple(
            s
            for s in old.sessions
            if policy is not None
            and (s.remote_asn, s.peer_id, s.chassis_id) in peers
            and (passwords is None or passwords.get(s.id))
            and (
                s.identity not in bindings
                or (s.id, old.domain, s.function, s.vlan, s.chassis_id, s.peer)
                in bindings[s.identity]
            )
            and host.local_asn == previous.host.local_asn
            and host.router_id == previous.host.router_id
        )
        contexts = tuple(
            c.model_copy(update={"exports": tuple(p for p in c.exports if p in prefixes)})
            for c in old.contexts
        )
        restricted.append(
            old.model_copy(
                update={
                    "prefixes": tuple(sorted(prefixes)),
                    "imports": imports,
                    "sessions": sessions,
                    "contexts": contexts,
                }
            )
        )
    return MultiDomainPlan(host=previous.host, domains=tuple(restricted))


def snapshots_for_resolution(documents: list[Advertisements]):
    """Unknown readiness contributes no new authorization, never a fleet-wide wait."""
    return [doc for doc in documents if doc.advertisements is not None]


def _attachment_bindings(documents):
    """Intersect complete card reports; delivery order must not defeat revocation."""
    bindings = {}
    for document in documents:
        for card in document.cards or ():
            if card.identity is None or card.attachments is None:
                continue  # Unknown is not an authoritative empty observation.
            observed = {
                (
                    a.id,
                    a.domain,
                    a.function,
                    a.vlan,
                    (a.chassis_id or "").lower(),
                    a.peer_link_local,
                )
                for a in card.attachments
                if a.ready is True
            }
            if card.identity in bindings:
                bindings[card.identity] &= observed
            else:
                bindings[card.identity] = observed
    return bindings
