"""Render private FRR peer configuration from real, accepted BIRD plan actions."""

import argparse
import json
import re
from ipaddress import IPv4Network
from pathlib import Path

import jubilant
import yaml
from commission import HOSTS, PROJECT, ROOT, command
from juju_workers import worker_binding
from maas_bird.models import MultiDomainPlan, sessions_for_context
from maas_bird.runtime import write_atomic

NODES = {"spine0": 1, "spine1": 2, "r1a": 11, "r1b": 12, "r2a": 13, "r2b": 14}
WORKERS = {"probe": "r1a", "probe-alt": "r1b", "probe-r2a": "r2a", "probe-r2b": "r2b"}


def base_configs():
    configs = {node: (ROOT / f"containerlab/frr/{node}.conf").read_text() for node in NODES}
    for tor in WORKERS.values():
        for vlan in range(3400, 3405):
            configs[tor] += f"\ninterface eth3.{vlan}\n ipv6 address fe80::1/64\nexit\n"
    return configs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--passwords", type=Path, default=ROOT / "runtime/bgp/credentials.json")
    parser.add_argument("--output", type=Path, default=ROOT / "runtime/host-fabric")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / "runtime").resolve()):
        raise ValueError("Private generated configuration must stay under ignored runtime storage")
    if args.passwords.stat().st_mode & 0o077:
        raise ValueError("BGP password file must not be accessible to other users")
    passwords = json.loads(args.passwords.read_text())
    if not all(
        isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v) for v in passwords.values()
    ):
        raise ValueError("Expected generated hexadecimal BGP passwords")
    juju = jubilant.Juju(model="network-lab:network-lab")
    plans = {}
    for name, suffix in WORKERS.items():
        instance = json.loads(command("lxc", "query", f"/1.0/instances/{name}?project={PROJECT}"))
        if instance["config"].get("user.maas-bird-owner") != "network-lab":
            raise RuntimeError("Expected an owned worker")
        plan = MultiDomainPlan.model_validate_json(
            juju.run(worker_binding(juju, name, "bird-" + suffix).unit, "plan").results["plan"]
        )
        if (
            plan.host.system_uuid != instance["config"]["volatile.uuid"]
            or str(plan.host.router_id) != HOSTS[name]["loopback"].split("/")[0]
            or plan.advertisement_mode != "node-addresses"
        ):
            raise RuntimeError(
                "Native plan does not match the owned worker and node-address intent"
            )
        plans[name] = plan
    session_ids = {s.id for p in plans.values() for d in p.domains for s in d.sessions}
    if set(passwords) != session_ids:
        raise ValueError("Supply exactly one password for each accepted native session")
    origins = sorted({str(p) for plan in plans.values() for d in plan.domains for p in d.prefixes})
    if any(
        IPv4Network(p).prefixlen != 32
        or not IPv4Network(p).subnet_of(IPv4Network("10.205.0.0/16"))
        for p in origins
    ):
        raise ValueError("Only explicit laboratory node /32 origins are supported")
    topology = yaml.safe_load((ROOT / "containerlab/topology.clab.yml").read_text())
    configs = base_configs()
    peers: dict[str, dict[str, dict[str, int | str]]] = {
        node: {
            ip: {"asn": int(asn)}
            for ip, asn in re.findall(
                r"^ neighbor ([0-9.]+) remote-as ([0-9]+)$", text, re.MULTILINE
            )
        }
        for node, text in configs.items()
    }
    for node in NODES:
        configs[node] += (
            "\n"
            + "\n".join(
                f"ip prefix-list LAB-LOOPBACKS seq {20 + i} permit {prefix}"
                for i, prefix in enumerate(origins)
            )
            + "\n"
        )
        if node.startswith("spine"):
            asn = 65000 if node == "spine0" else 65001
            configs[node] += f"""router bgp {asn}
 bgp bestpath as-path multipath-relax
 address-family ipv4 unicast
  maximum-paths 2
 exit-address-family
exit
"""
    index = 0
    for plan in plans.values():
        if plan.host.local_asn is None:
            raise ValueError("Native host ASN is missing")
        for domain in plan.domains:
            for session in domain.sessions:
                tor = session.peer_id
                if tor not in ("r1a", "r1b", "r2a", "r2b") or session.vlan not in range(
                    3400, 3405
                ):
                    raise ValueError("Session is outside the lab rack/VLAN allocation")
                chassis = topology["topology"]["nodes"][tor]["env"]["LAB_HOST_MAC"]
                if (
                    session.chassis_id != chassis
                    or not session.local.is_link_local
                    or session.local.scope_id is not None
                ):
                    raise ValueError(
                        "Session does not match the native ToR chassis and link-local allocation"
                    )
                peer = str(session.local)
                if peer in peers[tor]:
                    raise ValueError("Each FRR neighbor needs a unique remote address per router")
                exports = sorted(
                    {
                        str(p)
                        for c in domain.contexts
                        if session in sessions_for_context(domain, c)
                        for p in c.exports
                    }
                )
                if not exports:
                    raise ValueError("Native session has no authorized node origins")
                index += 1
                incoming, outgoing = f"HOST_IN_{index}", f"HOST_OUT_{index}"
                text = "\n"
                for prefix_list, prefixes in (
                    (incoming, exports),
                    (outgoing, map(str, domain.imports)),
                ):
                    for seq, prefix in enumerate(prefixes, 1):
                        text += f"ip prefix-list {prefix_list} seq {seq * 10} permit {prefix}\n"
                    text += f"""route-map {prefix_list} permit 10
 match ip address prefix-list {prefix_list}
exit
"""
                asn = 65101 + list(("r1a", "r1b", "r2a", "r2b")).index(tor)
                text += f"""router bgp {asn}
 neighbor {peer} remote-as {plan.host.local_asn}
 neighbor {peer} interface eth3.{session.vlan}
 neighbor {peer} password {passwords[session.id]}
 neighbor {peer} bfd
 neighbor {peer} timers 3 9
 neighbor {peer} capability extended-nexthop
 address-family ipv4 unicast
  neighbor {peer} activate
  neighbor {peer} route-map {incoming} in
  neighbor {peer} route-map {outgoing} out
  neighbor {peer} maximum-prefix {len(exports)}
 exit-address-family
exit
"""
                configs[tor] += text
                peers[tor][peer] = {
                    "asn": plan.host.local_asn,
                    "interface": f"eth3.{session.vlan}",
                    "session_id": session.id,
                }
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    frr = output / "frr"
    frr.mkdir(exist_ok=True, mode=0o700)
    if not frr.resolve().is_relative_to(output):
        raise ValueError("Private FRR directory must stay within the output directory")
    for node, content in configs.items():
        write_atomic(frr / f"{node}.conf", content, 0o600)
    write_atomic(frr / "daemons", (ROOT / "containerlab/frr/daemons").read_text(), 0o600)
    write_atomic(output / "topology.clab.yml", yaml.safe_dump(topology, sort_keys=False), 0o600)
    manifest = {
        "origins": origins,
        "peers": peers,
        "expected_routes": {
            n: sorted([f"10.205.250.{v}/32" for other, v in NODES.items() if other != n] + origins)
            for n in NODES
        },
        "worker_uuids": {name: p.host.system_uuid for name, p in plans.items()},
        "origin_racks": {
            str(prefix.network_address): plan.host.rack
            for plan in plans.values()
            for domain in plan.domains
            for prefix in domain.prefixes
        },
    }
    write_atomic(output / "public-plan.json", json.dumps(manifest, indent=2) + "\n", 0o600)
    print(
        f"Rendered {len(configs)} private router configurations and {index} native host sessions "
        f"under {output}"
    )


if __name__ == "__main__":
    main()
