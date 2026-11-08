"""Read declared role policy with optional, enrollment-bound runtime identities."""

import json

import yaml
from commission import HOSTS, ROOT


def bindings():
    manifest = ROOT / "runtime/policy/bindings.json"
    if not manifest.exists():
        return None
    record = json.loads(manifest.read_text())
    if set(record["worker_uuids"]) != set(HOSTS) or set(record["bindings"]) != set(HOSTS):
        raise RuntimeError("Runtime policy must bind exactly the four workers")
    for name in HOSTS:
        directory = ROOT / "runtime" if name == "probe" else ROOT / "runtime" / name
        enrollment = json.loads((directory / "commission.json").read_text())
        if (
            record["worker_uuids"][name] != enrollment["lxd_uuid"]
            or record["bindings"][name]["machine_id"] != enrollment["system_id"]
        ):
            raise RuntimeError("Runtime policy belongs to a different MAAS/LXD enrollment")
    return record["bindings"]


def read(name):
    if name not in {"all-roles.yaml", "all-attachments.yaml", "inventory.yaml"}:
        raise ValueError("Unsupported laboratory policy file")
    directory = ROOT / "runtime/policy" if bindings() is not None else ROOT / "juju/bird"
    return yaml.safe_load((directory / name).read_text())
