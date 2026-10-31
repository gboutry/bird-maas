"""Resolve generic inventory and attachments into a locally verified routing plan."""

import json
import re
from ipaddress import IPv4Network

from maas_bird.discovery import LocalLink
from maas_bird.models import (
    Advertisements,
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
)


class NotReady(RuntimeError):
    """Inputs are incomplete; preserve an existing working configuration."""


class InvalidIntent(ValueError):
    """Inputs conflict or violate the supported routing policy."""


class AuthorizationWithdrawn(InvalidIntent):
    """A complete authoritative snapshot no longer authorizes this host."""


def resolve(
    uid: str,
    inventory: Inventory,
    attachments: list[Attachments],
    advertisements: list[Advertisements],
    local_links: list[LocalLink],
    config: dict,
) -> Plan | MultiDomainPlan:
    raw = json.loads(config.get("routing-domains", "[]"))
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise InvalidIntent("routing-domains must be an array of objects")
    if raw:
        return resolve_domains(
            uid, inventory, attachments, advertisements, local_links, config, raw
        )
    return resolve_single(uid, inventory, attachments, advertisements, local_links, config)


def resolve_single(
    uid: str,
    inventory: Inventory,
    attachments: list[Attachments],
    advertisements: list[Advertisements],
    local_links: list[LocalLink],
    config: dict,
) -> Plan:
    """Resolve one domain without recursively widening the result to a host plan."""
    host, peers = _resolve_host(uid, inventory)
    sessions = _resolve_sessions(attachments, local_links, peers, config["routing-domain"])
    prefixes = _ready_prefixes(advertisements, host, config["routing-domain"])
    imports = tuple(
        IPv4Network(p.strip()) for p in config["import-prefixes"].split(",") if p.strip()
    )
    allowed = [IPv4Network(p.strip()) for p in config["export-allowlist"].split(",") if p.strip()]
    if config.get("_rack-mode"):
        prefixes = {p for p in prefixes if any(p.subnet_of(network) for network in allowed)}
    elif any(not any(prefix.subnet_of(network) for network in allowed) for prefix in prefixes):
        raise InvalidIntent("Advertisement is outside the operator export allowlist")
    table, tx, rx, mult, hold, keepalive = _routing_parameters(config)
    sessions.sort(key=lambda session: session.interface)
    return Plan(
        host=host,
        sessions=tuple(sessions),
        prefixes=tuple(sorted(prefixes)),
        imports=imports,
        kernel_table=table,
        bfd_tx=tx,
        bfd_rx=rx,
        bfd_multiplier=mult,
        hold_time=hold,
        keepalive_time=keepalive,
        contexts=resolve_contexts(config, sessions, advertisements, host.machine_id, imports),
        domain=config["routing-domain"],
    )


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
    if not f or f.controller is None or f.pf is None:
        raise NotReady("Attachment function coordinates are incomplete")
    if not attachment.chassis_id or attachment.chassis_id.lower() not in peers:
        raise InvalidIntent("Local attachment reaches an unapproved TOR")
    matches = [
        link
        for link in candidates
        if (link.controller, link.pf, link.vf, link.vlan)
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
        chassis_id=attachment.chassis_id.lower(),
        numa_node=link.numa_node,
    )


def _ready_prefixes(
    advertisements: list[Advertisements], host: Host, domain: str
) -> set[IPv4Network]:
    """Require complete snapshots before selecting this host's ready /32 exports."""
    if not advertisements or any(d.advertisements is None for d in advertisements):
        raise NotReady("Waiting for complete advertisement snapshots")
    prefixes = set()
    for document in advertisements:
        for a in document.advertisements or ():
            if a.machine_id != host.machine_id or a.domain != domain:
                continue
            if a.ready is None or a.prefix is None:
                raise NotReady("Advertisement is incomplete")
            if a.ready:
                if a.prefix.prefixlen != 32:
                    raise InvalidIntent("Only locally owned IPv4 /32 advertisements are supported")
                prefixes.add(a.prefix)
    return prefixes


