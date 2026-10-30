"""Read-only attachment provider. Configuration declares intent; observations validate it."""

from maas_bird import discovery
from maas_bird.models import Attachment, Attachments, Card, Function


def observe(configured: list[dict]) -> Attachments:
    serials = discovery.cards()
    ports, neighbors = discovery.dpu_observations()
    by_serial = {}
    for item in configured:
        serial = item["serial"]
        identity = next(
            (identity for identity in serials.values() if identity.value == serial), None
        )
        if identity is None:
            raise ValueError("Configured card serial is not present on this DPU")
        coordinates = Function.model_validate(item["function"])
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
        ready = len(matches) == 1
        chassis = item["chassis_id"].lower()
        ready = ready and discovery.lldp_chassis(neighbors, item["uplink"]) == {chassis}
        if ready:
            representor = matches[0].get("netdev")
            if not representor:
                ready = False
            else:
                bridge = discovery.command(["ovs-vsctl", "iface-to-br", representor]).strip()
                uplink_bridge = discovery.command(
                    ["ovs-vsctl", "iface-to-br", item["uplink"]]
                ).strip()
                trunks = discovery.command(["ovs-vsctl", "get", "Port", representor, "trunks"])
                # Require explicit VLAN membership; do not assume an empty trunk list means safe.
                import json

                ready = bridge == uplink_bridge and item["vlan"] in json.loads(trunks)
        attachment = Attachment(
            id=item["id"],
            function=coordinates,
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
