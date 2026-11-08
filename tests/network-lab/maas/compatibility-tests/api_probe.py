"""Native MAAS CLI model probes; run inside the disposable MAAS guest.

Requires bootstrap/admin.py. Retains named objects for inspection. This does
not commission or deploy a machine and must not be reported as lifecycle proof.
"""

import argparse
import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path


class Probe:
    def __init__(self):
        self.records = []

    def call(self, label, *args):
        result = subprocess.run(
            ["maas", "lab", *map(str, args)],
            text=True,
            capture_output=True,
            timeout=120,
        )
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError:
            response = result.stdout
        self.records.append(
            {
                "label": label,
                "arguments": list(args),
                "exit_code": result.returncode,
                "response": response,
                "stderr": result.stderr,
            }
        )
        return response if result.returncode == 0 else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    probe = Probe()
    name = "compat-" + uuid.uuid4().hex[:8]
    vlans = []
    try:
        existing = probe.call("initial-subnet-inventory", "subnets", "read")
        occupied = {f"10.204.{i}.0/24" for i in range(1, 5)} | {
            "10.205.0.1/32",
            "10.205.0.2/32",
            "10.205.0.4/31",
        }
        if not isinstance(existing, list):
            raise RuntimeError("Cannot inspect initial subnet inventory")
        if any(subnet["cidr"] in occupied for subnet in existing):
            raise RuntimeError("Test CIDRs already exist; use a fresh disposable controller")
        for rack in (1, 2):
            for tor in ("a", "b"):
                fabric = probe.call(
                    f"fabric-r{rack}-{tor}", "fabrics", "create", f"name={name}-r{rack}-{tor}"
                )
                if not fabric:
                    raise RuntimeError("Cannot create isolated fabric")
                vlan = probe.call(
                    f"vlan-r{rack}-{tor}",
                    "vlans",
                    "create",
                    fabric["id"],
                    "vid=3404",
                    f"name={name}-r{rack}-{tor}",
                )
                if not vlan:
                    raise RuntimeError("Cannot create VLAN")
                vlans.append(vlan)
                probe.call(
                    f"subnet-r{rack}-{tor}",
                    "subnets",
                    "create",
                    f"cidr=10.204.{len(vlans)}.0/24",
                    f"vlan={vlan['id']}",
                    f"name={name}-r{rack}-{tor}",
                )
        probe.call(
            "duplicate-cidr-different-fabric",
            "subnets",
            "create",
            "cidr=10.204.1.0/24",
            f"vlan={vlans[1]['id']}",
            f"name={name}-duplicate",
        )
        for label, cidr, managed in (
            ("host32-managed", "10.205.0.1/32", "true"),
            ("host32-unmanaged", "10.205.0.2/32", "false"),
            ("point31", "10.205.0.4/31", "true"),
        ):
            subnet = probe.call(
                label,
                "subnets",
                "create",
                f"cidr={cidr}",
                f"managed={managed}",
                f"vlan={vlans[0]['id']}",
                f"name={name}-{label}",
            )
            if not subnet:
                continue
            address = cidr.split("/")[0]
            allocated = probe.call(
                label + "-explicit-reservation",
                "ipaddresses",
                "reserve",
                f"ip={address}",
            )
            if allocated:
                probe.call(label + "-release-explicit", "ipaddresses", "release", f"ip={address}")
            allocated = probe.call(
                label + "-automatic-reservation", "ipaddresses", "reserve", f"subnet={cidr}"
            )
            if allocated:
                probe.call(
                    label + "-release-automatic",
                    "ipaddresses",
                    "release",
                    f"ip={allocated['ip']}",
                )
            dynamic = probe.call(
                label + "-dynamic-range",
                "ipranges",
                "create",
                "type=dynamic",
                f"subnet={subnet['id']}",
                f"start_ip={address}",
                f"end_ip={address}",
            )
            if dynamic:
                probe.call(label + "-delete-dynamic-range", "iprange", "delete", dynamic["id"])
            device = probe.call(
                label + "-device",
                "devices",
                "create",
                f"hostname={name}-{label}",
                "mac_addresses=02:"
                + ":".join(uuid.uuid4().hex[i : i + 2] for i in range(0, 10, 2)),
            )
            if device and device.get("interface_set"):
                interface = device["interface_set"][0]["id"]
                probe.call(
                    label + "-interface-vlan",
                    "interface",
                    "update",
                    device["system_id"],
                    interface,
                    f"vlan={vlans[0]['id']}",
                )
                probe.call(
                    label + "-interface-static-link",
                    "interface",
                    "link-subnet",
                    device["system_id"],
                    interface,
                    "mode=STATIC",
                    f"subnet={subnet['id']}",
                    f"ip_address={address}",
                )
        probe.call("fabric-inventory", "fabrics", "read")
        probe.call("subnet-inventory", "subnets", "read")
    finally:
        document = {
            "timestamp": datetime.now(UTC).isoformat(),
            "run": name,
            "boundary": "MAAS API model only; no commissioning or deployment",
            "snap_versions": subprocess.check_output(["snap", "list"], text=True, timeout=30),
            "records": probe.records,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(document, indent=2) + "\n")
    print(f"Recorded {len(probe.records)} native CLI calls in {args.output}")


if __name__ == "__main__":
    main()
