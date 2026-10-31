"""Compile one rack's typed domains into a host-local, fail-closed BIRD plan."""

import json
import re
from ipaddress import IPv4Network

from maas_bird.discovery import LocalLink
from maas_bird.models import (
    Attachment,
    Attachments,
    Host,
    Inventory,
    MultiDomainPlan,
    Peer,
    Plan,
    RoutingContext,
    RoutingDomain,
    Session,
    advertisement_mode,
    policy_protocol,
)


class NotReady(RuntimeError):
    """An input is unavailable; never interpret it as fresh authorization."""


class InvalidIntent(ValueError):
    """The proposed intent cannot safely be activated."""


class AuthorizationWithdrawn(InvalidIntent):
    """A complete authoritative snapshot no longer authorizes this host."""


def resolve_rack(uid, inventory, attachments, advertisements, local_links, config):
    if local_links is None:
        raise NotReady("Local link discovery is unavailable")
    if inventory.scope_state in {"conflict", "revoked"}:
        raise AuthorizationWithdrawn("MAAS application scope was withdrawn")
    if inventory.scope_state == "pending":
        raise NotReady("MAAS application membership is pending")
    mode = advertisement_mode(config)
    if not isinstance(inventory.rack, str) or not re.fullmatch(
        r"[a-zA-Z0-9_-]{1,48}", inventory.rack
    ):
        raise NotReady("Waiting for an authoritative inventory rack")
    host, peers = _resolve_host(uid, inventory)
    if host.rack != inventory.rack:
        raise InvalidIntent("Host is placed in a different physical rack")
    raw = json.loads(config["routing-domains"])
    if not isinstance(raw, list) or not raw:
        raise InvalidIntent("Configure a nonempty routing-domains array")
    domains = sorted((RoutingDomain.model_validate(item) for item in raw), key=lambda d: d.id)
    ids = [domain.id for domain in domains]
    if len(set(ids)) != len(ids) or any(
        not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", name) for name in ids
    ):
        raise InvalidIntent("Routing domain IDs must be unique safe identifiers")
    protected = tuple(
        IPv4Network(p.strip()) for p in config["protected-prefixes"].split(",") if p.strip()
    )
    if not protected:
        raise InvalidIntent("Declare protected management prefixes")
    contexts = [context for domain in domains for context in domain.contexts]
    if any(not context.machine_id for context in contexts):
        raise InvalidIntent("Bind every context explicitly to a MAAS machine ID")
    allocated = [
        (source, context.machine_id) for context in contexts for source in context.sources
    ]
    if any(
        a == b and machine_a != machine_b
        for index, (a, machine_a) in enumerate(allocated)
        for b, machine_b in allocated[index + 1 :]
    ):
        raise InvalidIntent("A rack source VIP cannot be allocated to multiple hosts")
    plans = tuple(
        _domain(
            host, peers, domain, protected, attachments, advertisements, local_links, config, mode
        )
        for domain in domains
    )
    for domain in domains:
        if any(
            source.overlaps(prefix)
            for context in domain.contexts
            for source in context.sources
            for prefix in protected
        ):
            raise InvalidIntent("Source bindings overlap protected management prefixes")
    for index, domain in enumerate(domains):
        if any(
            a.overlaps(b)
            for other in domains[index + 1 :]
            for a in domain.export_allowlist
            for b in other.export_allowlist
        ):
            raise InvalidIntent("Routing domain export allowlists overlap")
    _separation(plans)
    return MultiDomainPlan(
        host=host, domains=plans, advertisement_mode=mode, policy_protocol=policy_protocol(config)
    )


