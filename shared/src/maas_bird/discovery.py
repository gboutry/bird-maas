"""Read-only Linux attachment discovery, separate from BIRD routing policy."""

import json
import re
import subprocess
from dataclasses import dataclass
from ipaddress import IPv6Address
from pathlib import Path

from maas_bird import ovsdb
from maas_bird.models import Identity, OvsPort


class DiscoveryError(RuntimeError):
    pass


def command(argv: list[str]) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        # Do not include command outputs or arguments: these helpers are reused by workloads.
        raise DiscoveryError(f"Read-only command failed: {argv[0]}") from exc
    return result.stdout


def cards() -> dict[str, Identity]:
    """Return physical PCI functions with devlink-reported device serials."""
    raw = json.loads(command(["devlink", "-j", "dev", "info"]))
    return {
        key.removeprefix("pci/"): Identity(scheme="devlink-serial", value=value["serial_number"])
        for key, value in raw.get("info", {}).items()
        if key.startswith("pci/") and value.get("serial_number")
    }


def host_uuid(root: Path = Path("/sys")) -> str:
    value = (root / "class/dmi/id/product_uuid").read_text().strip().lower()
    if not value or value == "00000000-0000-0000-0000-000000000000":
        raise DiscoveryError("Host has no usable hardware UUID")
    return value


@dataclass(frozen=True)
class LocalLink:
    interface: str
    identity: Identity
    controller: int | None
    pf: int | None
    vf: int | None
    vlan: int | None
    link_local: IPv6Address | None
    numa_node: int | None = None
    ovs_port: OvsPort | None = None

    def __post_init__(self):
        if self.ovs_port is None:
            if self.controller is None or self.pf is None:
                raise ValueError("Physical local link requires native function coordinates")
        elif (
            any(value is not None for value in (self.controller, self.pf, self.vf))
            or not self.ovs_port.complete
            or self.vlan != self.ovs_port.vlan
        ):
            raise ValueError("Logical local link cannot invent physical coordinates")


def links(root: Path = Path("/sys")) -> list[LocalLink]:
    """Join physical card serials, virtfn indices and existing VLAN devices.

    No interfaces or link-local addresses are created. Ambiguous identities are rejected later.
    """
    try:
        serials = cards()
    except DiscoveryError:
        serials = {}
    addresses = json.loads(command(["ip", "-j", "-d", "address", "show"]))
    by_name = {item["ifname"]: item for item in addresses}
    result = []
    for name, address in by_name.items():
        info = address.get("linkinfo", {})
        vlan = info.get("info_data", {}).get("id") if info.get("info_kind") == "vlan" else None
        parent_name = address.get("link") if vlan is not None else name
        if vlan is not None and not parent_name:
            parent_name = next(
                (x["ifname"] for x in addresses if x["ifindex"] == address.get("link_index")), None
            )
        if not parent_name:
            continue
        device_link = root / "class/net" / parent_name / "device"
        if not device_link.exists():
            continue
        device = device_link.resolve()
        pf_path = device / "physfn"
        vf = None
        if pf_path.exists():
            pf = pf_path.resolve()
            candidates = [int(p.name[6:]) for p in pf.glob("virtfn*") if p.resolve() == device]
            if len(candidates) != 1:
                raise DiscoveryError("Cannot uniquely determine VF index")
            vf = candidates[0]
        else:
            pf = device
        identity = serials.get(pf.name)
        if identity is None:
            # Ordinary NIC backend: a permanent port MAC, not a mutable interface address.
            pf_netdevs = list((pf / "net").glob("*"))
            if len(pf_netdevs) != 1:
                continue
            output = command(["ethtool", "-P", pf_netdevs[0].name]).strip()
            match_mac = re.fullmatch(r"Permanent address: ([0-9a-fA-F:]{17})", output)
            if not match_mac or match_mac[1].lower() == "00:00:00:00:00:00":
                continue
            identity = Identity(scheme="permanent-mac", value=match_mac[1].lower())
        # Conventional PCI host functions only; controllers not discoverable here are unsupported.
        match = re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.([0-7])", pf.name)
        if not match:
            continue
        ll = [
            IPv6Address(x["local"])
            for x in address.get("addr_info", [])
            if x["family"] == "inet6"
            and x.get("scope") == "link"
            and not x.get("tentative", False)
            and not x.get("dadfailed", False)
        ]
        try:
            node = int((pf / "numa_node").read_text().strip())
            node = node if node >= 0 else None
        except (OSError, ValueError):
            node = None
        result.append(
            LocalLink(
                name,
                identity,
                0,
                int(match[1]),
                vf,
                vlan,
                ll[0] if len(ll) == 1 else None,
                node,
            )
        )
    return result


