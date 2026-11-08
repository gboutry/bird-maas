"""Deploy the owned network lab serially through its existing native helpers.

Bootstrap infrastructure/controllers first and provide source-checked packed
charms. Without --apply this lists stages only. It never tears down resources.
Fresh deployment through this entry point requires separate lifecycle proof.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import deploy_ovn as lab
import host_acceptance as host
from bootstrap import JUJU, owned, run
from commission import HOSTS, ROOT, command, maas
from deploy_probe import validate_configuration, validate_roles
from deployment_targets import owned_infrastructure
from maas_bird.runtime import write_atomic
from upgrade_hwe import installed_kernel

PHASES = (
    "fabric",
    "workers",
    "agents",
    "policy",
    "charms",
    "routing",
    "overlay",
    "provider",
    "gateways",
)
CURRENT = ROOT / "runtime/host-fabric"


def helper(name, *args, timeout=3600):
    run(sys.executable, str(ROOT / "scripts" / name), *args, timeout=timeout)


def manifest():
    path = CURRENT / "public-plan.json"
    if not path.exists():
        return None
    row = json.loads(path.read_text())
    if row["worker_uuids"] != {name: owned(name, 2, "4GiB") for name in HOSTS}:
        raise RuntimeError("Compiled fabric belongs to a different worker generation")
    return row


def apply_stage(source):
    if CURRENT.is_symlink() or (CURRENT / "frr").is_symlink():
        raise RuntimeError("Canonical private fabric directories must not be symlinks")
    helper("apply_host_fabric.py", "--directory", str(source), "--apply")
    shutil.copytree(source, CURRENT, dirs_exist_ok=True)


def seed_fabric():
    current = manifest()
    if current is not None:
        guest = json.loads(lab.fabric("cat", "/opt/network-lab/host-fabric/public-plan.json"))
        if guest != current:
            raise RuntimeError("Local and installed fabric manifests differ")
        if lab.fabric("systemctl", "is-active", "network-lab-fabric") != "active":
            raise RuntimeError("Installed fabric service is not active")
        return
    helper("bootstrap_fabric.py", "--apply")
    candidates = sorted(
        (ROOT / "runtime").glob("fabric-seed-*/receipt.json"),
        key=lambda p: json.loads(p.read_text()).get("service_started", 0),
    )
    if not candidates or not json.loads(candidates[-1].read_text()).get("service_started"):
        raise RuntimeError("Seed helper did not retain a successful service receipt")
    shutil.copytree(candidates[-1].parent, CURRENT)


def hwe_ready(name):
    try:
        expected = installed_kernel(name)
    except subprocess.CalledProcessError:
        expected = None
    running = lab.ssh(name, "uname", "-r")
    if expected == running and not running.startswith("6.8."):
        return
    native = json.loads(
        lab.ssh(
            name,
            "sudo",
            "python3",
            "-c",
            "import json,pathlib; print(json.dumps({'agents':list(map(str,"
            "pathlib.Path('/var/lib/juju/agents').glob('machine-*'))),"
            "'overlay':pathlib.Path('/etc/openvswitch/system-id.conf').exists()}))",
        )
    )
    if native["agents"] or native["overlay"]:
        raise RuntimeError("Use the existing HWE maintenance helper for an already managed worker")
    lab.ssh(name, "sudo", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "update", "-qq")
    lab.ssh(
        name,
        "sudo",
        "env",
        "DEBIAN_FRONTEND=noninteractive",
        "apt-get",
        "install",
        "-y",
        "linux-generic-hwe-24.04",
        timeout=1200,
    )
    expected = installed_kernel(name)
    if expected.startswith("6.8."):
        raise RuntimeError("The available HWE image does not meet the non-GA kernel requirement")
    if lab.ssh(name, "uname", "-r") != expected:
        previous = lab.ssh(name, "cat", "/proc/sys/kernel/random/boot_id")
        try:
            lab.ssh(name, "sudo", "systemctl", "reboot", "--no-wall", timeout=15)
        except subprocess.CalledProcessError as error:
            if error.returncode != 255:
                raise
        deadline = time.monotonic() + 600
        while True:
            try:
                assert (
                    lab.ssh(name, "cat", "/proc/sys/kernel/random/boot_id", timeout=12) != previous
                )
                assert lab.ssh(name, "uname", "-r") == expected
                break
            except (AssertionError, subprocess.SubprocessError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
    identity = owned(name, 2, "4GiB")
    if lab.ssh(name, "sudo", "cat", "/sys/class/dmi/id/product_uuid") != identity:
        raise RuntimeError("HWE readiness belongs to a different native worker")


def ensure_routing_fabrics():
    fabrics = maas("fabrics", "read")
    matches = {}
    for rack in sorted({config["rack"] for config in HOSTS.values()}):
        for tor in ("a", "b"):
            suffix = f"-r{rack}-{tor}"
            candidates = [f for f in fabrics if (f["name"] or "").endswith(suffix)]
            if len(candidates) > 1:
                raise RuntimeError("Ambiguous MAAS routing fabric: " + suffix)
            matches[rack, tor] = candidates[0] if candidates else None
    result = []
    for (rack, tor), fabric in matches.items():
        if fabric is None:
            fabric = maas("fabrics", "create", f"name=network-lab-r{rack}-{tor}")
        if not any(v["vid"] == 0 for v in fabric["vlans"]):
            raise RuntimeError("Routing fabric has no untagged parent VLAN")
        gp = next((v for v in fabric["vlans"] if v["vid"] == 3404), None)
        if gp is None:
            gp = maas("vlans", "create", str(fabric["id"]), "vid=3404", "name=gp")
        result.append(
            {
                "rack": rack,
                "tor": tor,
                "name": fabric["name"],
                "fabric_id": fabric["id"],
                "gp_vlan_id": gp["id"],
            }
        )
    if len({f["fabric_id"] for f in result}) != len(result):
        raise RuntimeError("Routing domains must have distinct MAAS fabric identities")
    return result


def workers():
    fabric_model = {
        "maas_uuid": owned("maas", 4, "8GiB"),
        "fabrics": ensure_routing_fabrics(),
        "observed": time.time(),
    }
    write_atomic(
        ROOT / "runtime/maas-fabric-model.json", json.dumps(fabric_model, indent=2) + "\n", 0o600
    )
    for name, config in HOSTS.items():
        identity = owned(name, 2, "4GiB")
        runtime = ROOT / "runtime" if name == "probe" else ROOT / "runtime" / name
        enrollment = runtime / "commission.json"
        if not enrollment.exists():
            helper("commission.py", "--name", name)
        record = json.loads(enrollment.read_text())
        if record["lxd_uuid"] != identity:
            raise RuntimeError("Commissioning receipt belongs to a different VM")
        machine = maas("machine", "read", record["system_id"])
        if machine["status_name"] == "Commissioning":
            helper("commission.py", "--name", name, "--resume")
            machine = maas("machine", "read", record["system_id"])
        if machine["status_name"] in {"Ready", "Deploying"}:
            helper(
                "deploy_probe.py",
                "--name",
                name,
                "--model",
                "roles",
                "--kernel",
                "hwe-24.04",
                *(("--resume",) if machine["status_name"] == "Deploying" else ()),
            )
            machine = maas("machine", "read", record["system_id"])
        if machine["status_name"] != "Deployed" or machine["hardware_uuid"] != identity:
            raise RuntimeError("Worker is not natively deployed; inspect existing lifecycle state")
        validate_configuration(machine, name, "loopback32", config["management_ip"])
        validate_roles(machine)
    helper("prepare_ssh.py", "--scope", "workers")
    for name in HOSTS:
        try:
            lab.ssh(name, "cloud-init", "status", "--wait", "--format=json", timeout=600)
        except subprocess.CalledProcessError as error:
            if error.returncode != 2 or json.loads(error.stdout).get("status") != "done":
                raise
            print(name + ": cloud-init is degraded; strict acceptance remains a separate failure")
        hwe_ready(name)


def agents():
    models = json.loads(command(str(JUJU), "models", "-c", "network-lab", "--format=json"))
    if not any(m["name"].split("/")[-1] == "network-lab" for m in models["models"]):
        run(str(JUJU), "add-model", "network-lab", "-c", "network-lab")
    helper("prepare_ssh.py", "--scope", "all")
    for name in HOSTS:
        helper("rejoin_juju.py", "--name", name, "--agents-only")


def routing():
    try:
        for name in HOSTS:
            host.routing_ready(name)
        return
    except (AssertionError, subprocess.CalledProcessError):
        current = manifest()
        if current and ("provider_intent" in current or "gateway_intent" in current):
            raise RuntimeError(
                "Repair installed service stages before plain role activation"
            ) from None
    helper("activate_roles.py")
    candidates = sorted(
        (ROOT / "runtime").glob("role-activation-*/receipt.json"),
        key=lambda p: p.stat().st_mtime_ns,
    )
    if not candidates:
        raise RuntimeError("Role activation did not retain its native receipt")
    shutil.copytree(candidates[-1].parent / "fabric", CURRENT, dirs_exist_ok=True)
    for name in HOSTS:
        host.routing_ready(name)


def overlay():
    if (ROOT / "runtime/ovn/manifest.json").exists():
        for name in HOSTS:
            host.overlay_ready(name)
    else:
        helper("deploy_ovn.py", "--install", "--layer4-ecmp")


def provider(directory):
    current = manifest()
    if current is None:
        raise RuntimeError("Provider deployment requires compiled host routing")
    intent = json.loads((ROOT / "containerlab/provider.json").read_text())
    if "provider_intent" not in current:
        source = directory / "provider-fabric"
        helper("render_provider_fabric.py", "--output", str(source))
        apply_stage(source)
        helper("deploy_provider.py", "--apply")
    elif (
        current["provider_intent"] != intent
        or lab.fabric("systemctl", "is-active", "network-lab-provider") != "active"
    ):
        raise RuntimeError("Existing provider stage differs from its declared active service")


def gateways(directory):
    current = manifest()
    if current is None or "provider_intent" not in current:
        raise RuntimeError("Gateways require the native provider stage")
    intent = json.loads((ROOT / "containerlab/gateways.json").read_text())
    if "gateway_intent" in current:
        if (
            current["gateway_intent"] != intent
            or lab.fabric("systemctl", "is-active", "network-lab-gateways") != "active"
        ):
            raise RuntimeError("Existing gateway stage differs from its declared active service")
        return
    for unit in ("ovn-host", "openvswitch-switch", "ovsdb-server", "ovs-vswitchd"):
        active = host.root("fabric", "systemctl", "is-active", unit, check=False)
        if active.stdout.strip() == "active":
            raise RuntimeError("Gateway namespaces require inactive global OVS/OVN host services")
    lab.fabric(
        "systemctl", "mask", "ovn-host", "openvswitch-switch", "ovsdb-server", "ovs-vswitchd"
    )
    lab.fabric("env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "update", "-qq")
    lab.fabric(
        "env",
        "DEBIAN_FRONTEND=noninteractive",
        "apt-get",
        "install",
        "-y",
        "ovn-host",
        "openvswitch-switch",
    )
    source = directory / "gateway-fabric"
    helper("render_gateway_fabric.py", "--output", str(source))
    apply_stage(source)
    helper("deploy_gateway_nodes.py", "--apply")
    helper("configure_gateway_imports.py")
    helper("configure_ovn_gateway.py")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--phase", choices=("all", *PHASES), default="all")
    parser.add_argument("--artifacts", type=Path, default=ROOT / "runtime/charms")
    parser.add_argument("--refresh-charms", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    selected = PHASES if args.phase == "all" else (args.phase,)
    if not args.apply:
        print("Planned native deployment phases: " + ", ".join(selected))
        print(
            "No changes applied. Bootstrap controllers and provide "
            "source-matching packed charms first."
        )
        return
    directory = ROOT / f"runtime/deployment-{time.time_ns()}"
    directory.mkdir(mode=0o700, parents=True)
    targets = owned_infrastructure()
    receipt = {
        "started": time.time(),
        "phases": [],
        "requested": selected,
        "target_uuids": targets,
    }
    actions = {
        "fabric": seed_fabric,
        "workers": workers,
        "agents": agents,
        "policy": lambda: helper("prepare_policy.py"),
        "charms": lambda: helper(
            "deploy_charms.py",
            "--artifacts",
            str(args.artifacts.resolve()),
            "--apply",
            *(("--refresh-existing",) if args.refresh_charms else ()),
        ),
        "routing": routing,
        "overlay": overlay,
        "provider": lambda: provider(directory),
        "gateways": lambda: gateways(directory),
    }
    monitor = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts/monitor.py"),
            "--output",
            str(directory / "resources.jsonl"),
        ]
    )
    try:
        for phase in selected:
            if monitor.poll() is not None:
                raise RuntimeError("Resource monitor exited; deployment stopped")
            if owned_infrastructure() != targets:
                raise RuntimeError("Owned VM identities changed before deployment phase " + phase)
            print("Deployment phase: " + phase, flush=True)
            actions[phase]()
            receipt["phases"].append({"name": phase, "completed": time.time()})
            write_atomic(directory / "receipt.json", json.dumps(receipt, indent=2) + "\n", 0o600)
        if monitor.poll() is not None:
            raise RuntimeError("Resource monitor exited during deployment")
        receipt["completed"] = time.time()
        print("Selected deployment phases completed; full native acceptance remains required")
    finally:
        write_atomic(directory / "receipt.json", json.dumps(receipt, indent=2) + "\n", 0o600)
        if monitor.poll() is None:
            monitor.terminate()
            monitor.wait(timeout=10)


if __name__ == "__main__":
    main()