def _domain(host, peers, domain, protected, attachments, advertisements, links, config, mode):
    if not domain.contexts or not domain.import_prefixes or not domain.export_allowlist:
        raise InvalidIntent("Every domain needs contexts, imports and export allowlists")
    imports = domain.import_prefixes
    if any(p.prefixlen == 0 or any(p.overlaps(q) for q in protected) for p in imports):
        raise InvalidIntent("Domain imports overlap protected management prefixes")
    if any(p.overlaps(q) for i, p in enumerate(imports) for q in imports[i + 1 :]):
        raise InvalidIntent("Domain import prefixes overlap")
    sessions = tuple(
        sorted(
            _resolve_domain_sessions(attachments, links, peers, domain, host), key=lambda s: s.id
        )
    )
    contexts = _contexts(domain, host, sessions, advertisements, mode)
    tx, rx, mult, hold, keepalive = _timers(domain, config)
    return Plan(
        host=host,
        domain=domain.id,
        sessions=sessions,
        contexts=contexts,
        prefixes=tuple(sorted({p for context in contexts for p in context.exports})),
        imports=tuple(sorted(imports)),
        kernel_table=contexts[0].table,
        bfd_tx=tx,
        bfd_rx=rx,
        bfd_multiplier=mult,
        hold_time=hold,
        keepalive_time=keepalive,
    )


def _contexts(domain, host, sessions, documents, mode):
    selected = [c for c in domain.contexts if c.machine_id in (None, host.machine_id)]
    if not selected:
        raise NotReady("Waiting for this host's routing contexts")
    ids = [c.id for c in selected]
    if len(set(ids)) != len(ids) or any(
        not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", name) for name in ids
    ):
        raise InvalidIntent("Context IDs must be unique safe identifiers")
    if any(not c.sources or any(p.prefixlen != 32 for p in c.sources) for c in selected):
        raise InvalidIntent("Every context requires source /32 bindings")
    for context in selected:
        paths = [
            s for s in sessions if context.attachment_ids is None or s.id in context.attachment_ids
        ]
        if context.numa_node is None:
            continue  # Explicitly neutral: no locality preference or NUMA requirement.
        if any(s.numa_node is None for s in paths):
            raise NotReady("NUMA locality is unknown for a host attachment")
        if not any(s.numa_node == context.numa_node for s in paths):
            raise InvalidIntent("Every context requires a verified NUMA-local attachment")
    exports = _context_exports(domain, host, selected, documents, mode)
    return tuple(
        RoutingContext(**c.model_dump(), exports=tuple(sorted(exports[c.id])))
        for c in sorted(selected, key=lambda c: c.id)
    )


def _context_exports(domain, host, selected, documents, mode):
    """Node origins are explicit address intent, never synthetic workload health.

    The charm still checks every /32 on lo and the runtime validates persistent
    source policy before any origin is activated. Workload mode retains the
    existing producer-controlled authorization boundary.
    """
    by_id = {c.id: c for c in selected}
    exports = {c.id: set() for c in selected}
    if mode == "node-addresses":
        return {
            c.id: {p for p in c.sources if any(p.subnet_of(a) for a in domain.export_allowlist)}
            for c in selected
        }
    for document in documents:
        if document.machine_id != host.machine_id or document.domain != domain.id:
            continue
        for ad in document.advertisements or ():
            if ad.ready is None or ad.prefix is None or ad.context is None:
                raise NotReady("Local advertisement snapshot is incomplete")
            if ad.context not in by_id or ad.prefix not in by_id[ad.context].sources:
                raise InvalidIntent("Advertisement does not match its declared source context")
            if ad.ready and any(ad.prefix.subnet_of(p) for p in domain.export_allowlist):
                exports[ad.context].add(ad.prefix)
    return exports


def _timers(domain, config):
    values = [
        getattr(domain, field) if getattr(domain, field) is not None else config[key]
        for field, key in (
            ("bfd_min_tx_ms", "bfd-min-tx-ms"),
            ("bfd_min_rx_ms", "bfd-min-rx-ms"),
            ("bfd_multiplier", "bfd-multiplier"),
            ("bgp_hold_time_seconds", "bgp-hold-time-seconds"),
            ("bgp_keepalive_time_seconds", "bgp-keepalive-time-seconds"),
        )
    ]
    if any(type(v) is not int for v in values):
        raise InvalidIntent("BGP/BFD timers must be integers")
    tx, rx, mult, hold, keepalive = values
    if not (50 <= tx <= 60000 and 50 <= rx <= 60000 and 1 <= mult <= 255):
        raise InvalidIntent("Invalid BFD timers")
    if not (1 <= keepalive <= 21845 and 3 * keepalive <= hold <= 65535):
        raise InvalidIntent("BGP hold time must be at least three keepalive intervals")
    return tx, rx, mult, hold, keepalive


