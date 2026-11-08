"""Configure an actual OVN distributed gateway and bounded provider NAT intent."""

import argparse
import ipaddress
import json
import time

import deploy_ovn as lab
from commission import ROOT
from maas_bird.runtime import write_atomic


def nb(*args):
    return lab.fabric("ovn-nbctl", *args)


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    identities = lab.owned()
    model = json.loads((ROOT / "containerlab/gateways.json").read_text())
    fabric = json.loads((ROOT / "runtime/host-fabric/public-plan.json").read_text())
    if fabric.get("gateway_intent") != model:
        raise RuntimeError("Apply the declared gateway transport before configuring OVN")
    logical = model["logical_router"]
    router, external = logical["name"], logical["provider_switch"]
    current = json.loads((ROOT / "runtime/ovn/manifest.json").read_text())
    if set(current) != set(lab.WORKERS) or any(
        current[n]["uuid"] != identities[n] for n in current
    ):
        raise RuntimeError("Tenant endpoints differ from the owned native workers")
    chassis = json.loads(
        lab.fabric("ovn-sbctl", "--format=json", "--columns=name", "list", "Chassis")
    )
    if {row[0] for row in chassis["data"]} != {
        *(identities[n] for n in lab.WORKERS),
        *model["gateways"],
    }:
        raise RuntimeError("Require exactly four compute chassis and two isolated gateways")
    nb("--may-exist", "lr-add", router)
    nb("--may-exist", "ls-add", external)
    for switch, lrp, lsp, address, mac in (
        (
            "network-lab-tenant",
            "provider-router-to-tenant",
            "tenant-to-provider-router",
            logical["tenant_address"],
            logical["tenant_mac"],
        ),
        (
            external,
            "provider-router-to-external",
            "provider-to-router",
            logical["provider_address"],
            logical["provider_mac"],
        ),
    ):
        nb("--may-exist", "lrp-add", router, lrp, mac, address)
        nb("--may-exist", "lsp-add", switch, lsp)
        nb("lsp-set-type", lsp, "router")
        nb("lsp-set-addresses", lsp, "router")
        nb("lsp-set-options", lsp, "router-port=" + lrp)
    nb("--may-exist", "lsp-add", external, "provider-localnet")
    nb("lsp-set-type", "provider-localnet", "localnet")
    nb("lsp-set-addresses", "provider-localnet", "unknown")
    nb("lsp-set-options", "provider-localnet", "network_name=" + logical["physnet"])
    for name, config in model["gateways"].items():
        nb("lrp-set-gateway-chassis", "provider-router-to-external", name, str(config["priority"]))
    floating = ipaddress.IPv4Address(logical["floating_start"])
    gateway = str(ipaddress.ip_interface(logical["tenant_address"]).ip)
    endpoints = {}
    for index, name in enumerate(lab.WORKERS):
        address = str(floating + index)
        nb("--may-exist", "lr-nat-add", router, "dnat_and_snat", address, current[name]["address"])
        current[name]["gateway"] = gateway
        lab.endpoint(name, current[name])
        endpoints[name] = {
            "tenant": current[name]["address"],
            "floating": address,
            "uuid": identities[name],
        }
    write_atomic(ROOT / "runtime/ovn/manifest.json", json.dumps(current, indent=2) + "\n", 0o600)
    receipt = {
        "model": model,
        "endpoints": endpoints,
        "completed": time.time(),
        "boundary": "Native OVN router, centralized NAT, gateway priorities and tenant defaults; "
        "packet and failover acceptance separate",
    }
    write_atomic(
        ROOT / "runtime/gateways/ovn-intent.json", json.dumps(receipt, indent=2) + "\n", 0o600
    )
    print(
        "Configured native OVN gateway intent; "
        "actual NAT traffic and HA acceptance remain required"
    )


if __name__ == "__main__":
    main()
