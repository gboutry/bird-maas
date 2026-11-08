"""Validate packed sources and initialize charms in the empty owned lab model.

Without --apply this checks artifact contents, identity bindings and native
agents only. Existing applications are never refreshed or reconfigured here.
"""

import argparse
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from zipfile import ZipFile

import jubilant
import lab_policy
import yaml
from commission import HOSTS, PROJECT, ROOT, command
from deployment_targets import owned_infrastructure
from juju_workers import worker_binding
from maas_bird.runtime import write_atomic
from render_host_fabric import WORKERS

MODEL = "network-lab:network-lab"
REPOSITORY = ROOT.parents[1]
CHARMS = ("maas-inventory", "dpu-attachments", "bird")


def artifacts(directory):
    result = {}
    for name in CHARMS:
        path = directory / (name + "_amd64.charm")
        with ZipFile(path) as archive:
            if yaml.safe_load(archive.read("metadata.yaml"))["name"] != name:
                raise RuntimeError("Charm artifact metadata differs from its declared application")
            roots = [
                p.removesuffix("maas_bird/models.py")
                for p in archive.namelist()
                if p.endswith("site-packages/maas_bird/models.py")
            ]
            if len(roots) != 1:
                raise RuntimeError("Expected one installed shared maas_bird package")
            files = {}
            for packaged in ("metadata.yaml", "manifest.yaml"):
                files[packaged] = hashlib.sha256(archive.read(packaged)).hexdigest()
            for base, prefix in (
                (REPOSITORY / "shared/src", roots[0]),
                (REPOSITORY / "charms" / name / "src", "src/"),
            ):
                for source in base.rglob("*.py"):
                    packaged = prefix + source.relative_to(base).as_posix()
                    if archive.read(packaged) != source.read_bytes():
                        raise RuntimeError(
                            f"Packed source differs from this checkout: {name}/{packaged}"
                        )
                    files[packaged] = hashlib.sha256(source.read_bytes()).hexdigest()
        result[name] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_hashes": files,
        }
    return result


def installed_sources(juju, app, expected):
    current = json.loads(juju.cli("status", "--format=json"))["applications"][app]
    units = current.get("units", {})
    if len(units) != 1:
        raise RuntimeError("Source verification requires exactly one identified application unit")
    unit = next(iter(units))
    directory = "/var/lib/juju/agents/unit-" + unit.replace("/", "-") + "/charm"
    script = (
        "import hashlib,json,pathlib,sys; root=pathlib.Path(sys.argv[1]); "
        "print(json.dumps({p:hashlib.sha256((root/p).read_bytes()).hexdigest() "
        "for p in json.loads(sys.argv[2])}))"
    )
    actual = json.loads(
        juju.exec(
            "sudo", "python3", "-c", script, directory, json.dumps(list(expected)), unit=unit
        ).stdout
    )
    if actual != expected:
        raise RuntimeError("Installed charm files differ from the source-checked artifact")
    return {"unit": unit, "hashes": actual}


