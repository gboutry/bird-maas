"""Admit only the declared gateway VTEPs into the hosts' GP routing domain."""

import argparse
import json
import time

import jubilant
from commission import HOSTS, ROOT
from juju_workers import worker_binding
from maas_bird.runtime import write_atomic
from render_host_fabric import WORKERS


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    model = json.loads((ROOT / "containerlab/gateways.json").read_text())
    manifest = json.loads((ROOT / "runtime/host-fabric/public-plan.json").read_text())
    if manifest.get("gateway_intent") != model:
        raise RuntimeError("Apply the matching gateway transport first")
    base = {h["loopback"] for h in HOSTS.values()}
    target = base | {g["vtep"] for g in model["gateways"].values()}
    juju = jubilant.Juju(model="network-lab:network-lab")
    updates = {}
    bindings = {}
    for name, suffix in WORKERS.items():
        app = "bird-" + suffix
        bindings[name] = worker_binding(juju, name, app)
        rows = json.loads(juju.cli("config", app, "routing-domains"))
        if {row["id"] for row in rows} != {"general", "ceph-access", "ceph-replication"}:
            raise RuntimeError("Require the complete, declared three-domain host recipe")
        general = next(row for row in rows if row["id"] == "general")
        if (
            set(general["import_prefixes"]) not in (base, target)
            or len(general["contexts"]) != 1
            or general["contexts"][0]["sources"] != [HOSTS[name]["loopback"]]
            or general["contexts"][0]["table"] != 4400
            or general["kernel_ecmp_limit"] != 2
        ):
            raise RuntimeError("Refusing to replace an unexpected GP routing configuration")
        general["import_prefixes"] = sorted(target)
        updates[app] = json.dumps(rows, indent=2)
    directory = ROOT / "runtime/gateways"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt = {
        "started": time.time(),
        "worker_uuids": {n: b.uuid for n, b in bindings.items()},
        "general_imports": sorted(target),
        "applications": list(updates),
    }
    for app, value in updates.items():
        juju.config(app, {"routing-domains": value})
    juju.wait(lambda s: jubilant.all_active(s, *updates), timeout=300)
    receipt["completed"] = time.time()
    receipt["boundary"] = (
        "Declared GP imports and active applications; gateway packet acceptance separate"
    )
    write_atomic(
        directory / f"host-imports-{time.time_ns()}.json",
        json.dumps(receipt, indent=2) + "\n",
        0o600,
    )
    print("Admitted two declared gateway VTEPs into GP; storage domain imports unchanged")


if __name__ == "__main__":
    main()
