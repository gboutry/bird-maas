"""Run isolated logical OVN gateways inside the owned fabric VM.

Each namespace owns an independent OVS database, switch daemon and controller.
These are software gateway roles, not emulated DPU hardware or MAAS machines.
"""

import argparse
import ipaddress
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/network-lab/provider-src")
import provider_dataplane as fabric  # noqa: E402

OWNER = "network-lab-gateway"
STATE = Path("/opt/network-lab/gateways")
RUNTIME = Path("/run/network-lab/gateways")
TLS = Path("/etc/ovn/network-lab-gateways")
CONTROL_BRIDGE = "nlab-gw-ctl"


def nsrun(namespace, *args):
    return fabric.run("ip", "netns", "exec", namespace, *args)


def link_map(namespace=None, tor=None):
    if namespace:
        text = nsrun(namespace, "ip", "-j", "-d", "link", "show")
    elif tor:
        text = fabric.router(tor, "ip", "-j", "-d", "link", "show")
    else:
        text = fabric.run("ip", "-j", "-d", "link", "show")
    return {row["ifname"]: row for row in json.loads(text)}


def own_link(row, kind):
    if row.get("ifalias") != OWNER or row.get("linkinfo", {}).get("info_kind") != kind:
        raise RuntimeError("Gateway interface ownership or type does not match")


def namespace_ready(namespace, config):
    directory = STATE / namespace
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = directory / "owner.json"
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    old = json.loads(marker.read_text()) if marker.exists() else None
    if Path("/run/netns", namespace).exists():
        if old != {"config": config, "boot_id": boot}:
            raise RuntimeError("Existing gateway namespace is not owned by this allocation")
    else:
        if old is not None and (old["config"] != config or old["boot_id"] == boot):
            raise RuntimeError("A current or different gateway namespace marker is orphaned")
        fabric.run("ip", "netns", "add", namespace)
        marker.write_text(json.dumps({"config": config, "boot_id": boot}, indent=2) + "\n")
        marker.chmod(0o600)
    nsrun(
        namespace,
        "sysctl",
        "-w",
        "net.ipv6.conf.all.disable_ipv6=1",
        "net.ipv6.conf.default.disable_ipv6=1",
        "net.ipv4.conf.all.rp_filter=2",
        "net.ipv4.conf.default.rp_filter=2",
    )
    nsrun(namespace, "ip", "link", "set", "lo", "up")


def control_bridge(model):
    current = link_map()
    if CONTROL_BRIDGE in current:
        own_link(current[CONTROL_BRIDGE], "bridge")
    else:
        network = ipaddress.ip_network(model["control_network"])
        for route in json.loads(fabric.run("ip", "-j", "-4", "route", "show")):
            if route.get("dst") not in (None, "default") and network.overlaps(
                ipaddress.ip_network(route["dst"])
            ):
                raise RuntimeError("The gateway control subnet overlaps an existing fabric route")
        fabric.run("ip", "link", "add", CONTROL_BRIDGE, "type", "bridge")
        fabric.run("ip", "link", "set", CONTROL_BRIDGE, "alias", OWNER)
    fabric.run("ip", "link", "set", CONTROL_BRIDGE, "addrgenmode", "none", "up")
    fabric.run("ip", "address", "replace", model["control_router"], "dev", CONTROL_BRIDGE)


def pair(namespace, config, interface, remote, *, tor=None, mac):
    local = link_map(namespace=namespace)
    other = link_map(tor=tor)
    if (interface in local) != (remote in other):
        raise RuntimeError("Partial gateway veth exists; inspect it before rebuilding")
    if interface not in local:
        prefix = f"ng{config['rack']}{interface[0]}"
        first, second = prefix + "a", prefix + "b"
        root = link_map()
        if first in root or second in root:
            raise RuntimeError("An unexpected gateway staging interface exists")
        fabric.run("ip", "link", "add", first, "type", "veth", "peer", "name", second)
        for name in (first, second):
            fabric.run("ip", "link", "set", name, "alias", OWNER)
        if tor:
            pid = fabric.run(
                "docker", "inspect", "--format", "{{.State.Pid}}", "clab-maas-bird-fabric-" + tor
            )
            fabric.run("ip", "link", "set", first, "netns", pid)
            fabric.router(tor, "ip", "link", "set", first, "name", remote)
        else:
            fabric.run("ip", "link", "set", first, "name", remote)
        fabric.run("ip", "link", "set", second, "netns", namespace)
        nsrun(namespace, "ip", "link", "set", second, "name", interface)
        nsrun(namespace, "ip", "link", "set", interface, "address", mac)
    local, other = link_map(namespace=namespace), link_map(tor=tor)
    own_link(local[interface], "veth")
    own_link(other[remote], "veth")
    if (
        local[interface]["address"] != mac
        or local[interface].get("link_index") != other[remote]["ifindex"]
        or other[remote].get("link_index") != local[interface]["ifindex"]
    ):
        raise RuntimeError("Gateway MAC or native veth peer differs from the allocation")
    nsrun(namespace, "ip", "link", "set", interface, "mtu", "1500", "up")
    command = fabric.router if tor else lambda _node, *args: fabric.run(*args)
    command(tor, "ip", "link", "set", remote, "mtu", "1500", "up")