def dpu_observations() -> tuple[dict, dict]:
    """Collect standard devlink port metadata and LLDP; never modify OVS or the switch."""
    ports = json.loads(command(["devlink", "-j", "port", "show"]))
    neighbors = json.loads(command(["lldpcli", "-f", "json", "show", "neighbors"]))
    return ports, neighbors


def netdev_identity(interface, serials, root=Path("/sys")):
    """Find a physical netdev's card through PCI or auxiliary sysfs ancestry."""
    ovsdb.interface_name(interface)
    device = root / "class/net" / interface / "device"
    if not device.is_symlink():
        return None
    path = device.resolve()
    return next((serials[p.name] for p in (path, *path.parents) if p.name in serials), None)


def ovs_links(configured, root=Path("/sys")) -> list[LocalLink]:
    """Discover only explicitly selected, local-card OVS internal interfaces.

    This backend runs in the OVS kernel namespace, normally on the DPU itself.
    It never treats a host VF as a DPU internal interface. Ordinary hosts invoke
    no OVS commands unless the operator opts in with local-ovs-ports.
    """
    if not configured:
        return []
    serials = cards()
    local = {identity.value: identity for identity in serials.values()}
    selected = [item for item in configured if item.get("serial") in local]
    if not selected:
        return []
    state = ovsdb.snapshot(command)
    addresses = json.loads(command(["ip", "-j", "-d", "address", "show"]))
    result = []
    for item in selected:
        identity = local[item["serial"]]
        if set(item) - {"serial", "bridge", "port", "uplink", "uplink_bond"}:
            raise ValueError("Unknown local OVS attachment intent field")
        try:
            port = ovsdb.binding(
                state,
                item["bridge"],
                item["port"],
                item["uplink"],
                item["serial"],
                {pci: i.value for pci, i in serials.items()},
                root,
                bond=item.get("uplink_bond"),
            )
            matches = [a for a in addresses if a["ifname"] == item["port"]]
            if len(matches) != 1 or matches[0]["ifindex"] != port["ifindex"]:
                raise ovsdb.TopologyError("Internal interface no longer matches its kernel netdev")
        except ovsdb.TopologyError:
            continue  # Known absence revokes this binding, not unrelated paths.
        except OSError as exc:
            raise DiscoveryError("Cannot inspect native OVS uplink ownership") from exc
        link_local = [
            IPv6Address(a["local"])
            for a in matches[0].get("addr_info", [])
            if a["family"] == "inet6"
            and a.get("scope") == "link"
            and not a.get("tentative", False)
            and not a.get("dadfailed", False)
        ]
        result.append(
            LocalLink(
                item["port"],
                identity,
                None,
                None,
                None,
                port["vlan"],
                link_local[0] if len(link_local) == 1 else None,
                ovs_port=OvsPort.model_validate(port),
            )
        )
    if len({link.interface for link in result}) != len(result):
        raise ValueError("Duplicate local OVS interface selectors")
    return result


def ovs_uplink_bridge(
    interface: str,
    identity: Identity,
    serials: dict[str, Identity],
    root: Path = Path("/sys"),
    *,
    bond: str | None = None,
    state=None,
) -> str | None:
    """Resolve a direct uplink or explicitly approved same-card LACP aggregate.

    The kernel master and its single-interface OVS Port are both verified. A
    physical LLDP member is not treated as a separate OVS uplink. Read failures
    remain unknown; observed structural mismatches refuse readiness.
    """
    ovsdb.interface_name(interface)
    if bond is None:
        return command(["ovs-vsctl", "iface-to-br", interface]).strip()
    try:
        native = {pci: card.value for pci, card in serials.items()}
        ovsdb.kernel_uplink(interface, identity.value, native, root, bond=bond)
        state = ovsdb.snapshot(command) if state is None else state
        bridge_uuid, _ = ovsdb.uplink_binding(
            state, interface, identity.value, native, root, bond=bond
        )
        return next(row["name"] for row in state["Bridge"] if row["_uuid"] == bridge_uuid)
    except ovsdb.TopologyError:
        return None
    except OSError as exc:
        raise DiscoveryError("Cannot validate kernel LACP uplink membership") from exc


def lldp_chassis(neighbors: dict, interface: str) -> set[str]:
    """lldpd JSON may encode its interface collection as a dict or a list of dicts."""
    rows = neighbors.get("lldp", {}).get("interface", [])
    if isinstance(rows, dict):
        rows = [rows]
    found = set()
    for row in rows:
        value = row.get(interface)
        if not value:
            continue
        chassis = value.get("chassis", {})
        for details in chassis.values():
            identifier = details.get("id", {})
            if identifier.get("type") == "mac" and identifier.get("value"):
                found.add(identifier["value"].lower())
    return found