def _separation(plans):
    ids = [s.id for p in plans for s in p.sessions]
    interfaces = [s.interface for p in plans for s in p.sessions]
    if len(set(ids)) != len(ids) or len(set(interfaces)) != len(interfaces):
        raise InvalidIntent("Sessions and attachment interfaces must be unique across domains")
    tables, sources, priorities = set(), set(), set()
    for index, plan in enumerate(plans):
        for other in plans[index + 1 :]:
            if any(p.overlaps(q) for p in plan.imports for q in other.imports):
                raise InvalidIntent("Routing domain destination prefixes overlap")
        for context in plan.contexts:
            occupied = set(
                range(context.rule_priority, context.rule_priority + len(context.sources))
            )
            if context.table in tables or priorities & occupied or max(occupied) > 32000:
                raise InvalidIntent("Routing domains conflict in tables or source-rule priorities")
            if len(set(context.sources)) != len(context.sources) or sources & set(context.sources):
                raise InvalidIntent("Routing domain source bindings must be unique")
            tables.add(context.table)
            priorities.update(occupied)
            sources.update(context.sources)


def _resolve_host(uid: str, inventory: Inventory) -> tuple[Host, dict[str, Peer]]:
    """Select one host and validate its allocated routing identity and rack peers."""
    if inventory.hosts is not None and not any(
        h.system_uuid == uid.lower() for h in inventory.hosts
    ):
        raise AuthorizationWithdrawn("Host routing identity withdrawn from inventory")
    hosts = [h for h in inventory.hosts or () if h.system_uuid == uid.lower()]
    if len(hosts) != 1:
        raise (
            NotReady("Waiting for one matching MAAS host")
            if not hosts
            else InvalidIntent("Multiple inventory records match this host")
        )
    host = hosts[0]
    host_peers = host.peers
    if not host.machine_id or not host.local_asn or not host.router_id or not host_peers:
        raise NotReady("Host routing identity is incomplete")
    peers = {p.chassis_id.lower(): p for p in host_peers if p.chassis_id}
    if len(peers) != len(host_peers):
        raise InvalidIntent("Rack peer identities are missing or duplicated")
    if any(not p.asn or not p.id for p in peers.values()):
        raise NotReady("Rack peer configuration is incomplete")
    return host, peers


def _resolve_domain_sessions(documents, links, peers, domain, host):
    """Resolve each context's approved pair without conflating same-TOR paths."""
    selected = [c for c in domain.contexts if c.machine_id == host.machine_id]
    sessions = {}
    for context in selected:
        ids = context.attachment_ids
        if ids is not None and (len(set(ids)) != len(ids) or any(not name for name in ids)):
            raise InvalidIntent("Context attachment IDs must be unique nonempty identifiers")
        scoped = _documents_for_paths(documents, ids)
        paths = _resolve_sessions(scoped, links, peers, domain.id)
        if ids is not None and {s.id for s in paths} != set(ids):
            raise NotReady("Waiting for all declared context attachments")
        for session in paths:
            if session.id in sessions and sessions[session.id] != session:
                raise InvalidIntent("An attachment ID identifies conflicting paths")
            sessions[session.id] = session
    return list(sessions.values())


def _documents_for_paths(documents, ids):
    """Retain unknown snapshots, filtering only explicit context path grants."""
    if ids is None:
        return documents
    scoped = []
    for document in documents:
        cards = []
        for card in document.cards or ():
            attachments = (
                None
                if card.attachments is None
                else tuple(a for a in card.attachments if a.id in ids)
            )
            cards.append(card.model_copy(update={"attachments": attachments}))
        scoped.append(document.model_copy(update={"cards": tuple(cards)}))
    return scoped