def configure_links(namespace, config, model):
    namespace_ready(namespace, config)
    rack, tor = config["rack"], config["tor"]
    pair(namespace, config, "control0", f"gw{rack}-ctl", mac=f"02:00:00:dc:{rack:02x}:03")
    fabric.run("ip", "link", "set", f"gw{rack}-ctl", "master", CONTROL_BRIDGE)
    nsrun(namespace, "ip", "address", "replace", config["control"], "dev", "control0")
    pair(namespace, config, "provider0", "eth4", tor=tor, mac=config["provider_mac"])
    bridge = link_map(tor=tor)["br-provider"]
    fabric.owned_link(bridge, "bridge")
    fabric.router(tor, "ip", "link", "set", "eth4", "master", "br-provider")
    pair(namespace, config, "transport0", "eth5", tor=tor, mac=f"02:00:00:dc:{rack:02x}:02")
    fabric.router(tor, "ip", "address", "replace", config["tor_transport"], "dev", "eth5")
    nsrun(namespace, "ip", "address", "replace", config["transport"], "dev", "transport0")
    nsrun(namespace, "ip", "address", "replace", config["vtep"], "dev", "lo")
    nsrun(namespace, "ip", "route", "replace", "unreachable", "default", "metric", "42760")
    nsrun(
        namespace,
        "ip",
        "route",
        "replace",
        "10.205.0.0/16",
        "via",
        config["tor_transport"].split("/")[0],
        "dev",
        "transport0",
        "src",
        config["vtep"].split("/")[0],
    )
    nsrun(
        namespace,
        "ip",
        "route",
        "replace",
        "10.203.240.11/32",
        "via",
        model["control_router"].split("/")[0],
        "dev",
        "control0",
    )


def process(namespace, component):
    path = RUNTIME / namespace / (component + ".pid")
    if not path.exists():
        return None
    pid = int(path.read_text().strip())
    directory = Path(f"/proc/{pid}")
    if not directory.exists():
        path.unlink()
        return None
    expected = Path("/run/netns", namespace).stat()
    actual = (directory / "ns/net").stat()
    if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino) or (
        directory / "exe"
    ).resolve().name != component:
        raise RuntimeError("Refusing to operate on a daemon outside its owned gateway namespace")
    return pid


def start_daemon(namespace, component, *args):
    if process(namespace, component) is not None:
        return
    directory = RUNTIME / namespace
    options = [
        "--detach",
        "--no-chdir",
        f"--pidfile={directory / (component + '.pid')}",
        f"--log-file={STATE / namespace / (component + '.log')}",
    ]
    if component != "ovn-controller":
        options.append(f"--unixctl={directory / (component + '.ctl')}")
    nsrun(namespace, "env", "OVS_RUNDIR=" + str(directory), component, *options, *args)
    if process(namespace, component) is None:
        raise RuntimeError("Native gateway daemon did not create its owned PID")


