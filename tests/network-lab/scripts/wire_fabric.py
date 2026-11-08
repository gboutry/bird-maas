"""Apply Terraform's rack-port Netplan to an existing owned fabric VM.

Cloud-init consumes the same configuration on a fresh VM. Host bridge LLDP
forwarding requires separate host administration; this script never elevates.
"""

import json
from pathlib import Path

import yaml
from commission import PROJECT, ROOT, command

TORS = ("r1a", "r1b", "r2a", "r2b")
CARRIERS = ("nlab-probe-pf0", "nlab-probe-pf1", "nlab-r2-pf0", "nlab-r2-pf1")


def guest(*args):
    return command("lxc", "exec", "fabric", "--project", PROJECT, "--", *args)


def main():
    vm = json.loads(command("lxc", "query", f"/1.0/instances/fabric?project={PROJECT}"))
    if (
        vm["config"].get("user.maas-bird-owner") != "network-lab"
        or vm["config"]["limits.cpu"] != "2"
        or vm["config"]["limits.memory"] != "8GiB"
    ):
        raise RuntimeError("Expected the owned fabric VM with its 2-CPU / 8-GiB cap")
    for network in CARRIERS:
        info = json.loads(command("lxc", "query", f"/1.0/networks/{network}"))
        if (
            info["type"] != "bridge"
            or info["config"]["ipv4.address"] != "none"
            or info["config"]["ipv6.address"] != "none"
        ):
            raise RuntimeError("Expected an unnumbered rack carrier bridge")
        for uri in info["used_by"]:
            if not (uri.startswith("/1.0/instances/") and uri.endswith(f"?project={PROJECT}")):
                raise RuntimeError("Carrier has a consumer outside the lab project")
            instance = json.loads(command("lxc", "query", uri))
            if instance["config"].get("user.maas-bird-owner") != "network-lab":
                raise RuntimeError("Carrier has a consumer not owned by this lab")
    config = yaml.safe_load(vm["config"]["cloud-init.network-config"])
    if set(config["bridges"]) != {f"br-{t}" for t in TORS} or config["ethernets"]["management"][
        "addresses"
    ] != ["10.203.240.11/24"]:
        raise RuntimeError("Expected Terraform's four rack bridges and static management")
    path = ROOT / "runtime/fabric-network.yaml"
    path.write_text(yaml.safe_dump({"network": config}))
    command(
        "lxc",
        "file",
        "push",
        "--project",
        PROJECT,
        str(path),
        "fabric/etc/netplan/50-cloud-init.yaml",
        "--mode=0600",
    )
    cloud = yaml.safe_load(vm["config"]["cloud-init.user-data"])
    for tor in TORS:
        target = f"/etc/systemd/network/10-netplan-br-{tor}.netdev.d/20-lldp.conf"
        entry = next(f for f in cloud["write_files"] if f["path"] == target)
        if entry["content"] != "[Bridge]\nGroupForwardMask=16384\n":
            raise RuntimeError("Unexpected rack bridge LLDP forwarding policy")
        path = ROOT / f"runtime/lldp-{tor}.conf"
        path.write_text(entry["content"])
        guest("mkdir", "-p", str(Path(target).parent))
        command("lxc", "file", "push", "--project", PROJECT, str(path), "fabric" + target)
    guest("netplan", "generate")
    guest("netplan", "apply")
    for tor in TORS:
        guest("ip", "link", "set", "dev", "br-" + tor, "type", "bridge", "group_fwd_mask", "16384")
    host_masks = {
        n: int(Path(f"/sys/class/net/{n}/bridge/group_fwd_mask").read_text().strip(), 0)
        for n in CARRIERS
    }
    print(json.dumps({"fabric_ports": list(TORS), "host_lldp_masks": host_masks}))
    if any(not v & 16384 for v in host_masks.values()):
        print("Host LLDP forwarding is pending; native attachment readiness is not established.")


if __name__ == "__main__":
    main()
