"""Declared GP destinations and AS-path preferences, independent of live FIBs."""

import json

from commission import HOSTS, ROOT


def gateway_intent():
    path = ROOT / "runtime/host-fabric/public-plan.json"
    if not path.exists():
        return None
    declared = json.loads(path.read_text()).get("gateway_intent")
    if declared is None:
        return None
    expected = json.loads((ROOT / "containerlab/gateways.json").read_text())
    if declared != expected:
        raise RuntimeError("Gateway stage differs from the tracked topology")
    return expected


def general_paths(name, parents=("enp7s0", "enp8s0")):
    available = set(parents)
    if not available <= {"enp7s0", "enp8s0"}:
        raise ValueError("Unexpected GP parent")
    result = {
        h["loopback"].split("/")[0]: {p + ".3404" for p in available}
        for other, h in HOSTS.items()
        if other != name
    }
    model = gateway_intent()
    if model:
        for config in model["gateways"].values():
            selected = available
            if config["rack"] == HOSTS[name]["rack"]:
                direct = "enp7s0" if config["tor"].endswith("a") else "enp8s0"
                if direct in available:
                    selected = {direct}
            result[config["vtep"].split("/")[0]] = {p + ".3404" for p in selected}
    return result
