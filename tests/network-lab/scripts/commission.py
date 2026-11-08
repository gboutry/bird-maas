"""Commission the owned disk-empty probe through real MAAS and LXD power.

Run on the LXD host after admin/TLS bootstrap and boot image import. Uses the
native MAAS CLI profile inside the owned controller; no BMC credentials or
unrestricted LXD client certificates are given to MAAS.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

PROJECT = "maas-bird-network-lab"
ROOT = Path(__file__).resolve().parents[1]
HOSTS = json.loads((ROOT / "maas/network-model/hosts.json").read_text())


def command(*args):
    return subprocess.check_output(args, text=True, timeout=120).strip()


def maas(*args):
    result = command("lxc", "exec", "--project", PROJECT, "maas", "--", "maas", "lab", *args)
    return json.loads(result) if result else None


def observe(receipt, output, timeout):
    deadline = time.monotonic() + timeout
    previous = None
    try:
        while time.monotonic() < deadline:
            machine = maas("machine", "read", receipt["system_id"])
            state = machine["status_name"]
            receipt["states"].append({"timestamp": time.time(), "state": state})
            output.write_text(json.dumps(receipt, indent=2) + "\n")
            if state != previous:
                print(f"{receipt.get('name', 'probe')}: {state}", flush=True)
                previous = state
            if state == "Ready":
                if machine["hardware_uuid"].lower() != receipt["lxd_uuid"].lower():
                    raise RuntimeError("Commissioned UUID does not match the owned VM")
                receipt["machine"] = machine
                receipt["completed"] = time.time()
                print("Real commissioning completed with matching native UUID", flush=True)
                return
            if state.startswith("Failed"):
                raise RuntimeError(f"Commissioning entered {state}")
            time.sleep(10)
        raise TimeoutError("MAAS commissioning did not reach Ready")
    finally:
        output.write_text(json.dumps(receipt, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--name", choices=tuple(HOSTS), default="probe")
    parser.add_argument(
        "--resume", action="store_true", help="Resume only the recorded commissioning"
    )
    args = parser.parse_args()
    target = args.name
    runtime = ROOT / "runtime"
    if target != "probe":
        runtime = runtime / target
    runtime.mkdir(parents=True, exist_ok=True)
    instances = {
        name: json.loads(command("lxc", "query", f"/1.0/instances/{name}?project={PROJECT}"))
        for name in ("maas", target)
    }
    for instance in instances.values():
        if instance["config"].get("user.maas-bird-owner") != "network-lab":
            raise RuntimeError("Instance ownership check failed")
    probe = instances[target]
    if args.resume:
        output = runtime / "commission.json"
        receipt = json.loads(output.read_text())
        if receipt["lxd_uuid"] != probe["config"]["volatile.uuid"]:
            raise RuntimeError("Recorded commissioning belongs to another VM")
        machine = maas("machine", "read", receipt["system_id"])
        actual_macs = {i["mac_address"] for i in machine["interface_set"]}
        if machine["hostname"] != target or not set(receipt["macs"]) <= actual_macs:
            raise RuntimeError("Recorded MAAS machine identity does not match")
        if machine["status_name"] != "Commissioning":
            raise RuntimeError("Only a still-commissioning machine may be resumed")
        receipt.setdefault("resumed", []).append(time.time())
        if probe["status"] == "Stopped":
            command("lxc", "start", "--project", PROJECT, target)
        observe(receipt, output, args.timeout)
        return
    if probe["status"] != "Stopped":
        raise RuntimeError("Probe must be stopped before initial commissioning")
    if command("lxc", "network", "get", "nlab-mgmt", "ipv4.dhcp") != "false":
        raise RuntimeError("LXD DHCP must be disabled before enabling MAAS DHCP")
    machines = maas("machines", "read")
    if any(m["hostname"] == target for m in machines):
        raise RuntimeError(
            "Probe is already enrolled; inspect its state instead of recommissioning"
        )
    key = runtime / "ssh-key"
    if not key.exists():
        command("ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key))
    public = key.with_suffix(".pub").read_text().strip()
    known = maas("sshkeys", "read")
    if not any(public.split()[1] in k["key"] for k in known):
        maas("sshkeys", "create", "key=" + public)
    subnets = maas("subnets", "read")
    management = next(s for s in subnets if s["cidr"] == "10.203.240.0/24")
    ranges = maas("ipranges", "read")
    if not any(r["start_ip"] == "10.203.240.100" for r in ranges):
        maas(
            "ipranges",
            "create",
            "type=dynamic",
            f"subnet={management['id']}",
            "start_ip=10.203.240.100",
            "end_ip=10.203.240.199",
        )
    rack = next(r for r in maas("rack-controllers", "read") if r["hostname"] == "maas")
    vlan = management["vlan"]
    maas(
        "vlan",
        "update",
        str(vlan["fabric_id"]),
        str(vlan["vid"]),
        "dhcp_on=true",
        "primary_rack=" + rack["system_id"],
    )
    macs = [
        probe["config"][f"volatile.{name}.hwaddr"] for name in ("mgmt0", "mgmt1", "pf0", "pf1")
    ]
    machine = maas(
        "machines",
        "create",
        f"hostname={target}",
        "architecture=amd64/generic",
        "power_type=manual",
        "commission=false",
        *("mac_addresses=" + mac for mac in macs),
    )
    system_id = machine["system_id"]
    receipt = {
        "name": target,
        "system_id": system_id,
        "lxd_uuid": probe["config"]["volatile.uuid"],
        "macs": macs,
        "started": time.time(),
        "states": [],
        "boundary": "real MAAS commissioning of a virtual machine; no hardware VF proof",
    }
    output = runtime / "commission.json"
    try:
        maas(
            "machine",
            "commission",
            system_id,
            "testing_scripts=none",
            "enable_ssh=1",
            "skip_bmc_config=1",
        )
        command("lxc", "start", "--project", PROJECT, target)
        observe(receipt, output, args.timeout)
    finally:
        output.write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