def start_switch(namespace, config, model):
    directory = RUNTIME / namespace
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    database = STATE / namespace / "conf.db"
    if not database.exists():
        fabric.run(
            "ovsdb-tool", "create", str(database), "/usr/share/openvswitch/vswitch.ovsschema"
        )
    connection = "unix:" + str(directory / "db.sock")
    start_daemon(namespace, "ovsdb-server", "--remote=p" + connection, str(database))

    def ovs(*args):
        return nsrun(namespace, "ovs-vsctl", "--db=" + connection, "--no-wait", *args)

    ovs("init")
    ovs("--may-exist", "add-br", "br-int", "--", "--may-exist", "add-br", "br-ex")
    ovs("set", "Bridge", "br-ex", "other_config:hwaddr=" + config["provider_mac"])
    ovs("--may-exist", "add-port", "br-ex", "provider0")
    ovs(
        "set",
        "Open_vSwitch",
        ".",
        "external_ids:system-id=" + namespace,
        "external_ids:hostname=" + namespace,
        "external_ids:rundir=" + str(directory),
        "external_ids:ovn-remote=ssl:10.203.240.11:6642",
        "external_ids:ovn-encap-type=geneve",
        "external_ids:ovn-encap-ip=" + config["vtep"].split("/")[0],
        "external_ids:ovn-set-local-ip=true",
        "external_ids:ovn-cms-options=enable-chassis-as-gw",
        "external_ids:ovn-bridge-mappings=" + model["logical_router"]["physnet"] + ":br-ex",
        "other_config:n-handler-threads=1",
        "other_config:n-revalidator-threads=1",
    )
    certificate = TLS / namespace
    for filename in ("key.pem", "cert.pem", "ca.crt"):
        path = certificate / filename
        if not path.is_file() or path.stat().st_uid != 0 or path.stat().st_mode & 0o077:
            raise RuntimeError("Gateway TLS material must be root-only")
    ovs(
        "set-ssl",
        str(certificate / "key.pem"),
        str(certificate / "cert.pem"),
        str(certificate / "ca.crt"),
    )
    start_daemon(namespace, "ovs-vswitchd", connection)
    start_daemon(
        namespace,
        "ovn-controller",
        "-p",
        str(certificate / "key.pem"),
        "-c",
        str(certificate / "cert.pem"),
        "-C",
        str(certificate / "ca.crt"),
        connection,
    )


def stop_gateway(namespace, config):
    if not Path("/run/netns", namespace).exists():
        return
    marker = json.loads((STATE / namespace / "owner.json").read_text())
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if marker != {"config": config, "boot_id": boot}:
        raise RuntimeError("Refusing to stop an unowned gateway namespace")
    for component in ("ovn-controller", "ovs-vswitchd", "ovsdb-server"):
        pid = process(namespace, component)
        if pid is None:
            continue
        if component == "ovn-controller":
            os.kill(pid, signal.SIGTERM)
        else:
            arguments = ["--cleanup"] if component == "ovs-vswitchd" else []
            fabric.run(
                "ovs-appctl",
                "--target=" + str(RUNTIME / namespace / (component + ".ctl")),
                "exit",
                *arguments,
            )
        deadline = time.monotonic() + 15
        while Path(f"/proc/{pid}").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Owned gateway daemon did not exit")
            time.sleep(0.1)
    for interface in ("provider0", "transport0"):
        links = link_map(namespace=namespace)
        if interface in links:
            own_link(links[interface], "veth")
            nsrun(namespace, "ip", "link", "delete", "dev", interface)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--name", choices=("nlab-gw-r1", "nlab-gw-r2"))
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--apply", action="store_true")
    operation.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0 or not Path("/opt/network-lab/host-fabric/topology.clab.yml").exists():
        raise RuntimeError("Run inside the owned fabric VM")
    model = json.loads(args.model.read_text())
    if set(model["gateways"]) != {"nlab-gw-r1", "nlab-gw-r2"} or model["hardware_emulation"]:
        raise ValueError("Unexpected logical gateway allocation")
    if ipaddress.ip_network(model["control_network"]) != ipaddress.ip_network("172.31.205.0/24"):
        raise ValueError("Unexpected private gateway control subnet")
    for namespace, config in model["gateways"].items():
        if config["tor"] != {"nlab-gw-r1": "r1a", "nlab-gw-r2": "r2a"}[namespace]:
            raise ValueError("Gateway ToR differs from its rack allocation")
        for key, pool, prefix in (
            ("vtep", "10.205.251.0/24", 32),
            ("transport", "10.205.210.0/24", 31),
            ("control", "172.31.205.0/24", 24),
        ):
            address = ipaddress.ip_interface(config[key])
            if address.ip not in ipaddress.ip_network(pool) or address.network.prefixlen != prefix:
                raise ValueError("Gateway address differs from its reserved pool")
        labels = json.loads(
            fabric.run(
                "docker",
                "inspect",
                "--format",
                "{{json .Config.Labels}}",
                "clab-maas-bird-fabric-" + config["tor"],
            )
        )
        if labels.get("containerlab") != "maas-bird-fabric":
            raise RuntimeError("Gateway attachment is outside the owned fabric")
    if args.stop:
        for namespace, config in model["gateways"].items():
            if args.name is None or namespace == args.name:
                stop_gateway(namespace, config)
    elif args.apply:
        fabric.run("modprobe", "openvswitch")
        control_bridge(model)
        for namespace, config in model["gateways"].items():
            if args.name is None or namespace == args.name:
                configure_links(namespace, config, model)
                start_switch(namespace, config, model)
    else:
        print("Logical gateway model and native ToR ownership checked; no dataplane changed")
        return
    print("Applied logical gateway operation; OVN binding and packet acceptance remain required")


if __name__ == "__main__":
    main()
