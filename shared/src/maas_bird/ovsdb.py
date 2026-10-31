"""Small stdlib-only, read-only OVS snapshot parser shared with startup admission.

Names are selectors, not identity. An accepted internal port is bound to its
bridge, Port and Interface UUIDs and kernel ifindex. A recreated object must earn
new authority even when it reuses the same name and VLAN tag.
"""

import json
import re
from pathlib import Path
from uuid import UUID


class TopologyError(ValueError):
    """A complete native read proves that an attachment is not present as declared."""


def interface_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.:-]{0,14}", value):
        raise ValueError("Unsafe OVS interface name")
    return value


def _atom(value):
    if isinstance(value, list):
        if len(value) != 2:
            raise ValueError("Malformed OVSDB atom")
        if value[0] == "uuid":
            if not isinstance(value[1], str):
                raise ValueError("Malformed OVSDB UUID")
            return str(UUID(value[1]))
        if value[0] == "set":
            if not isinstance(value[1], list):
                raise ValueError("Malformed OVSDB set")
            return [_atom(item) for item in value[1]]
        raise ValueError("Unsupported OVSDB atom kind")
    return value


def binding(state, bridge, port, physical, serial, serials, root=Path("/sys"), *, bond=None):
    """One complete logical binding, reusable by discovery and stdlib startup."""
    result = internal_port(state, bridge, port)
    bridge_uuid, uplink = uplink_binding(state, physical, serial, serials, root, bond=bond)
    if bridge_uuid != result["bridge_uuid"]:
        raise TopologyError("Internal port and uplink belong to different bridges")
    return {**result, "uplink": uplink}


def _set(value):
    return value if isinstance(value, list) else [value]


def snapshot(execute):
    """Read all three tables in one ovs-vsctl invocation, with no mutation verbs."""
    text = execute(
        [
            "ovs-vsctl",
            "--timeout=5",
            "--format=json",
            "--columns=_uuid,name,ports",
            "list",
            "Bridge",
            "--",
            "--columns=_uuid,name,interfaces,tag,trunks,vlan_mode",
            "list",
            "Port",
            "--",
            "--columns=_uuid,name,type,ifindex,ofport,error",
            "list",
            "Interface",
        ]
    )
    decoder = json.JSONDecoder()
    tables = []
    expected = (
        {"_uuid", "name", "ports"},
        {"_uuid", "name", "interfaces", "tag", "trunks", "vlan_mode"},
        {"_uuid", "name", "type", "ifindex", "ofport", "error"},
    )
    for columns in expected:
        document, end = decoder.raw_decode(text.lstrip())
        text = text.lstrip()[end:]
        headings = document["headings"]
        if len(set(headings)) != len(headings) or set(headings) != columns:
            raise ValueError("Unexpected OVS snapshot columns")
        tables.append(
            [
                dict(zip(headings, (_atom(value) for value in row), strict=True))
                for row in document["data"]
            ]
        )
    if text.strip():
        raise ValueError("Unexpected OVS snapshot output")
    return dict(zip(("Bridge", "Port", "Interface"), tables, strict=True))


def _one(rows, key, value):
    found = [row for row in rows if row.get(key) == value]
    if len(found) != 1:
        raise TopologyError("OVS object is absent or ambiguous")
    return found[0]


def internal_port(state, bridge, port):
    """Validate one access-tagged internal interface and its complete native join."""
    interface_name(bridge)
    interface_name(port)
    b = _one(state["Bridge"], "name", bridge)
    p = _one(state["Port"], "name", port)
    if p["_uuid"] not in _set(b["ports"]):
        raise TopologyError("OVS port belongs to another bridge")
    members = _set(p["interfaces"])
    if len(members) != 1:
        raise TopologyError("Internal OVS port requires exactly one interface")
    i = _one(state["Interface"], "_uuid", members[0])
    tag = p["tag"]
    if isinstance(tag, list) and len(tag) == 1:
        tag = tag[0]
    if (
        i["name"] != port
        or i["type"] != "internal"
        or type(tag) is not int
        or not 1 <= tag <= 4094
        or p["trunks"] != []
        or p["vlan_mode"] not in ([], "access")
        or i["error"] not in ([], "")
    ):
        raise TopologyError("Unsupported OVS internal port topology")
    ifindex = i["ifindex"]
    if isinstance(ifindex, list) and len(ifindex) == 1:
        ifindex = ifindex[0]
    if (
        type(ifindex) is not int
        or ifindex <= 0
        or type(i["ofport"]) is not int
        or i["ofport"] <= 0
    ):
        raise TopologyError("OVS internal interface is not installed")
    return {
        "bridge": bridge,
        "port": port,
        "bridge_uuid": str(UUID(b["_uuid"])),
        "port_uuid": str(UUID(p["_uuid"])),
        "interface_uuid": str(UUID(i["_uuid"])),
        "ifindex": ifindex,
        "vlan": tag,
    }


def card_serial(interface, serials, root=Path("/sys")):
    interface_name(interface)
    device = root / "class/net" / interface / "device"
    if not device.is_symlink():
        return None
    path = device.resolve()
    return next((serials[p.name] for p in (path, *path.parents) if p.name in serials), None)


def kernel_uplink(physical, serial, serials, root=Path("/sys"), *, bond=None):
    """Prove static same-card membership without requiring both members to be up."""
    interface_name(physical)
    net = root / "class/net"
    if bond is None:
        if (net / physical / "master").exists():
            raise TopologyError("Direct uplink unexpectedly has a kernel master")
        members = [physical]
    else:
        interface_name(bond)
        if (net / physical / "master").resolve().name != bond:
            raise TopologyError("Physical uplink uses another kernel master")
        mode = (net / bond / "bonding/mode").read_text().split()
        members = (net / bond / "bonding/slaves").read_text().split()
        if mode != ["802.3ad", "4"] or len(members) < 2 or len(set(members)) != len(members):
            raise TopologyError("Declared uplink is not a distinct-member LACP bond")
        if physical not in members:
            raise TopologyError("Physical uplink is not a declared LACP member")
    for member in members:
        if bond and (net / member / "master").resolve().name != bond:
            raise TopologyError("LACP member belongs to another master")
        if card_serial(member, serials, root) != serial:
            raise TopologyError("Uplink member belongs to another physical card")
    return sorted(members)


def uplink_binding(state, physical, serial, serials, root=Path("/sys"), *, bond=None):
    """Bind the native kernel uplink to a single-interface OVS Port and Bridge."""
    members = kernel_uplink(physical, serial, serials, root, bond=bond)
    interface = _one(state["Interface"], "name", bond or physical)
    ports = [p for p in state["Port"] if interface["_uuid"] in _set(p["interfaces"])]
    if len(ports) != 1 or len(_set(ports[0]["interfaces"])) != 1:
        raise TopologyError("OVS uplink must be a single-interface port")
    port = ports[0]
    bridges = [b for b in state["Bridge"] if port["_uuid"] in _set(b["ports"])]
    if (
        len(bridges) != 1
        or interface["error"] not in ([], "")
        or type(interface["ofport"]) is not int
        or interface["ofport"] <= 0
    ):
        raise TopologyError("OVS uplink is absent, ambiguous or in error")
    return str(UUID(bridges[0]["_uuid"])), {
        "physical": physical,
        "bond": bond,
        "members": members,
        "serial": serial,
        "port_uuid": str(UUID(port["_uuid"])),
        "interface_uuid": str(UUID(interface["_uuid"])),
    }
