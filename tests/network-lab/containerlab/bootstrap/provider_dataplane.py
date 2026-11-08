"""Configure the owned fabric VM's provider VXLAN and isolated L2 probes.

Run inside the dedicated fabric VM after applying the EVPN configuration.
Network namespaces here are packet endpoints, not OVN gateway chassis.
"""

import argparse
import ipaddress
import json
import os
import re
import subprocess
import time
from pathlib import Path

OWNER = "network-lab-provider"
TORS = {"r1a": 11, "r1b": 12, "r2a": 13, "r2b": 14}
DIRECTORY = Path("/opt/network-lab/provider")


def run(*args):
    return subprocess.check_output(args, text=True, timeout=30).strip()


def router(node, *args):
    if node not in TORS:
        raise ValueError("Only the four owned ToRs can terminate the provider service")
    return run("docker", "exec", "clab-maas-bird-fabric-" + node, *args)


def links(node):
    return {
        row["ifname"]: row for row in json.loads(router(node, "ip", "-j", "-d", "link", "show"))
    }


def owned_link(row, kind):
    if row.get("ifalias") != OWNER or row.get("linkinfo", {}).get("info_kind") != kind:
        raise RuntimeError(
            "Refusing to change an interface without the expected ownership and type"
        )


def wait_for_vteps():
    deadline = time.monotonic() + 60
    while True:
        pending = []
        for node, last in TORS.items():
            addresses = json.loads(router(node, "ip", "-j", "-4", "address", "show", "dev", "lo"))
            if not any(
                a["local"] == f"10.205.250.{last}" and a["prefixlen"] == 32
                for interface in addresses
                for a in interface["addr_info"]
            ):
                pending.append(node)
        if not pending:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Native provider VTEP addresses are not ready: " + ",".join(pending)
            )
        time.sleep(0.2)


def tor_dataplane(node, model):
    current = links(node)
    if any(current[interface]["mtu"] < model["mtu"] + 50 for interface in ("eth1", "eth2")):
        raise RuntimeError("Routed core MTUs cannot carry the declared provider frames")
    for interface, kind, creation in (
        ("br-provider", "bridge", ("type", "bridge", "stp_state", "0", "mcast_snooping", "0")),
        (
            "vx3405",
            "vxlan",
            (
                "type",
                "vxlan",
                "id",
                str(model["vni"]),
                "local",
                f"10.205.250.{TORS[node]}",
                "dstport",
                str(model["vxlan_port"]),
                "nolearning",
                "ttl",
                "64",
            ),
        ),
    ):
        if interface in current:
            owned_link(current[interface], kind)
        else:
            router(node, "ip", "link", "add", "dev", interface, *creation)
            router(node, "ip", "link", "set", "dev", interface, "alias", OWNER)
        router(node, "ip", "link", "set", "dev", interface, "addrgenmode", "none")
        router(node, "ip", "link", "set", "dev", interface, "mtu", str(model["mtu"]))
    current = links(node)
    info = current["vx3405"]["linkinfo"]["info_data"]
    if (
        info["id"] != model["vni"]
        or info["local"] != f"10.205.250.{TORS[node]}"
        or info["port"] != model["vxlan_port"]
        or info["learning"]
        or current["vx3405"].get("master") not in (None, "br-provider")
    ):
        raise RuntimeError("Existing VXLAN configuration differs from the provider intent")
    router(node, "ip", "link", "set", "dev", "vx3405", "master", "br-provider")
    router(node, "bridge", "link", "set", "dev", "vx3405", "learning", "off")
    router(node, "ip", "link", "set", "dev", "br-provider", "up")
    router(node, "ip", "link", "set", "dev", "vx3405", "up")
    addresses = json.loads(router(node, "ip", "-j", "address", "show", "dev", "br-provider"))
    if addresses[0]["addr_info"]:
        raise RuntimeError("The provider bridge must remain unaddressed")


