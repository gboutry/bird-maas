"""Compile routed reachability for dedicated logical OVN gateway namespaces.

Each gateway is attached to one ToR. Its /32 is advertised by that ToR only;
this is deliberately distinct from the dual-attached compute origins.
"""

import argparse
import json
import re
import shutil
from ipaddress import IPv4Interface, IPv4Network
from pathlib import Path

from commission import ROOT
from maas_bird.runtime import write_atomic
from render_host_fabric import NODES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "runtime/host-fabric")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    runtime = (ROOT / "runtime").resolve()
    if (
        not source.is_relative_to(runtime)
        or not output.is_relative_to(runtime)
        or source.is_relative_to(output)
        or output.is_relative_to(source)
        or output.exists()
    ):
        raise ValueError("Require separate runtime source and new output directories")
    manifest = json.loads((source / "public-plan.json").read_text())
    provider = json.loads((ROOT / "containerlab/provider.json").read_text())
    if manifest.get("provider_intent") != provider or "gateway_intent" in manifest:
        raise ValueError("Compile gateways from the declared provider fabric without gateways")
    model = json.loads((ROOT / "containerlab/gateways.json").read_text())
    if set(model["gateways"]) != {"nlab-gw-r1", "nlab-gw-r2"} or model["hardware_emulation"]:
        raise ValueError("Unexpected logical gateway allocation")
    configs = {node: (source / f"frr/{node}.conf").read_text() for node in NODES}
    origins = []
    for index, config in enumerate(model["gateways"].values()):
        node = config["tor"]
        if node not in {"r1a", "r2a"}:
            raise ValueError("Each initial gateway must use its own rack's ToR A")
        prefix = config["vtep"]
        if (
            IPv4Interface(prefix).network.prefixlen != 32
            or IPv4Interface(prefix).ip not in IPv4Network("10.205.251.0/24")
            or IPv4Interface(config["transport"]).network
            != IPv4Interface(config["tor_transport"]).network
            or IPv4Interface(config["transport"]).network.prefixlen != 31
            or not IPv4Interface(config["transport"]).network.subnet_of(
                IPv4Network("10.205.210.0/24")
            )
        ):
            raise ValueError("Gateway addressing differs from the reserved routed pools")
        origins.append(prefix)
        for router in NODES:
            configs[router] += (
                f"\nip prefix-list LAB-LOOPBACKS seq {1000 + index * 10} permit {prefix}\n"
            )
            for peer, intent in manifest["peers"][router].items():
                if intent.get("interface") != "eth3.3404":
                    continue
                outgoing = re.findall(
                    rf"^  neighbor {re.escape(peer)} route-map (HOST_OUT_[0-9]+) out$",
                    configs[router],
                    re.MULTILINE,
                )
                if len(outgoing) != 1:
                    raise ValueError("Expected one explicit GP export filter per host peer")
                configs[router] += (
                    f"ip prefix-list {outgoing[0]} seq {1000 + index * 10} permit {prefix}\n"
                )
        core = (ROOT / f"containerlab/frr/{node}.conf").read_text()
        asn = re.search(r"^router bgp ([0-9]+)$", core, re.MULTILINE)
        if asn is None:
            raise ValueError("Missing gateway ToR ASN")
        neighbor = config["transport"].split("/")[0]
        configs[node] += f"""interface eth5
 ip address {config["tor_transport"]}
exit
ip route {prefix} {neighbor}
router bgp {asn[1]}
 address-family ipv4 unicast
  network {prefix}
 exit-address-family
exit
"""
        for router in NODES:
            if router != node:
                manifest["expected_routes"][router].append(prefix)
    manifest["origins"] = sorted(set(manifest["origins"]) | set(origins))
    manifest["gateway_intent"] = model
    manifest["gateway_origin_tors"] = {
        config["vtep"].split("/")[0]: config["tor"] for config in model["gateways"].values()
    }
    shutil.copytree(source, output)
    output.chmod(0o700)
    for node, text in configs.items():
        write_atomic(output / f"frr/{node}.conf", text, 0o600)
    write_atomic(output / "public-plan.json", json.dumps(manifest, indent=2) + "\n", 0o600)
    print(
        "Compiled two single-ToR gateway /32 origins; "
        "native parser and dataplane checks remain required"
    )


if __name__ == "__main__":
    main()