def _resolve_sessions(
    documents: list[Attachments],
    local_links: list[LocalLink],
    peers: dict[str, Peer],
    domain: str,
) -> list[Session]:
    """Match card producers first, then validate each domain's local adjacency.

    A card can expose several functions, but two producers must never claim the
    same local card. Each approved TOR and local interface may be selected once.
    """
    sessions: list[Session] = []
    attached_peers = set()
    serial_producers = {}
    for producer, document in enumerate(documents):
        # One card can expose multiple PFs. The provider deduplicates those as one card record.
        for card in document.cards or ():
            identity = card.identity
            if not identity or not identity.scheme or not identity.value:
                continue
            candidates = [link for link in local_links if link.identity == identity]
            if not candidates:
                continue
            if identity in serial_producers:
                raise InvalidIntent("Duplicate attachment providers for one local card")
            serial_producers[identity] = producer
            for attachment in card.attachments or ():
                if attachment.domain != domain:
                    continue
                sessions.append(_resolve_attachment(attachment, candidates, peers, attached_peers))
    if len(sessions) != len(peers):
        raise NotReady("Waiting for one attachment per approved TOR")
    if len({s.interface for s in sessions}) != len(sessions):
        raise InvalidIntent("Multiple sessions resolve to the same interface")
    return sessions


def _resolve_attachment(
    attachment: Attachment,
    candidates: list[LocalLink],
    peers: dict[str, Peer],
    attached_peers: set[str],
) -> Session:
    """Join exact function/VLAN coordinates and reserve one approved TOR binding."""
    if attachment.ready is not True:
        raise NotReady("A local attachment is not ready")
    f = attachment.function
    if attachment.ovs_port is not None and f is not None:
        raise InvalidIntent("Attachment mixes physical and logical selectors")
    if attachment.ovs_port is None and (not f or f.controller is None or f.pf is None):
        raise NotReady("Attachment function coordinates are incomplete")
    if not attachment.chassis_id or attachment.chassis_id.lower() not in peers:
        raise InvalidIntent("Local attachment reaches an unapproved TOR")
    if attachment.ovs_port is not None:
        if not attachment.ovs_port.complete or attachment.ovs_port.vlan != attachment.vlan:
            raise NotReady("Logical OVS attachment identity is incomplete")
        matches = [link for link in candidates if link.ovs_port == attachment.ovs_port]
    else:
        assert f is not None
        matches = [
            link
            for link in candidates
            if link.ovs_port is None
            and (link.controller, link.pf, link.vf, link.vlan)
            == (f.controller, f.pf, f.vf, attachment.vlan)
        ]
    if len(matches) != 1:
        raise (
            NotReady("Waiting for one matching local interface")
            if not matches
            else (InvalidIntent("Attachment matches multiple local interfaces"))
        )
    link = matches[0]
    if not link.link_local or not attachment.peer_link_local or not attachment.id:
        raise NotReady("Attachment has no usable IPv6 link-local adjacency")
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,15}", link.interface):
        raise InvalidIntent("Unsafe interface name")
    peer = peers[attachment.chassis_id.lower()]
    assert peer.asn is not None  # Validated for every approved peer above.
    if attachment.chassis_id.lower() in attached_peers:
        raise InvalidIntent("Multiple attachments resolve to the same approved TOR")
    attached_peers.add(attachment.chassis_id.lower())
    return Session(
        id=attachment.id,
        interface=link.interface,
        peer=attachment.peer_link_local,
        local=link.link_local,
        remote_asn=peer.asn,
        identity=link.identity,
        peer_id=peer.id,
        function=attachment.function,
        ovs_port=link.ovs_port,
        vlan=attachment.vlan,
        chassis_id=attachment.chassis_id.lower(),
        numa_node=link.numa_node,
    )