def _routing_parameters(config) -> tuple[int, int, int, int, int, int]:
    """Validate kernel-table ownership range and BGP/BFD timer bounds."""
    table = config["kernel-table"]
    if not 1000 <= table <= 4294967295:
        raise InvalidIntent("Use a dedicated kernel routing table >= 1000")
    tx, rx, mult = [config[k] for k in ("bfd-min-tx-ms", "bfd-min-rx-ms", "bfd-multiplier")]
    if not (50 <= tx <= 60000 and 50 <= rx <= 60000 and 1 <= mult <= 255):
        raise InvalidIntent("Invalid BFD timers")
    hold, keepalive = config["bgp-hold-time-seconds"], config["bgp-keepalive-time-seconds"]
    if not 1 <= keepalive <= 21845 or not 3 * keepalive <= hold <= 65535:
        raise InvalidIntent("BGP hold time must be at least three keepalive intervals")
    return table, tx, rx, mult, hold, keepalive


def resolve_contexts(config, sessions, advertisements, machine_id, imports):
    raw = json.loads(config.get("routing-contexts", "[]"))
    if not isinstance(raw, list):
        raise InvalidIntent("routing-contexts must be an array")
    if not raw:
        return ()
    if any(not isinstance(item, dict) for item in raw):
        raise InvalidIntent("routing-contexts must contain objects")
    contexts = [
        RoutingContext.model_validate(item)
        for item in raw
        if item.get("machine_id") in (None, machine_id)
    ]
    if not contexts:
        raise NotReady("Waiting for this host's routing contexts")
    protected = [
        IPv4Network(value.strip())
        for value in config.get("protected-prefixes", "").split(",")
        if value.strip()
    ]
    if not protected or not imports or any(p.prefixlen == 0 for p in imports):
        raise InvalidIntent("NUMA contexts require public imports and protected-prefixes")
    if any(a.overlaps(b) for a in imports for b in protected):
        raise InvalidIntent(
            "Public destinations overlap protected management/replication prefixes"
        )
    if any(a.overlaps(b) for i, a in enumerate(imports) for b in imports[i + 1 :]):
        raise InvalidIntent("Public destination prefixes must not overlap")
    if any(s.numa_node is None for s in sessions):
        raise NotReady("NUMA locality is unknown for a host attachment")
    if any(not any(s.numa_node == c.numa_node for s in sessions) for c in contexts):
        raise InvalidIntent("Each context needs a verified local attachment")
    ids, tables, priorities, sources = set(), set(), set(), set()
    allowed = [
        IPv4Network(value.strip())
        for value in config["export-allowlist"].split(",")
        if value.strip()
    ]
    for context in contexts:
        if (
            not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", context.id)
            or context.id in ids
            or context.table in tables
            or not context.sources
        ):
            raise InvalidIntent("Context IDs, tables and source bindings must be unique")
        ids.add(context.id)
        tables.add(context.table)
        for source in context.sources:
            if (
                source.prefixlen != 32
                or source in sources
                or (
                    not config.get("_rack-mode")
                    and not any(source.subnet_of(prefix) for prefix in allowed)
                )
            ):
                raise InvalidIntent("Context sources must be unique authorized /32 prefixes")
            sources.add(source)
        occupied = set(
            range(
                context.rule_priority,
                context.rule_priority
                + len(context.sources) * (1 if config.get("_rack-mode") else len(imports)),
            )
        )
        if priorities & occupied or max(occupied) > 32000:
            raise InvalidIntent("Context rule priority ranges overlap or exceed 32000")
        priorities |= occupied
    grouped = {c.id: set() for c in contexts}
    by_id = {c.id: c for c in contexts}
    for doc in advertisements:
        for ad in doc.advertisements or ():
            if ad.machine_id != machine_id or ad.domain != config["routing-domain"]:
                continue
            if ad.context is None:
                raise NotReady("Advertisement must identify its NUMA routing context")
            if ad.context not in by_id or ad.prefix not in by_id[ad.context].sources:
                raise InvalidIntent("Advertisement does not match its declared source context")
            if ad.ready and any(ad.prefix.subnet_of(prefix) for prefix in allowed):
                grouped[ad.context].add(ad.prefix)
    return tuple(
        c.model_copy(update={"exports": tuple(sorted(grouped[c.id]))})
        for c in sorted(contexts, key=lambda c: c.id)
    )


