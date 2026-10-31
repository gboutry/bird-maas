"""Read-only attachment provider. Configuration declares intent; observations validate it."""

import json
from typing import Any

from maas_bird import discovery, ovsdb
from maas_bird.models import Attachment, Attachments, Card, Function, OvsPort


def observe(configured: list[dict[str, Any]]) -> Attachments:
    serials = discovery.cards()
    ports, neighbors = discovery.dpu_observations()
    ovs_state = (
        ovsdb.snapshot(discovery.command)
        if any(item.get("uplink_bond") and item.get("ovs_port") is None for item in configured)
        else None
    )
    by_serial = {}
    for item in configured:
        serial = item["serial"]
        identity = next(
            (identity for identity in serials.values() if identity.value == serial), None
        )
        if identity is None:
            raise ValueError("Configured card serial is not present on this DPU")
        if item.get("ovs_port") is not None:
            if item.get("function") is not None or item.get("host_function") is not None:
                raise ValueError("Declare either physical coordinates or an OVS internal port")
            by_serial.setdefault(serial, (identity, []))[1].append(
                _logical_attachment(item, neighbors)
            )
            continue
        coordinates = Function.model_validate(item["function"])
        # DPU representor coordinates and host PCI coordinates are different
        # namespaces. The provisioning owner must declare their mapping explicitly.
        host_coordinates = Function.model_validate(item.get("host_function", item["function"]))
        if (
            host_coordinates.controller != 0
            or host_coordinates.pf is None
            or host_coordinates.vf is None
        ):
            raise ValueError("Use explicit canonical host PF/VF coordinates")
        matches = []
        for key, port in ports.get("port", {}).items():
            pci = key.split("/", 2)[1] if key.startswith("pci/") else None
            if pci not in serials or serials[pci].value != serial:
                continue
            if (port.get("controller"), port.get("pfnum"), port.get("vfnum")) == (
                coordinates.controller,
                coordinates.pf,
                coordinates.vf,
            ):
                matches.append(port)
        ready = (
            len(matches) == 1 and discovery.netdev_identity(item["uplink"], serials) == identity
        )
        chassis = item["chassis_id"].lower()
        ready = ready and discovery.lldp_chassis(neighbors, item["uplink"]) == {chassis}
        if ready:
            representor = matches[0].get("netdev")
            if not representor:
                ready = False
            else:
                bridge = discovery.command(["ovs-vsctl", "iface-to-br", representor]).strip()
                uplink_bridge = discovery.ovs_uplink_bridge(
                    item["uplink"],
                    identity,
                    serials,
                    bond=item.get("uplink_bond"),
                    state=ovs_state,
                )
                trunks = discovery.command(["ovs-vsctl", "get", "Port", representor, "trunks"])
                # Require explicit VLAN membership; do not assume an empty trunk list means safe.

                ready = bridge == uplink_bridge and item["vlan"] in json.loads(trunks)
        attachment = Attachment(
            id=item["id"],
            function=host_coordinates,
            observed_function=coordinates,
            domain=item["domain"],
            vlan=item["vlan"],
            chassis_id=chassis,
            peer_link_local=item["peer_link_local"],
            ready=ready,
        )
        if coordinates.controller is None or coordinates.pf is None or coordinates.vf is None:
            raise ValueError(
                "DPU provider currently requires explicit controller/PF/VF coordinates"
            )
        by_serial.setdefault(serial, (identity, []))[1].append(attachment)
    return Attachments(
        cards=tuple(
            Card(identity=identity, attachments=tuple(attachments))
            for identity, attachments in by_serial.values()
        )
    )


def _logical_attachment(item, neighbors):
    """An internal netdev is owned by this DPU kernel, not by a host PCI VF."""
    selector = item["ovs_port"]
    if set(selector) != {"bridge", "port"}:
        raise ValueError("Declare only bridge and port in an OVS attachment selector")
    ovsdb.interface_name(selector["bridge"])
    ovsdb.interface_name(selector["port"])
    local = {
        "bridge": selector["bridge"],
        "port": selector["port"],
        **{key: item[key] for key in ("serial", "uplink")},
    }
    if "uplink_bond" in item:
        local["uplink_bond"] = item["uplink_bond"]
    links = discovery.ovs_links([local])
    link = links[0] if len(links) == 1 else None
    return Attachment(
        id=item["id"],
        ovs_port=link.ovs_port if link else OvsPort.model_validate(selector),
        domain=item["domain"],
        vlan=item["vlan"],
        chassis_id=item["chassis_id"].lower(),
        peer_link_local=item["peer_link_local"],
        ready=(
            link is not None
            and link.vlan == item["vlan"]
            and discovery.lldp_chassis(neighbors, item["uplink"]) == {item["chassis_id"].lower()}
        ),
    )