def native_machines(juju):
    owned_infrastructure()
    details = json.loads(
        juju.cli("show-controller", "network-lab", "--format=json", include_model=False)
    )["network-lab"]["details"]
    if details["cloud"] != "manual" or details["api-endpoints"] != ["10.203.240.12:17070"]:
        raise RuntimeError("Charm deployment requires the dedicated manual lab controller")
    model = json.loads(juju.cli("show-model", MODEL, "--format=json", include_model=False))[
        "network-lab"
    ]
    if model["controller-uuid"] != details["uuid"] or model["cloud"] != "manual":
        raise RuntimeError("Model belongs to a different controller or cloud")
    status = json.loads(juju.cli("status", "--format=json"))
    rows = status.get("machines", {})
    if len(rows) != 4 or {r.get("instance-id") for r in rows.values()} != {
        "manual:" + h["management_ip"] for h in HOSTS.values()
    }:
        raise RuntimeError("Expected exactly the four native worker agents")
    machines = {}
    for name, host in HOSTS.items():
        row = json.loads(command("lxc", "query", f"/1.0/instances/{name}?project={PROJECT}"))
        if (
            row["type"] != "virtual-machine"
            or row["config"].get("user.maas-bird-owner") != "network-lab"
            or row["config"]["limits.cpu"] != "2"
            or row["config"]["limits.memory"] != "4GiB"
        ):
            raise RuntimeError("Charm placement requires the owned, capped workers")
        machine, agent = next(
            (k, v)
            for k, v in rows.items()
            if v["instance-id"] == "manual:" + host["management_ip"]
        )
        if agent.get("juju-status", {}).get("current") != "started":
            raise RuntimeError("Existing native agent is not started; observe it before retrying")
        native = juju.exec(
            "sudo", "cat", "/sys/class/dmi/id/product_uuid", machine=machine
        ).stdout.strip()
        if native != row["config"]["volatile.uuid"]:
            raise RuntimeError("Native Juju machine differs from the owned LXD worker")
        machines[name] = machine
    return status, machines, model["model-uuid"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=ROOT / "runtime/charms")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Refresh the nine identified lab applications from checked local artifacts",
    )
    args = parser.parse_args()
    os.umask(0o077)
    packed = artifacts(args.artifacts.resolve())
    if lab_policy.bindings() is None:
        raise RuntimeError("Prepare native runtime identity policy before deploying charms")
    juju = jubilant.Juju(model=MODEL)
    status, machines, model_uuid = native_machines(juju)
    directory = ROOT / f"runtime/charm-deployment-{time.time_ns()}"
    directory.mkdir(mode=0o700)
    receipt = {
        "started": time.time(),
        "apply_requested": args.apply,
        "artifacts": packed,
        "machines": machines,
        "model_uuid": model_uuid,
    }
    write_atomic(directory / "receipt.json", json.dumps(receipt, indent=2) + "\n", 0o600)
    if not args.apply:
        print("Packed sources and all four native agents checked; no application changes")
        return
    if status.get("applications"):
        if not args.refresh_existing:
            raise RuntimeError("Initial charm deployment refuses an existing application model")
        applications = {"inventory": "maas-inventory"}
        for suffix in WORKERS.values():
            applications["bird-" + suffix] = "bird"
            applications["nic-" + suffix] = "dpu-attachments"
        if set(status["applications"]) != set(applications):
            raise RuntimeError("Refresh requires exactly the nine declared lab applications")
        for name, suffix in WORKERS.items():
            for app in ("bird-" + suffix, "nic-" + suffix):
                if worker_binding(juju, name, app).machine != machines[name]:
                    raise RuntimeError("Refresh target differs from native worker placement")
        if worker_binding(juju, "probe", "inventory").machine != machines["probe"]:
            raise RuntimeError("Inventory publisher is outside the declared worker placement")
        refreshed = []
        receipt["refreshed"] = refreshed
        for app, charm in applications.items():
            before = json.loads(juju.cli("status", "--format=json"))["applications"][app]
            old_idle = {unit: row["juju-status"]["since"] for unit, row in before["units"].items()}
            juju.refresh(app, path=Path(packed[charm]["path"]))
            deadline = time.monotonic() + 600
            while True:
                after = json.loads(juju.cli("status", "--format=json"))["applications"][app]
                if (
                    after["charm-rev"] != before["charm-rev"]
                    and after["application-status"]["current"] == "active"
                    and set(after["units"]) == set(old_idle)
                    and all(
                        row["juju-status"]["current"] == "idle"
                        and row["juju-status"]["since"] != old_idle[unit]
                        for unit, row in after["units"].items()
                    )
                ):
                    installed = installed_sources(juju, app, packed[charm]["source_hashes"])
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Native charm refresh has not completed; inspect the retained receipt"
                    )
                time.sleep(2)
            refreshed.append(
                {"app": app, "charm_revision": after["charm-rev"], "installed": installed}
            )
            write_atomic(directory / "receipt.json", json.dumps(receipt, indent=2) + "\n", 0o600)
        juju.wait(lambda s: jubilant.all_active(s, *applications), timeout=300)
        receipt["completed"] = time.time()
        write_atomic(directory / "receipt.json", json.dumps(receipt, indent=2) + "\n", 0o600)
        print("Nine native applications refreshed; forwarding acceptance remains required")
        return
    bird, native = lab_policy.read("all-roles.yaml"), lab_policy.read("all-attachments.yaml")
    inventory = lab_policy.read("inventory.yaml")["inventory"]
    bgp = ROOT / "runtime/bgp"
    bgp.mkdir(mode=0o700, exist_ok=True)
    credentials_path = bgp / "credentials.json"
    if credentials_path.exists():
        raise RuntimeError(
            "Initial deployment refuses existing BGP credentials; inspect prior progress"
        )
    keys = {
        a["id"]: secrets.token_hex(16)
        for config in native.values()
        for a in json.loads(config["attachments"])
    }
    if len(keys) != 40:
        raise RuntimeError("Four workers require exactly forty declared authenticated sessions")
    write_atomic(credentials_path, json.dumps(keys) + "\n", 0o600)

    def secret(label, content):
        path = directory / (label + ".json")
        write_atomic(path, json.dumps(content) + "\n", 0o600)
        uri = juju.cli("add-secret", label, "--file", str(path)).strip()
        if not uri.startswith("secret:") or len(uri.split()) != 1:
            raise RuntimeError("Native Juju did not return one secret URI")
        return uri

    api = command(
        "lxc", "exec", "--project", PROJECT, "maas", "--", "maas", "apikey", "--username=lab"
    )
    api_secret = secret("network-lab-maas-api", {"api-key": api})
    ca_secret = secret(
        "network-lab-maas-ca", {"ca-cert": (ROOT / "runtime/maas-ca.crt").read_text()}
    )
    juju.deploy(
        Path(packed["maas-inventory"]["path"]),
        "inventory",
        to=machines["probe"],
        base="ubuntu@24.04",
    )
    juju.grant_secret(api_secret, "inventory")
    juju.grant_secret(ca_secret, "inventory")
    inventory.update(
        {
            "maas-url": "https://10.203.240.10:5443/MAAS/",
            "maas-credentials": api_secret,
            "maas-ca-certificate": ca_secret,
            "inventory-cache-seconds": 30,
        }
    )
    path = directory / "inventory.yaml"
    write_atomic(path, yaml.safe_dump({"inventory": inventory}), 0o600)
    juju.cli("config", "inventory", "--file", str(path))
    secret_ids = {}
    for name, suffix in WORKERS.items():
        observer, app = "nic-" + suffix, "bird-" + suffix
        juju.deploy(
            Path(packed["dpu-attachments"]["path"]),
            observer,
            to=machines[name],
            base="ubuntu@24.04",
            config=native[observer],
        )
        juju.deploy(
            Path(packed["bird"]["path"]),
            app,
            to=machines[name],
            base="ubuntu@24.04",
            config=bird[app],
        )
        content = {a["id"]: keys[a["id"]] for a in json.loads(native[observer]["attachments"])}
        uri = secret("network-lab-bgp-" + suffix, content)
        secret_ids[suffix] = uri
        write_atomic(bgp / "secret-ids.json", json.dumps(secret_ids) + "\n", 0o600)
        juju.grant_secret(uri, app)
        juju.cli("config", app, "bgp-credentials=" + uri)
        juju.integrate(app + ":inventory", "inventory:inventory")
        juju.integrate(app + ":attachments", observer + ":attachments")
    juju.cli("model-config", "update-status-hook-interval=1m")
    juju.wait(lambda s: jubilant.all_active(s, "inventory", *native), timeout=900)
    for name, suffix in WORKERS.items():
        for app in ("bird-" + suffix, "nic-" + suffix):
            if worker_binding(juju, name, app).machine != machines[name]:
                raise RuntimeError("Native application placement differs from the selected worker")
    receipt["inputs_ready"] = time.time()
    write_atomic(directory / "receipt.json", json.dumps(receipt, indent=2) + "\n", 0o600)
    print("Inventory and native observers ready; BIRD remains disabled until fabric activation")


if __name__ == "__main__":
    main()
