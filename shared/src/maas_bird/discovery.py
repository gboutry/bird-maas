"""Read-only Linux attachment discovery, separate from BIRD routing policy."""

import json
import re
import subprocess
from dataclasses import dataclass
from ipaddress import IPv6Address
from pathlib import Path

from maas_bird.models import Identity


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
    controller: int
    pf: int
    vf: int | None
    vlan: int | None
    link_local: IPv6Address | None
    numa_node: int | None = None


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
