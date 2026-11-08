"""Add one explicitly filtered EVPN L2 service to the compiled private fabric.

Rendering and native FRR syntax validation do not establish a VXLAN dataplane
or OVN gateway functionality. IPv4 unicast configuration stays byte-identical.
"""

import argparse
import json
import re
import shutil
from pathlib import Path

from commission import ROOT
from maas_bird.runtime import write_atomic
from render_host_fabric import NODES


def provider_config(node, original, model):
    if "LAB_PROVIDER_EVPN" in original:
        raise ValueError("Compile the provider service from a plain host fabric")
    core = (ROOT / f"containerlab/frr/{node}.conf").read_text()
    asn = re.search(r"^router bgp ([0-9]+)$", core, re.MULTILINE)
    if asn is None:
        raise ValueError("Missing native fabric ASN")
    peers = re.findall(r"^ neighbor ([0-9.]+) remote-as [0-9]+$", core, re.MULTILINE)
    if len(peers) != (4 if node.startswith("spine") else 2):
        raise ValueError("Unexpected routed core peer allocation")
    text = (
        original
        + f"""
bgp extcommunity-list standard LAB_PROVIDER_RT permit rt {model["route_target"]}
route-map LAB_PROVIDER_EVPN permit 10
 match extcommunity LAB_PROVIDER_RT
 set ip next-hop unchanged
exit
router bgp {asn[1]}
 address-family l2vpn evpn
"""
    )
    for peer in peers:
        text += f"""  neighbor {peer} activate
  neighbor {peer} route-map LAB_PROVIDER_EVPN in
  neighbor {peer} route-map LAB_PROVIDER_EVPN out
  neighbor {peer} maximum-prefix 128
"""
    if not node.startswith("spine"):
        text += f"""  advertise-all-vni
  vni {model["vni"]}
   rd 10.205.250.{NODES[node]}:{model["vni"]}
   route-target import {model["route_target"]}
   route-target export {model["route_target"]}
  exit-vni
"""
    return text + " exit-address-family\nexit\n"


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
        or output.is_relative_to(source)
        or source.is_relative_to(output)
        or output.exists()
    ):
        raise ValueError("Require separate, private runtime source and new output directories")
    model = json.loads((ROOT / "containerlab/provider.json").read_text())
    if (model["vni"], model["route_target"], model["mtu"], model["vxlan_port"]) != (
        3405,
        "65000:3405",
        1500,
        4789,
    ):
        raise ValueError("Provider parameters differ from the reserved laboratory service")
    configs = {
        node: provider_config(node, (source / f"frr/{node}.conf").read_text(), model)
        for node in NODES
    }
    shutil.copytree(source, output)
    output.chmod(0o700)
    for node, config in configs.items():
        write_atomic(output / f"frr/{node}.conf", config, 0o600)
    manifest = json.loads((source / "public-plan.json").read_text())
    manifest["provider_intent"] = model
    write_atomic(output / "public-plan.json", json.dumps(manifest, indent=2) + "\n", 0o600)
    print("Rendered one EVPN service; native parser, VXLAN and packet acceptance remain required")


if __name__ == "__main__":
    main()