def endpoint(namespace, config, model):
    marker = DIRECTORY / (namespace + ".json")
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    previous = json.loads(marker.read_text()) if marker.exists() else None
    if Path("/run/netns", namespace).exists():
        if previous != {"config": config, "boot_id": boot_id}:
            raise RuntimeError("Refusing to reuse an unowned or different namespace")
    else:
        if previous is not None and (
            previous["config"] != config or previous["boot_id"] == boot_id
        ):
            raise RuntimeError(
                "A current or different namespace marker exists without its namespace"
            )
        run("ip", "netns", "add", namespace)
        marker.write_text(json.dumps({"config": config, "boot_id": boot_id}, indent=2) + "\n")
        marker.chmod(0o600)
    node = config["tor"]
    current = links(node)
    native = {
        r["ifname"]: r
        for r in json.loads(
            run("ip", "netns", "exec", namespace, "ip", "-j", "-d", "link", "show")
        )
    }
    if ("eth4" in current) != ("provider0" in native):
        raise RuntimeError("Partial endpoint link exists; inspect it before rebuilding")
    if "eth4" not in current:
        first, second = "npr-" + node, "npe-" + node
        if first in {r["ifname"] for r in json.loads(run("ip", "-j", "link", "show"))}:
            raise RuntimeError("An unexpected staging veth exists")
        run("ip", "link", "add", first, "type", "veth", "peer", "name", second)
        for interface in (first, second):
            run("ip", "link", "set", interface, "alias", OWNER)
        pid = run(
            "docker", "inspect", "--format", "{{.State.Pid}}", "clab-maas-bird-fabric-" + node
        )
        run("ip", "link", "set", first, "netns", pid)
        router(node, "ip", "link", "set", first, "name", "eth4")
        run("ip", "link", "set", second, "netns", namespace)
        run("ip", "netns", "exec", namespace, "ip", "link", "set", second, "name", "provider0")
        run(
            "ip",
            "netns",
            "exec",
            namespace,
            "ip",
            "link",
            "set",
            "provider0",
            "address",
            config["mac"],
        )
    current = links(node)
    owned_link(current["eth4"], "veth")
    if current["eth4"].get("master") not in (None, "br-provider"):
        raise RuntimeError("The provider access port is attached to another bridge")
    router(node, "ip", "link", "set", "eth4", "master", "br-provider")
    router(node, "ip", "link", "set", "eth4", "mtu", str(model["mtu"]), "up")
    native = json.loads(
        run("ip", "netns", "exec", namespace, "ip", "-j", "-d", "link", "show", "dev", "provider0")
    )[0]
    owned_link(native, "veth")
    if native["address"] != config["mac"]:
        raise RuntimeError("Existing provider endpoint MAC differs from its allocation")
    for interface in ("lo", "provider0"):
        run("ip", "netns", "exec", namespace, "ip", "link", "set", interface, "up")
    run(
        "ip",
        "netns",
        "exec",
        namespace,
        "ip",
        "link",
        "set",
        "provider0",
        "mtu",
        str(model["mtu"]),
    )
    addressed = json.loads(
        run(
            "ip",
            "netns",
            "exec",
            namespace,
            "ip",
            "-j",
            "-4",
            "address",
            "show",
            "dev",
            "provider0",
        )
    )
    existing = [address for interface in addressed for address in interface["addr_info"]]
    if existing and {f"{a['local']}/{a['prefixlen']}" for a in existing} != {config["address"]}:
        raise RuntimeError("Existing endpoint addresses differ from the allocation")
    run(
        "ip",
        "netns",
        "exec",
        namespace,
        "ip",
        "address",
        "replace",
        config["address"],
        "dev",
        "provider0",
    )


def detach_endpoint(namespace, config):
    marker = DIRECTORY / (namespace + ".json")
    previous = json.loads(marker.read_text()) if marker.exists() else None
    if not Path("/run/netns", namespace).exists():
        if previous is not None and previous["config"] != config:
            raise RuntimeError("Refusing to detach a different endpoint allocation")
        return
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if previous != {"config": config, "boot_id": boot_id}:
        raise RuntimeError("Refusing to detach an unowned namespace")
    native = {
        r["ifname"]: r
        for r in json.loads(
            run("ip", "netns", "exec", namespace, "ip", "-j", "-d", "link", "show")
        )
    }
    if "provider0" in native:
        owned_link(native["provider0"], "veth")
        if native["provider0"]["address"] != config["mac"]:
            raise RuntimeError("Refusing to detach a different endpoint MAC")
        run("ip", "netns", "exec", namespace, "ip", "link", "delete", "dev", "provider0")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--detach", action="store_true")
    args = parser.parse_args()
    if args.apply and args.detach:
        parser.error("Apply and detach are separate operations")
    os.umask(0o077)
    if os.geteuid() != 0 or not Path("/opt/network-lab/host-fabric/topology.clab.yml").exists():
        raise RuntimeError("Run through the owned fabric VM's root execution channel")
    model = json.loads(args.model.read_text())
    if (model["vni"], model["route_target"], model["mtu"], model["vxlan_port"]) != (
        3405,
        "65000:3405",
        1500,
        4789,
    ) or set(model["endpoints"]) != {"nlab-provider-r1", "nlab-provider-r2"}:
        raise ValueError("Unexpected provider service allocation")
    addresses, macs = set(), set()
    for namespace, config in model["endpoints"].items():
        address = ipaddress.ip_interface(config["address"])
        if (
            config["tor"] != {"nlab-provider-r1": "r1b", "nlab-provider-r2": "r2b"}[namespace]
            or address.network != ipaddress.ip_network("192.0.2.0/24")
            or address.ip in {address.network.network_address, address.network.broadcast_address}
            or re.fullmatch(r"02:00:00:ee:[0-9a-f]{2}:[0-9a-f]{2}", config["mac"]) is None
        ):
            raise ValueError("Provider endpoint differs from the reserved lab allocation")
        addresses.add(address.ip)
        macs.add(config["mac"])
    if len(addresses) != 2 or len(macs) != 2:
        raise ValueError("Provider endpoints must have distinct IP and MAC identities")
    for node in TORS:
        labels = json.loads(
            run(
                "docker",
                "inspect",
                "--format",
                "{{json .Config.Labels}}",
                "clab-maas-bird-fabric-" + node,
            )
        )
        if labels.get("containerlab") != "maas-bird-fabric":
            raise RuntimeError("Provider targets must belong to the owned Containerlab topology")
    if args.detach:
        for namespace, config in model["endpoints"].items():
            detach_endpoint(namespace, config)
        print("Detached owned provider endpoint veths; namespace ownership retained")
        return
    if not args.apply:
        print("Provider model and four native router identities checked; no dataplane changed")
        return
    DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
    wait_for_vteps()
    for node in TORS:
        tor_dataplane(node, model)
    for namespace, config in model["endpoints"].items():
        endpoint(namespace, config, model)
    print(
        "Configured native provider VXLAN and two L2 endpoints; packet acceptance remains required"
    )


if __name__ == "__main__":
    main()
