"""Release an owned MAAS lab VM for a recorded redeployment experiment."""

import argparse
import json
import time

from commission import HOSTS, PROJECT, ROOT, command, maas


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", choices=tuple(HOSTS), required=True)
    args = parser.parse_args()
    runtime = ROOT / "runtime"
    if args.name != "probe":
        runtime = runtime / args.name
    commissioning = json.loads((runtime / "commission.json").read_text())
    system_id = commissioning["system_id"]
    instance = json.loads(command("lxc", "query", f"/1.0/instances/{args.name}?project={PROJECT}"))
    machine = maas("machine", "read", system_id)
    if (
        instance["config"].get("user.maas-bird-owner") != "network-lab"
        or instance["config"]["volatile.uuid"] != commissioning["lxd_uuid"]
        or machine["hardware_uuid"] != commissioning["lxd_uuid"]
        or machine["hostname"] != args.name
        or machine["status_name"] != "Deployed"
    ):
        raise RuntimeError("Release requires the owned, MAAS-deployed VM")
    receipt = {
        "name": args.name,
        "system_id": system_id,
        "started": time.time(),
        "before": machine,
        "states": [],
        "boundary": "Native MAAS release; actual VM power-off uses LXD because "
        "the enrollment uses the manual power driver.",
    }
    output = runtime / f"release-{int(receipt['started'])}.json"
    try:
        maas("machine", "release", system_id)
        command("lxc", "stop", "--project", PROJECT, args.name, "--timeout=60")
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            machine = maas("machine", "read", system_id)
            state = machine["status_name"]
            receipt["states"].append({"timestamp": time.time(), "state": state})
            print(f"{args.name}: {state}", flush=True)
            if state == "Ready":
                receipt["after"] = machine
                receipt["completed"] = time.time()
                return
            if state.startswith("Failed"):
                raise RuntimeError(f"Release entered {state}")
            time.sleep(5)
        raise TimeoutError("MAAS release did not finish")
    finally:
        output.write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
