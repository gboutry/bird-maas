"""Plan owned lab teardown; --apply destroys it and archives private runtime state."""

import argparse
import json
import os
import subprocess
import time

from bootstrap import JUJU, TF, registered_controllers, run
from commission import HOSTS, PROJECT, ROOT, command
from maas_bird.runtime import write_atomic

NETWORKS = {"nlab-mgmt", "nlab-probe-pf0", "nlab-probe-pf1", "nlab-r2-pf0", "nlab-r2-pf1"}
INSTANCES = {*HOSTS, "maas", "fabric", "juju"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Destroy only validated owned resources"
    )
    args = parser.parse_args()
    os.umask(0o077)
    runtime = ROOT / "runtime"
    directory = runtime / f"cleanup-{time.time_ns()}"
    directory.mkdir(mode=0o700, parents=True)
    projects = json.loads(command("lxc", "query", "/1.0/projects?recursion=1"))
    project_exists = any(row["name"] == PROJECT for row in projects)
    instances = (
        json.loads(command("lxc", "query", f"/1.0/instances?project={PROJECT}&recursion=1"))
        if project_exists
        else []
    )
    if any(
        r["name"] not in INSTANCES
        or r["type"] != "virtual-machine"
        or r["config"].get("user.maas-bird-owner") != "network-lab"
        for r in instances
    ):
        raise RuntimeError("Refuse cleanup: project contains unexpected or unowned instances")
    custom = (
        json.loads(
            command("lxc", "query", f"/1.0/storage-pools/default/volumes/custom?project={PROJECT}")
        )
        if project_exists
        else []
    )
    if custom:
        raise RuntimeError("Refuse cleanup: unexpected custom storage volumes exist")
    run("terraform", f"-chdir={TF}", "init", "-input=false")
    plan = directory / "terraform-destroy.plan"
    run(
        "terraform",
        f"-chdir={TF}",
        "plan",
        "-destroy",
        "-input=false",
        "-parallelism=1",
        "-out=" + str(plan),
    )
    parsed = json.loads(command("terraform", f"-chdir={TF}", "show", "-json", str(plan)))
    changes = parsed.get("resource_changes", [])
    deleted = []
    for resource in changes:
        actions = resource["change"]["actions"]
        if actions == ["no-op"]:
            continue
        if actions != ["delete"]:
            raise RuntimeError("Cleanup plan contains an action other than deletion")
        before = resource["change"]["before"]
        kind = resource["type"]
        if kind == "lxd_instance":
            if (
                before["name"] not in INSTANCES
                or before["project"] != PROJECT
                or before["config"].get("user.maas-bird-owner") != "network-lab"
            ):
                raise RuntimeError("Cleanup plan contains an unowned instance")
        elif kind == "lxd_project":
            if before["name"] != PROJECT:
                raise RuntimeError("Cleanup plan contains a different project")
        elif kind == "lxd_network":
            if before["name"] not in NETWORKS or before["project"] != "default":
                raise RuntimeError("Cleanup plan contains an unexpected network")
            actual = json.loads(command("lxc", "query", "/1.0/networks/" + before["name"]))
            for consumer in actual["used_by"]:
                if not (
                    consumer.startswith("/1.0/instances/")
                    and consumer.endswith("?project=" + PROJECT)
                ):
                    raise RuntimeError("A lab carrier has a consumer outside the owned project")
        else:
            raise RuntimeError("Cleanup plan contains an unsupported resource type: " + kind)
        deleted.append(resource["address"])
    controllers = registered_controllers()
    controller = controllers.get("network-lab")
    if controller is not None:
        known = set()
        for path in runtime.glob("bootstrap-*/receipt.json"):
            receipt = json.loads(path.read_text())
            if "juju" in receipt:
                known.add(receipt["juju"]["controller_uuid"])
        identifier = controller.get("uuid", controller.get("controller-uuid"))
        if identifier not in known or controller.get("api-endpoints") != ["10.203.240.12:17070"]:
            raise RuntimeError(
                "Controller registration lacks the native bootstrap ownership receipt"
            )
    receipt = {
        "started": time.time(),
        "apply_requested": args.apply,
        "validated_instance_uuids": {r["name"]: r["config"]["volatile.uuid"] for r in instances},
        "planned_deletions": deleted,
        "controller_unregister_planned": controller is not None,
    }
    write_atomic(directory / "receipt.json", json.dumps(receipt, indent=2) + "\n", 0o600)
    if not args.apply:
        print("Validated teardown plan saved; no resources removed: " + str(directory))
        return
    run("terraform", f"-chdir={TF}", "apply", "-input=false", "-parallelism=1", str(plan))
    projects = json.loads(command("lxc", "query", "/1.0/projects?recursion=1"))
    if any(row["name"] == PROJECT for row in projects):
        raise RuntimeError("Owned project still exists after Terraform teardown")
    for network in NETWORKS:
        check = subprocess.run(
            ["lxc", "query", "/1.0/networks/" + network],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if check.returncode == 0 or "not found" not in check.stderr.lower():
            raise RuntimeError("Carrier absence could not be verified: " + network)
    if controller is not None:
        run(str(JUJU), "unregister", "network-lab", "--no-prompt", timeout=60)
        remaining = registered_controllers()
        if "network-lab" in remaining:
            raise RuntimeError("Local controller registration was not removed")
    receipt["removed_and_verified"] = time.time()
    write_atomic(directory / "receipt.json", json.dumps(receipt, indent=2) + "\n", 0o600)
    ignored = subprocess.run(
        ["git", "check-ignore", str(runtime / "archives/check")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if ignored.returncode:
        raise RuntimeError("Refuse to archive private state outside Git ignore coverage")
    archive = runtime / "archives" / f"cleanup-{time.time_ns()}"
    archive.mkdir(mode=0o700, parents=True)
    for child in list(runtime.iterdir()):
        if child.name != "archives":
            child.rename(archive / child.name)
    print("Owned resources removed and checked; private state archived at " + str(archive))


if __name__ == "__main__":
    main()
