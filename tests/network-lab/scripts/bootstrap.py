"""Plan or bootstrap the owned Terraform infrastructure, MAAS and manual Juju controller."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from commission import PROJECT, ROOT, command, maas
from maas_bird.runtime import write_atomic

TF = ROOT / "lxd/terraform"
RUNTIME = ROOT / "runtime"
JUJU = Path("/snap/juju/current/bin/juju")


def registered_controllers():
    controllers = json.loads(command(str(JUJU), "controllers", "--format=json"))["controllers"]
    if controllers is None:
        return {}
    if not isinstance(controllers, dict):
        raise RuntimeError("Juju controller registry is not an object or null")
    return controllers


def run(*args, timeout=1800, env=None):
    subprocess.run(args, check=True, timeout=timeout, env=env)


def owned(name, cpu, memory):
    row = json.loads(command("lxc", "query", f"/1.0/instances/{name}?project={PROJECT}"))
    if (
        row["type"] != "virtual-machine"
        or row["config"].get("user.maas-bird-owner") != "network-lab"
        or row["config"]["limits.cpu"] != str(cpu)
        or row["config"]["limits.memory"] != memory
    ):
        raise RuntimeError("Bootstrap target is not the expected owned, capped VM")
    return row["config"]["volatile.uuid"]


def controller_key():
    key = RUNTIME / "juju-controller-key"
    public = key.with_suffix(".pub")
    if key.exists() != public.exists():
        raise RuntimeError("Controller SSH key pair is incomplete; refuse silent rotation")
    if not key.exists():
        run(
            "ssh-keygen",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "network-lab-juju-controller",
            "-f",
            str(key),
            timeout=60,
        )
    if key.stat().st_mode & 0o077:
        raise RuntimeError("Controller private key must be private")
    derived = command("ssh-keygen", "-y", "-P", "", "-f", str(key)).split()[:2]
    if derived != public.read_text().split()[:2]:
        raise RuntimeError("Controller public and private keys differ")


def infrastructure(directory, apply):
    controller_key()
    run("terraform", f"-chdir={TF}", "init", "-input=false")
    plan = directory / "terraform.plan"
    run("terraform", f"-chdir={TF}", "plan", "-input=false", "-parallelism=1", "-out=" + str(plan))
    parsed = json.loads(command("terraform", f"-chdir={TF}", "show", "-json", str(plan)))
    changes = parsed.get("resource_changes", [])
    if apply and any("delete" in r["change"]["actions"] for r in changes):
        raise RuntimeError("Bootstrap refuses deletion/replacement; inspect the saved plan")
    for change in changes:
        before = change["change"].get("before")
        if before and change["type"] == "lxd_instance":
            if (
                before.get("project") != PROJECT
                or before.get("config", {}).get("user.maas-bird-owner") != "network-lab"
            ):
                raise RuntimeError("Terraform plan contains an unowned existing instance")
    summary = [{"address": r["address"], "actions": r["change"]["actions"]} for r in changes]
    write_atomic(directory / "terraform-actions.json", json.dumps(summary, indent=2) + "\n", 0o600)
    if apply:
        run("terraform", f"-chdir={TF}", "apply", "-input=false", "-parallelism=1", str(plan))
        owned("maas", 4, "8GiB")
        owned("fabric", 2, "8GiB")
        owned("juju", 2, "4GiB")
    return summary


def maas_bootstrap():
    identity = owned("maas", 4, "8GiB")
    prefix = ("lxc", "exec", "--project", PROJECT, "maas", "--")
    run(*prefix, "cloud-init", "status", "--wait")
    for script in ("admin.py", "tls.py"):
        run(
            "lxc",
            "file",
            "push",
            "--project",
            PROJECT,
            str(ROOT / "maas/bootstrap" / script),
            "maas/root/" + script,
            "--mode=0600",
        )
    run(*prefix, "python3", "/root/tls.py")
    run(*prefix, "python3", "/root/admin.py", "--tls")
    run(
        "lxc",
        "file",
        "pull",
        "--project",
        PROJECT,
        "maas/var/snap/maas/common/network-lab-tls/ca.crt",
        str(RUNTIME / "maas-ca.crt"),
    )
    run(
        "curl",
        "--fail",
        "--cacert",
        str(RUNTIME / "maas-ca.crt"),
        "https://10.203.240.10:5443/MAAS/api/2.0/version/",
    )
    sources = maas("boot-sources", "read")
    if len(sources) != 1 or not sources[0]["url"].endswith("images.maas.io/ephemeral-v3/stable"):
        raise RuntimeError("Expected the dedicated controller's default signed-image source")
    source = str(sources[0]["id"])
    selections = maas("boot-source-selections", "read", source)
    if len(selections) > 1:
        raise RuntimeError("Refuse to replace multiple existing boot-image selections")
    params = (
        "os=ubuntu",
        "release=noble",
        "arches=amd64",
        "subarches=ga-24.04",
        "subarches=hwe-24.04",
        "labels=*",
    )
    if selections:
        maas("boot-source-selection", "update", source, str(selections[0]["id"]), *params)
    else:
        maas("boot-source-selections", "create", source, *params)

    def complete():
        resources = maas("boot-resources", "read")
        wanted = [
            ("ubuntu/noble", "amd64/ga-24.04"),
            ("ubuntu/noble", "amd64/hwe-24.04"),
            ("grub-efi-signed/uefi", "amd64/generic"),
        ]
        result = {}
        for name, architecture in wanted:
            matches = [
                r for r in resources if r["name"] == name and r["architecture"] == architecture
            ]
            if len(matches) != 1:
                return None
            item = maas("boot-resource", "read", str(matches[0]["id"]))
            sets = item.get("sets", {})
            if not sets or not sets[max(sets)].get("complete"):
                return None
            result[name + ":" + architecture] = {"id": item["id"], "set": max(sets)}
        return result

    ready = complete()
    if ready is None:
        maas("boot-resources", "import")
        deadline = time.monotonic() + 3600
        while ready is None:
            if time.monotonic() >= deadline:
                raise TimeoutError("Native Noble GA/HWE boot-image import did not complete")
            time.sleep(5)
            ready = complete()
    return {"uuid": identity, "boot_resources": ready}


def juju_bootstrap():
    identity = owned("juju", 2, "4GiB")
    run("lxc", "exec", "--project", PROJECT, "juju", "--", "cloud-init", "status", "--wait")
    run(sys.executable, str(ROOT / "scripts/prepare_ssh.py"), "--scope", "controller")
    env = {**os.environ, "PATH": str(RUNTIME / "juju-bootstrap-bin") + ":" + os.environ["PATH"]}
    controllers = registered_controllers()
    if "network-lab" not in controllers:
        run(
            str(JUJU),
            "bootstrap",
            "manual/ubuntu@10.203.240.12",
            "network-lab",
            "--agent-version=3.6.29",
            "--bootstrap-base=ubuntu@24.04",
            "--config",
            "bootstrap-timeout=1200",
            "--no-switch",
            timeout=1800,
            env=env,
        )
    details = json.loads(command(str(JUJU), "show-controller", "network-lab", "--format=json"))[
        "network-lab"
    ]["details"]
    if (
        details["cloud"] != "manual"
        or details["api-endpoints"] != ["10.203.240.12:17070"]
        or details["agent-version"] != "3.6.29"
    ):
        raise RuntimeError("Existing controller differs from the dedicated lab endpoint/version")
    return {
        "vm_uuid": identity,
        "controller_uuid": details["uuid"],
        "agent_version": details["agent-version"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Apply infrastructure/bootstrap stages"
    )
    parser.add_argument(
        "--phase", choices=("all", "infrastructure", "maas", "juju"), default="all"
    )
    args = parser.parse_args()
    os.umask(0o077)
    for executable in ("terraform", "lxc", "ssh-keygen", "curl"):
        if shutil.which(executable) is None:
            raise RuntimeError("Missing required native CLI: " + executable)
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = RUNTIME / f"bootstrap-{time.time_ns()}"
    directory.mkdir(mode=0o700)
    monitor = None
    receipt = {"phase": args.phase, "apply_requested": args.apply, "started": time.time()}
    try:
        if args.apply:
            monitor = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "scripts/monitor.py"),
                    "--output",
                    str(directory / "resources.jsonl"),
                ]
            )
        if not args.apply or args.phase in ("all", "infrastructure"):
            receipt["infrastructure"] = infrastructure(directory, args.apply)
        if args.apply and args.phase in ("all", "maas"):
            receipt["maas"] = maas_bootstrap()
        if args.apply and args.phase in ("all", "juju"):
            receipt["juju"] = juju_bootstrap()
        if monitor is not None and monitor.poll() is not None:
            raise RuntimeError("Resource monitor exited during bootstrap")
        receipt["completed"] = time.time()
        print(
            "Bootstrap stages completed" if args.apply else "Saved Terraform plan; nothing applied"
        )
    finally:
        if monitor is not None and monitor.poll() is None:
            monitor.terminate()
            monitor.wait(timeout=10)
        write_atomic(directory / "receipt.json", json.dumps(receipt, indent=2) + "\n", 0o600)


if __name__ == "__main__":
    main()
