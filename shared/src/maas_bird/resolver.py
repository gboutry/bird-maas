"""Resolve generic inventory and attachments into a locally verified routing plan."""

import re
from ipaddress import IPv4Network

from maas_bird.discovery import LocalLink
from maas_bird.models import Advertisements, Attachments, Inventory, Plan, Session


class NotReady(RuntimeError):
    """Inputs are incomplete; preserve an existing working configuration."""


class InvalidIntent(ValueError):
    """Inputs conflict or violate the supported routing policy."""


def resolve(
    uid: str,
    inventory: Inventory,
    attachments: list[Attachments],
    advertisements: list[Advertisements],
    local_links: list[LocalLink],
    config: dict,
) -> Plan:
    hosts = [h for h in inventory.hosts or () if h.system_uuid == uid.lower()]
    if len(hosts) != 1:
        raise (
            NotReady("Waiting for one matching MAAS host")
            if not hosts
            else InvalidIntent("Multiple inventory records match this host")
        )
    host = hosts[0]
    if not all([host.machine_id, host.local_asn, host.router_id, host.peers]):
        raise NotReady("Host routing identity is incomplete")
    peers = {p.chassis_id.lower(): p for p in host.peers if p.chassis_id}
    if len(peers) != len(host.peers):
        raise InvalidIntent("Rack peer identities are missing or duplicated")
    if any(not p.asn or not p.id for p in peers.values()):
        raise NotReady("Rack peer configuration is incomplete")
    sessions = []
    serial_producers = {}
    for producer, document in enumerate(attachments):
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
                if attachment.domain != config["routing-domain"]:
                    continue
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
                sessions.append(
                    Session(
                        id=attachment.id,
                        interface=link.interface,
                        peer=attachment.peer_link_local,
                        local=link.link_local,
                        remote_asn=peer.asn,
                    )
                )
    if len(sessions) != len(peers):
        raise NotReady("Waiting for one attachment per approved TOR")
    if len({s.interface for s in sessions}) != len(sessions):
        raise InvalidIntent("Multiple sessions resolve to the same interface")
    if not advertisements or any(d.advertisements is None for d in advertisements):
        raise NotReady("Waiting for complete advertisement snapshots")
    prefixes = set()
    for document in advertisements:
        for a in document.advertisements or ():
            if a.machine_id != host.machine_id or a.domain != config["routing-domain"]:
                continue
            if a.ready is None or a.prefix is None:
                raise NotReady("Advertisement is incomplete")
            if a.ready:
                if a.prefix.prefixlen != 32:
                    raise InvalidIntent("Only locally owned IPv4 /32 advertisements are supported")
                prefixes.add(a.prefix)
    imports = tuple(
        IPv4Network(p.strip()) for p in config["import-prefixes"].split(",") if p.strip()
    )
    allowed = [IPv4Network(p.strip()) for p in config["export-allowlist"].split(",") if p.strip()]
    if any(not any(prefix.subnet_of(network) for network in allowed) for prefix in prefixes):
        raise InvalidIntent("Advertisement is outside the operator export allowlist")
    table = config["kernel-table"]
    if not 1000 <= table <= 4294967295:
        raise InvalidIntent("Use a dedicated kernel routing table >= 1000")
    tx, rx, mult = [config[k] for k in ("bfd-min-tx-ms", "bfd-min-rx-ms", "bfd-multiplier")]
    if not (50 <= tx <= 60000 and 50 <= rx <= 60000 and 1 <= mult <= 255):
        raise InvalidIntent("Invalid BFD timers")
    hold, keepalive = config["bgp-hold-time-seconds"], config["bgp-keepalive-time-seconds"]
    if not 1 <= keepalive <= 21845 or not 3 * keepalive <= hold <= 65535:
        raise InvalidIntent("BGP hold time must be at least three keepalive intervals")
    return Plan(
        host=host,
        sessions=tuple(sorted(sessions, key=lambda x: x.interface)),
        prefixes=tuple(sorted(prefixes)),
        imports=imports,
        kernel_table=table,
        bfd_tx=tx,
        bfd_rx=rx,
        bfd_multiplier=mult,
        hold_time=hold,
        keepalive_time=keepalive,
    )
