"""Rack-scoped delivery, with host-local selection before payload validation.

Relations are an operator-established trust boundary. A rack request selects the
public inventory slice; it never grants access to MAAS credentials. A relation's
rack is immutable after first publication. Reintegrate to change its scope.
"""

import json
import re
from collections.abc import MutableMapping

from maas_bird.interfaces import MAX_FIELD_BYTES, decode
from maas_bird.models import Advertisements, Attachments, Identity, Inventory, RackRequest


class ScopeError(ValueError):
    """A producer's declared scope conflicts with its relation or payload."""


def rack_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", value):
        raise ScopeError("Set rack to a safe physical rack ID")
    return value


def inventory_slice(inventory: Inventory, request: RackRequest) -> Inventory:
    rack = rack_id(request.rack)
    return Inventory(
        rack=rack,
        hosts=None
        if inventory.hosts is None
        else tuple(h for h in inventory.hosts if h.rack == rack),
    )


def advertisements_for_host(bag: MutableMapping[str, str], rack: str, machine_id: str):
    """Unknown/unrelated producers cannot stall this host's authorization updates.

    The stable header is published before a payload. Missing payload for a matching
    host remains unknown, while [] explicitly revokes its exports. Never infer an
    empty authorized snapshot from a producer that has not identified itself.
    """
    if _header(bag, "rack") != rack or _header(bag, "machine_id") != machine_id:
        return None
    document = decode(Advertisements, bag)
    if not document.domain:
        raise ScopeError("Workload snapshot must identify one routing domain")
    if any(
        a.machine_id != machine_id or a.domain != document.domain
        for a in document.advertisements or ()
    ):
        raise ScopeError("Advertisement payload escapes its declared host scope")
    records = document.advertisements
    if records is not None:
        if any(ad.prefix is None or ad.ready is None or ad.context is None for ad in records):
            raise ValueError("Local advertisement snapshot is incomplete")
        if len({ad.prefix for ad in records}) != len(records):
            raise ScopeError("Conflicting local advertisement records")
    return document


def attachments_for_host(bag: MutableMapping[str, str], rack: str, identities: set[Identity]):
    if _header(bag, "rack") != rack:
        return None
    # Only inspect identities until this host has a matching card. An invalid
    # payload for an unrelated card must not poison every unit in the rack.
    try:
        if len(bag.get("cards", "").encode()) > MAX_FIELD_BYTES:
            return None
        raw = json.loads(bag.get("cards", "null"))
        if not isinstance(raw, list):
            return None
        related = any(
            isinstance(card, dict)
            and isinstance(card.get("identity"), dict)
            and (card["identity"].get("scheme"), card["identity"].get("value"))
            in {(identity.scheme, identity.value) for identity in identities}
            for card in raw
        )
    except (ValueError, TypeError, RecursionError):
        return None
    if not related:
        return None
    document = decode(Attachments, bag)
    for card in document.cards or ():
        if card.identity not in identities or card.attachments is None:
            continue
        if any(
            not attachment.id
            or not attachment.domain
            or attachment.function is None
            or attachment.function.controller is None
            or attachment.function.pf is None
            or attachment.vlan is None
            or not attachment.chassis_id
            or attachment.peer_link_local is None
            or attachment.ready is None
            for attachment in card.attachments
        ):
            raise ValueError("Local attachment snapshot is incomplete")
    return document


def _header(bag, field):
    try:
        if len(bag.get(field, "").encode()) > MAX_FIELD_BYTES:
            return None
        return json.loads(bag.get(field, "null"))
    except (TypeError, ValueError, RecursionError):
        return None
