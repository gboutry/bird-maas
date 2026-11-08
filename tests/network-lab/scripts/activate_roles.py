"""Apply declared role membership through Juju and the native FRR preflight.

Requires already provisioned MAAS workers, enrolled SSH keys and native Juju
agents. This is a lab maintenance operation: selected BIRD units are disabled
while their complete configuration and switch peers are replaced.
"""

import argparse
import json
import re
import secrets
import subprocess
import sys
import time

import deploy_ovn as lab
import jubilant
import lab_policy
import yaml
from commission import HOSTS, ROOT, maas
from generate_roles import address, configurations, roles
from juju_workers import worker_binding
from maas_bird.models import MultiDomainPlan
from maas_bird.runtime import write_atomic
from render_host_fabric import WORKERS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--members", nargs="+", choices=tuple(HOSTS), default=list(HOSTS))
    args = parser.parse_args()
    if len(args.members) != len(set(args.members)):
        parser.error("Membership must be unique")
    installed = json.loads(
        lab.command(
            "lxc",
            "exec",
            "--project",
            lab.PROJECT,
            "fabric",
            "--",
            "python3",
            "-c",
            "import json,pathlib; print(json.dumps([name for name in "
            "('network-lab-provider.service','network-lab-gateways.service') "
            "if (pathlib.Path('/etc/systemd/system')/name).is_file()]))",
        )
    )
    if installed:
        raise RuntimeError(
            "Role activation compiles a plain host fabric and cannot replace installed "
            "provider/gateway services. No application configuration was changed."
        )
    bindings = lab_policy.bindings()
    bird, native, _ = configurations(
        args.members,
        bindings={name: bindings[name] for name in args.members} if bindings is not None else None,
    )
    juju = jubilant.Juju(model="network-lab:network-lab")
    bindings = {}
    for name, suffix in WORKERS.items():
        bindings[name] = worker_binding(juju, name, "bird-" + suffix)
        runtime = ROOT / "runtime" if name == "probe" else ROOT / "runtime" / name
        enrollment = json.loads((runtime / "commission.json").read_text())
        machine = maas("machine", "read", enrollment["system_id"])
        if machine["status_name"] != "Deployed" or machine["hardware_uuid"] != bindings[name].uuid:
            raise RuntimeError("All workers must have a native, identified MAAS deployment")
        if name in args.members:
            links = json.loads(lab.ssh(name, "ip", "-j", "link", "show"))
            expected = {f"{p}.{r['vlan']}" for p in ("enp7s0", "enp8s0") for r in roles()}
            if not expected <= {link["ifname"] for link in links}:
                raise RuntimeError("Selected workers require all ten provisioned role interfaces")
            loopback = json.loads(lab.ssh(name, "ip", "-j", "-4", "address", "show", "dev", "lo"))
            actual = {a["local"] + "/32" for a in loopback[0]["addr_info"] if a["prefixlen"] == 32}
            if actual != {address(name, r) for r in roles()}:
                raise RuntimeError("Selected worker loopbacks differ from the role allocation")
    directory = ROOT / f"runtime/role-activation-{time.time_ns()}"
    directory.mkdir(mode=0o700)
    steps: list[dict[str, str | float]] = []
    receipt = {
        "members": args.members,
        "worker_uuids": {name: binding.uuid for name, binding in bindings.items()},
        "started": time.time(),
        "steps": steps,
    }

    def record(step):
        steps.append({"step": step, "at": time.time()})
        write_atomic(directory / "receipt.json", json.dumps(receipt, indent=2) + "\n", 0o600)
        print(step, flush=True)

    def helper(script, *arguments):
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), *arguments],
            check=True,
            timeout=900,
        )

    record("Verified native ownership, identity, resource caps and MAAS deployment")

    keys_path = ROOT / "runtime/bgp/credentials.json"
    if keys_path.stat().st_mode & 0o077:
        raise ValueError("BGP credentials must be private")
    keys = json.loads(keys_path.read_text())
    if not all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v) for v in keys.values()):
        raise ValueError("Expected private hexadecimal BGP credentials")
    secret_ids = json.loads((ROOT / "runtime/bgp/secret-ids.json").read_text())
    write_atomic(directory / "previous-credentials.json", json.dumps(keys) + "\n", 0o600)
    expected = set()
    for name, suffix in WORKERS.items():
        app = "bird-" + suffix
        if name in args.members:
            ids = {a["id"] for a in json.loads(native["nic-" + suffix]["attachments"])}
        else:
            plan = MultiDomainPlan.model_validate_json(
                juju.run(bindings[name].unit, "plan").results["plan"]
            )
            ids = {s.id for d in plan.domains for s in d.sessions}
        expected.update(ids)
        if name not in args.members:
            continue
        previous = json.loads(juju.cli("config", app, "--format=json"))
        write_atomic(directory / f"previous-{app}.json", json.dumps(previous) + "\n", 0o600)
        for identity in ids:
            keys.setdefault(identity, secrets.token_hex(16))
        per_app = {identity: keys[identity] for identity in sorted(ids)}
        path = ROOT / f"runtime/bgp/{suffix}.json"
        write_atomic(path, json.dumps(per_app) + "\n", 0o600)
        juju.cli("update-secret", secret_ids[suffix], "--file", str(path))
    write_atomic(keys_path, json.dumps({k: keys[k] for k in sorted(expected)}) + "\n", 0o600)
    record("Updated declared session credentials without rotating existing values")
    for app in bird:
        juju.cli("config", app, "enabled=false")
    for observer, config in native.items():
        path = directory / f"{observer}.yaml"
        write_atomic(path, yaml.safe_dump({observer: config}), 0o600)
        juju.cli("config", observer, "--file", str(path))
    juju.wait(lambda s: jubilant.all_active(s, *native), timeout=240)
    for app, config in bird.items():
        path = directory / f"{app}.yaml"
        write_atomic(path, yaml.safe_dump({app: config}), 0o600)
        juju.cli("config", app, "--file", str(path))
    record("Applied ten-role observer and disabled routing recipes")
    compiled = directory / "fabric"
    helper("render_host_fabric.py", "--output", str(compiled))
    helper("apply_host_fabric.py", "--directory", str(compiled), "--apply")
    record("Applied switch peers after native FRR validation")
    for app in bird:
        juju.cli("config", app, "enabled=true")
    juju.wait(lambda s: jubilant.all_active(s, *bird), timeout=300)
    record("All selected BIRD applications active; packet acceptance remains required")


if __name__ == "__main__":
    main()