def resolve_domains(uid, inventory, attachments, advertisements, local_links, config, raw):
    """Resolve domains independently, then enforce host-wide resource separation."""
    if json.loads(config.get("routing-contexts", "[]")):
        raise InvalidIntent("Do not combine routing-domains with legacy routing-contexts")
    domains = sorted((RoutingDomain.model_validate(item) for item in raw), key=lambda d: d.id)
    ids = [d.id for d in domains]
    if len(set(ids)) != len(ids) or any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", d) for d in ids):
        raise InvalidIntent("Routing domain IDs must be unique safe identifiers")
    if any(not d.contexts or not d.import_prefixes or not d.export_allowlist for d in domains):
        raise InvalidIntent(
            "Every routing domain requires contexts, imports and export allowlists"
        )
    for index, domain in enumerate(domains):
        for other in domains[index + 1 :]:
            if any(a.overlaps(b) for a in domain.import_prefixes for b in other.import_prefixes):
                raise InvalidIntent("Routing domain destination prefixes must not overlap")
            if any(a.overlaps(b) for a in domain.export_allowlist for b in other.export_allowlist):
                raise InvalidIntent("Routing domain export allowlists must not overlap")
    plans: list[Plan] = []
    for domain in domains:
        options = config | {
            "routing-domains": "[]",
            "routing-domain": domain.id,
            "routing-contexts": json.dumps([c.model_dump(mode="json") for c in domain.contexts]),
            "import-prefixes": ",".join(map(str, domain.import_prefixes)),
            "export-allowlist": ",".join(map(str, domain.export_allowlist)),
        }
        for field in (
            "bfd_min_tx_ms",
            "bfd_min_rx_ms",
            "bfd_multiplier",
            "bgp_hold_time_seconds",
            "bgp_keepalive_time_seconds",
        ):
            if (value := getattr(domain, field)) is not None:
                options[field.replace("_", "-")] = value
        plans.append(
            resolve_single(uid, inventory, attachments, advertisements, local_links, options)
        )
    sessions = [s for plan in plans for s in plan.sessions]
    if len({s.id for s in sessions}) != len(sessions):
        raise InvalidIntent("Session IDs must be unique across routing domains")
    if len({s.interface for s in sessions}) != len(sessions):
        raise InvalidIntent("Routing domains require separate verified attachment interfaces")
    tables, sources, priorities = set(), set(), set()
    destination_count = 1 if config.get("_rack-mode") else sum(len(p.imports) for p in plans)
    for plan in plans:
        for context in plan.contexts:
            occupied = set(
                range(
                    context.rule_priority,
                    context.rule_priority + len(context.sources) * destination_count,
                )
            )
            if context.table in tables or priorities & occupied or max(occupied) > 32000:
                raise InvalidIntent("Routing domains conflict in tables or policy priorities")
            if sources & set(context.sources):
                raise InvalidIntent("Routing domain source bindings must be unique")
            tables.add(context.table)
            sources.update(context.sources)
            priorities.update(occupied)
    return MultiDomainPlan(host=plans[0].host, domains=tuple(plans))


def resolve_rack(uid, inventory, attachments, advertisements, local_links, config):
    """The production entry point: exactly one rack and one multi-domain model."""
    if inventory.rack != config["rack"]:
        raise InvalidIntent("Inventory relation does not match this application's rack")
    raw = json.loads(config["routing-domains"])
    if not raw:
        raise InvalidIntent("Configure routing-domains, including contexts for this host")
    options = config | {"_rack-mode": True, "routing-contexts": "[]"}
    plan = resolve_domains(uid, inventory, attachments, advertisements, local_links, options, raw)
    if plan.host.rack != config["rack"]:
        raise InvalidIntent("Host is placed in a different physical rack")
    return plan
