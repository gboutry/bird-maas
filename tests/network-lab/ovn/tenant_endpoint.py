"""Recreate the owned workload namespace after native host networking starts."""

import json
import re
import subprocess
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from uuid import UUID


def run(*args):
    return subprocess.check_output(args, text=True, timeout=30).strip()


def main():
    config = json.loads(Path("/etc/ovn/network-lab/tenant.json").read_text())
    identity = str(UUID(config["uuid"]))
    if Path("/sys/class/dmi/id/product_uuid").read_text().strip().lower() != identity:
        raise RuntimeError("Tenant configuration belongs to another native host")
    if (
        run("ovs-vsctl", "get", "Open_vSwitch", ".", "external_ids:system-id").strip('"')
        != identity
    ):
        raise RuntimeError("OVN chassis identity differs from native host identity")
    if (
        run("ovs-vsctl", "get", "Open_vSwitch", ".", "external_ids:ovn-encap-ip").strip('"')
        != config["vtep"]
    ):
        raise RuntimeError("OVN VTEP differs from the owned endpoint configuration")
    run("systemctl", "is-active", "--quiet", "bird.service")
    address = str(IPv4Address(config["address"]))
    gateway = IPv4Address(config["gateway"]) if config.get("gateway") else None
    if gateway is not None:
        network = IPv4Network(address + "/24", strict=False)
        if gateway not in network or gateway in {
            IPv4Address(address),
            network.network_address,
            network.broadcast_address,
        }:
            raise ValueError("Tenant gateway must be another host in its declared subnet")
    if re.fullmatch(r"02:00:00:ab:00:[0-9a-f]{2}", config["mac"]) is None:
        raise ValueError("Unexpected tenant MAC allocation")
    if re.fullmatch(r"tenant-[a-z0-9-]+", config["port"]) is None:
        raise ValueError("Unexpected logical port name")
    namespaces = {line.split()[0] for line in run("ip", "netns", "list").splitlines()}
    if "nlab-tenant" not in namespaces:
        run("ip", "netns", "add", "nlab-tenant")
    host_links = {i["ifname"]: i for i in json.loads(run("ip", "-d", "-j", "link", "show"))}
    peer_links = {
        i["ifname"]: i
        for i in json.loads(
            run("ip", "netns", "exec", "nlab-tenant", "ip", "-d", "-j", "link", "show")
        )
    }
    host, peer = host_links.get("nlab-tenant"), peer_links.get("nlab-peer")
    if host is None and peer is None:
        run("ip", "link", "add", "nlab-tenant", "type", "veth", "peer", "name", "nlab-peer")
        run("ip", "link", "set", "nlab-peer", "netns", "nlab-tenant")
    else:
        if host is None or peer is None:
            raise RuntimeError("A partial tenant veth pair requires inspection")
        if any(link.get("linkinfo", {}).get("info_kind") != "veth" for link in (host, peer)):
            raise RuntimeError("Existing tenant links are not a veth pair")
        if host.get("link_index") != peer["ifindex"] or peer.get("link_index") != host["ifindex"]:
            raise RuntimeError("Existing tenant links do not peer with each other")
    run("ip", "link", "set", "nlab-tenant", "mtu", "1400", "up")
    run(
        "ip",
        "netns",
        "exec",
        "nlab-tenant",
        "ip",
        "link",
        "set",
        "nlab-peer",
        "address",
        config["mac"],
        "mtu",
        "1400",
        "up",
    )
    run("ip", "netns", "exec", "nlab-tenant", "ip", "link", "set", "lo", "up")
    run(
        "ip",
        "netns",
        "exec",
        "nlab-tenant",
        "ip",
        "address",
        "replace",
        address + "/24",
        "dev",
        "nlab-peer",
    )
    run(
        "ovs-vsctl",
        "--may-exist",
        "add-port",
        "br-int",
        "nlab-tenant",
        "--",
        "set",
        "Interface",
        "nlab-tenant",
        "external_ids:iface-id=" + config["port"],
    )
    if gateway is not None:
        run(
            "ip",
            "netns",
            "exec",
            "nlab-tenant",
            "ip",
            "route",
            "replace",
            "default",
            "via",
            str(gateway),
            "dev",
            "nlab-peer",
        )


if __name__ == "__main__":
    main()
