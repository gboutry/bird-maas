"""Read-only host NIC attachment observations through the host-attachments contract."""

import json
import re
from collections import defaultdict

from maas_bird import discovery, ovsdb
from maas_bird.models import Attachment, Attachments, Card, Function, Identity

_MAC = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")
_FIELDS = {
    "identity",
    "function",
    "id",
    "domain",
    "vlan",
    "chassis_id",
    "peer_link_local",
    "uplink",
}


def _intent(item):
    if set(item) != _FIELDS:
        raise ValueError("Declare only the complete native NIC attachment policy")
    identity = Identity.model_validate(item["identity"])
    if (
        identity.scheme not in {"permanent-mac", "devlink-serial"}
        or identity.value is None
        or identity.value.strip() == ""
    ):
        raise ValueError("Declare a native permanent MAC or devlink serial identity")
    if identity.scheme == "permanent-mac":
        value = identity.value.lower()
        if not _MAC.fullmatch(value) or value == "00:00:00:00:00:00":
            raise ValueError("Declare a usable permanent MAC")
        identity = Identity(scheme=identity.scheme, value=value)
    function = Function.model_validate(item["function"])
    if function.controller != 0 or function.pf is None:
        raise ValueError("Declare native host controller/PF coordinates")
    attachment = Attachment.model_validate(
        {key: value for key, value in item.items() if key not in {"identity", "uplink"}}
        | {"ready": False}
    )
    if not all(
        (
            attachment.id,
            attachment.domain,
            attachment.vlan,
            attachment.chassis_id,
            attachment.peer_link_local,
        )
    ):
        raise ValueError("Declare complete attachment, domain, VLAN and peer intent")
    chassis = str(attachment.chassis_id).lower()
    if not _MAC.fullmatch(chassis):
        raise ValueError("Declare the approved MAC-type LLDP chassis")
    return (
        identity,
        attachment.model_copy(update={"chassis_id": chassis}),
        ovsdb.interface_name(item["uplink"]),
    )


def observe(configured) -> Attachments:
    intents = [_intent(item) for item in configured]
    identifiers = set()
    selectors = set()
    for identity, attachment, _ in intents:
        identifier = (identity, attachment.id)
        selector = (identity, attachment.function, attachment.vlan)
        if identifier in identifiers or selector in selectors:
            raise ValueError("Duplicate native attachment identity or selector")
        identifiers.add(identifier)
        selectors.add(selector)
    if not intents:
        return Attachments(cards=())
    try:
        links = discovery.links()
        neighbors = json.loads(discovery.command(["lldpcli", "-f", "json", "show", "neighbors"]))
        if not isinstance(neighbors, dict) or not isinstance(neighbors.get("lldp"), dict):
            raise ValueError("Missing native LLDP snapshot")
        rows = neighbors["lldp"].get("interface", [])
        if not isinstance(rows, (list, dict)):
            raise ValueError("Malformed native LLDP interfaces")
        local = defaultdict(list)
        for link in links:
            local[link.identity].append(link)
        cards = defaultdict(list)
        for identity, intent, uplink in intents:
            if identity not in local:
                continue
            matches = [
                link
                for link in local[identity]
                if Function(controller=link.controller, pf=link.pf, vf=link.vf) == intent.function
                and link.vlan == intent.vlan
            ]
            parents = {link.parent_interface for link in local[identity] if link.vf is None}
            ambiguous_mac = identity.scheme == "permanent-mac" and len(parents) != 1
            link = matches[0] if len(matches) == 1 and not ambiguous_mac else None
            chassis = discovery.lldp_chassis(neighbors, uplink)
            ready = (
                link is not None
                and link.ovs_port is None
                and link.parent_interface == uplink
                and link.link_up is True
                and link.link_local is not None
                and link.link_local.is_link_local
                and not link.link_local.scope_id
                and chassis == {intent.chassis_id}
            )
            cards[identity].append(
                intent.model_copy(
                    update={
                        "observed_function": (
                            Function(controller=link.controller, pf=link.pf, vf=link.vf)
                            if link is not None
                            else None
                        ),
                        "ready": ready,
                    }
                )
            )
        return Attachments(
            cards=tuple(
                Card(identity=identity, attachments=tuple(attachments))
                for identity, attachments in cards.items()
            )
        )
    except discovery.DiscoveryError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise discovery.DiscoveryError("Native attachment observation is unavailable") from error
