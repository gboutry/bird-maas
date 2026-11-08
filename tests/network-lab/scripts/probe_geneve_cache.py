"""Compare a temporary tenant packet mark with native Geneve egress selection.

Marked skbs are ineligible for Linux tunnel destination caching. The control
is deliberately temporary and refuses an existing tenant qdisc or filter.
"""

import argparse
import json
import re

import deploy_ovn as lab
from measure_geneve import measure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=lab.WORKERS, default="probe")
    parser.add_argument("--target", choices=lab.WORKERS, default="probe-r2a")
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    if args.source == args.target or re.fullmatch(r"[a-z0-9-]+", args.name) is None:
        raise ValueError("Use distinct workers and a lowercase measurement label")
    lab.owned()
    lab.require_source_guards()
    directory = lab.ROOT / "runtime/geneve-flows"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = directory / (args.name + ".json")
    control = directory / (args.name + "-control.json")
    if output.exists() or control.exists():
        raise FileExistsError("Preserve existing native evidence; use a new label")
    name = args.source
    before = json.loads(lab.ssh(name, "tc", "-j", "qdisc", "show", "dev", "nlab-tenant"))
    filters = json.loads(
        lab.ssh(name, "tc", "-j", "filter", "show", "dev", "nlab-tenant", "ingress")
    )
    if any(q["kind"] != "noqueue" for q in before) or filters:
        raise RuntimeError("The diagnostic requires an otherwise unmodified owned tenant veth")
    rules = json.loads(lab.ssh(name, "ip", "-j", "-4", "-N", "rule", "show"))
    if any("fwmark" in r or "fwmask" in r for r in rules):
        raise RuntimeError("Mark-sensitive routing policy prevents this cache control")
    record = {"before_qdiscs": before, "rules": rules}
    lab.ssh(name, "sudo", "tc", "qdisc", "add", "dev", "nlab-tenant", "clsact")
    try:
        lab.ssh(
            name,
            "sudo",
            "tc",
            "filter",
            "add",
            "dev",
            "nlab-tenant",
            "ingress",
            "protocol",
            "ip",
            "pref",
            "49152",
            "matchall",
            "action",
            "skbedit",
            "mark",
            "1",
        )
        measure(name, args.target, output, 2)
        hosts = json.loads((lab.ROOT / "runtime/ovn/manifest.json").read_text())
        result = json.loads(output.read_text())
        record["route_lookups"] = {}
        ports = sorted({p for flow in result["paths"].values() for p in flow["outer_ports"]})
        for port in ports:
            lookups = {}
            for mark in (0, 1):
                rows = json.loads(
                    lab.ssh(
                        name,
                        "ip",
                        "-j",
                        "route",
                        "get",
                        hosts[args.target]["vtep"],
                        "from",
                        hosts[name]["vtep"],
                        "ipproto",
                        "udp",
                        "sport",
                        str(port),
                        "dport",
                        "6081",
                        "mark",
                        str(mark),
                    )
                )
                if len(rows) != 1:
                    raise RuntimeError("Expected one native tuple lookup result")
                lookups[str(mark)] = rows[0]
            record["route_lookups"][str(port)] = lookups
            if any(
                lookups["0"].get(key) != lookups["1"].get(key)
                for key in ("dev", "via", "gateway", "table")
            ):
                raise RuntimeError("Packet marking changed native tuple route selection")
            observed = {
                device
                for flow in result["paths"].values()
                if port in flow["outer_ports"]
                for device in flow["devices"]
            }
            if observed != {lookups["0"].get("dev")}:
                raise RuntimeError("Marked capture differs from the fresh native tuple lookup")
        record["validated"] = True
    finally:
        try:
            record["filter_statistics"] = json.loads(
                lab.ssh(
                    name,
                    "sudo",
                    "tc",
                    "-s",
                    "-j",
                    "filter",
                    "show",
                    "dev",
                    "nlab-tenant",
                    "ingress",
                )
            )
        finally:
            lab.ssh(name, "sudo", "tc", "qdisc", "del", "dev", "nlab-tenant", "clsact")
        record["after_qdiscs"] = json.loads(
            lab.ssh(name, "tc", "-j", "qdisc", "show", "dev", "nlab-tenant")
        )
        record["after_filters"] = json.loads(
            lab.ssh(name, "tc", "-j", "filter", "show", "dev", "nlab-tenant", "ingress")
        )
        if record["after_filters"] or record["after_qdiscs"] != before:
            control.write_text(json.dumps(record, indent=2) + "\n")
            raise RuntimeError("Native tenant qdisc/filter state was not restored")
        record["cleanup_verified"] = True
        control.write_text(json.dumps(record, indent=2) + "\n")
        print("Temporary packet mark removed; native qdisc and filter state restored", flush=True)


if __name__ == "__main__":
    main()
