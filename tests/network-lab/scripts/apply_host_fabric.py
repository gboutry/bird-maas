"""Validate compiled FRR files and optionally replace the owned running fabric.

The native parser runs before any live configuration replacement. Applying
does not establish BGP convergence or data-plane acceptance.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

import yaml
from commission import HOSTS, PROJECT, ROOT, command
from maas_bird.runtime import write_atomic
from render_host_fabric import NODES

CURRENT = "/opt/network-lab/host-fabric"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    source = args.directory.resolve()
    if not source.is_relative_to((ROOT / "runtime").resolve()):
        raise ValueError("Compiled configuration must be under the lab runtime directory")
    topology = yaml.safe_load((source / "topology.clab.yml").read_text())
    expected = yaml.safe_load((ROOT / "containerlab/topology.clab.yml").read_text())
    if topology != expected:
        raise ValueError("Compiled topology differs from the tracked lab topology")
    manifest = json.loads((source / "public-plan.json").read_text())
    if "provider_intent" in manifest and manifest["provider_intent"] != json.loads(
        (ROOT / "containerlab/provider.json").read_text()
    ):
        raise ValueError("Compiled provider intent differs from the tracked service")
    if "gateway_intent" in manifest and (
        "provider_intent" not in manifest
        or manifest["gateway_intent"]
        != json.loads((ROOT / "containerlab/gateways.json").read_text())
    ):
        raise ValueError("Compiled gateway intent differs from the tracked service")
    if set(manifest["worker_uuids"]) != set(HOSTS):
        raise ValueError("Manifest must contain exactly the four enrolled workers")
    for name in ("fabric", *HOSTS):
        instance = json.loads(command("lxc", "query", f"/1.0/instances/{name}?project={PROJECT}"))
        if (
            instance["type"] != "virtual-machine"
            or instance["config"].get("user.maas-bird-owner") != "network-lab"
            or instance["config"]["limits.cpu"] != "2"
            or instance["config"]["limits.memory"] != ("8GiB" if name == "fabric" else "4GiB")
        ):
            raise RuntimeError("Fabric deployment requires the owned, capped VMs")
        if (
            name != "fabric"
            and instance["config"]["volatile.uuid"] != manifest["worker_uuids"][name]
        ):
            raise RuntimeError("Compiled worker identity differs from LXD")
    files = ["topology.clab.yml", "public-plan.json", "frr/daemons"] + [
        f"frr/{node}.conf" for node in NODES
    ]
    for name in files:
        path = source / name
        if not path.resolve().is_relative_to(source) or path.stat().st_mode & 0o077:
            raise ValueError("Compiled files must be private and remain inside their directory")
    stamp = time.time_ns()
    log_path = ROOT / f"runtime/fabric-apply-{stamp}.json"
    commands: list[dict[str, object]] = []
    log: dict[str, object] = {
        "source": str(source),
        "apply_requested": args.apply,
        "commands": commands,
    }

    def run(*argv):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=180)
        commands.append(
            {
                "argv": argv,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        write_atomic(log_path, json.dumps(log, indent=2) + "\n", 0o600)
        if result.returncode:
            raise RuntimeError(f"Native command failed; inspect private log {log_path}")
        return result

    def guest(*argv):
        return run("lxc", "exec", "--project", PROJECT, "fabric", "--", *argv)

    provider_installed = (
        guest(
            "python3",
            "-c",
            "from pathlib import Path; "
            "print(Path('/etc/systemd/system/network-lab-provider.service').is_file())",
        ).stdout.strip()
        == "True"
    )
    if args.apply and provider_installed and "provider_intent" not in manifest:
        raise ValueError("Remove the owned provider service before applying a plain fabric")
    gateways_installed = (
        guest(
            "python3",
            "-c",
            "from pathlib import Path; "
            "print(Path('/etc/systemd/system/network-lab-gateways.service').is_file())",
        ).stdout.strip()
        == "True"
    )
    if args.apply and gateways_installed and "gateway_intent" not in manifest:
        raise ValueError(
            "Remove the owned gateway service before applying a fabric without gateways"
        )

    def deploy():
        if gateways_installed:
            guest("systemctl", "stop", "network-lab-gateways.service")
        if provider_installed:
            guest("systemctl", "stop", "network-lab-provider.service")
        guest(
            "containerlab",
            "deploy",
            "--topo",
            CURRENT + "/topology.clab.yml",
            "--reconfigure",
            "--max-workers",
            "1",
        )
        if provider_installed:
            guest("systemctl", "start", "network-lab-provider.service")
        if gateways_installed:
            guest("systemctl", "start", "network-lab-gateways.service")

    stage = f"/opt/network-lab/fabric-stage-{stamp}"
    guest("install", "-d", "-m", "700", stage, stage + "/frr")
    for name in files:
        run(
            "lxc",
            "file",
            "push",
            "--project",
            PROJECT,
            str(source / name),
            "fabric" + stage + "/" + name,
            "--mode=0600",
        )
    for node in NODES:
        container = "clab-maas-bird-fabric-" + node
        labels = json.loads(
            guest("docker", "inspect", "--format", "{{json .Config.Labels}}", container).stdout
        )
        if labels.get("containerlab") != "maas-bird-fabric":
            raise RuntimeError("Native parser target is not part of the owned topology")
        temporary = f"/tmp/network-lab-{stamp}.conf"
        guest("docker", "cp", stage + f"/frr/{node}.conf", container + ":" + temporary)
        try:
            check = guest("docker", "exec", container, "vtysh", "-C", "-f", temporary)
            if check.stdout.strip() or check.stderr.strip():
                raise RuntimeError(f"Native FRR parser reported diagnostics; inspect {log_path}")
        finally:
            guest("docker", "exec", container, "rm", "-f", temporary)
    print("All six compiled FRR configurations passed native vtysh validation", flush=True)
    if not args.apply:
        return
    guest("cp", "-a", CURRENT + "/frr", stage + "/previous-frr")
    guest("cp", "-a", CURRENT + "/topology.clab.yml", stage + "/previous-topology.clab.yml")
    had_manifest = "public-plan.json" in guest("ls", "-1", CURRENT).stdout.splitlines()
    if had_manifest:
        guest("cp", "-a", CURRENT + "/public-plan.json", stage + "/previous-public-plan.json")
    try:
        for name in files:
            guest("cp", "--", stage + "/" + name, CURRENT + "/" + name)
        deploy()
    except Exception:
        guest("cp", "-a", stage + "/previous-frr/.", CURRENT + "/frr/")
        guest("cp", "--", stage + "/previous-topology.clab.yml", CURRENT + "/topology.clab.yml")
        if had_manifest:
            guest("cp", "--", stage + "/previous-public-plan.json", CURRENT + "/public-plan.json")
        else:
            guest("rm", "-f", CURRENT + "/public-plan.json")
        deploy()
        raise
    for name in files:
        write_atomic(ROOT / "runtime/host-fabric" / name, (source / name).read_text(), 0o600)
    log["applied_at"] = time.time()
    log["backup_directory"] = stage
    write_atomic(log_path, json.dumps(log, indent=2) + "\n", 0o600)
    print("Applied the compiled fabric; BGP and packet acceptance remain required", flush=True)


if __name__ == "__main__":
    main()
