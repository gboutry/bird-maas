"""Install Noble HWE on one owned worker and optionally validate a native reboot.

Fresh MAAS deployments select HWE directly. This helper preserves the existing
Juju machine while upgrading an already deployed lab worker.
"""

import argparse
import json
import re
import subprocess
import time

import deploy_ovn as lab
from routing_expectations import general_paths


def installed_kernel(name):
    depends = lab.ssh(name, "dpkg-query", "-W", "-f=${Depends}", "linux-image-generic-hwe-24.04")
    match = re.search(r"linux-image-([0-9.]+-[0-9]+-generic)", depends)
    if match is None:
        raise RuntimeError("Cannot identify the installed HWE image dependency")
    return match[1]


def forwarding_ready(name, target):
    services = lab.ssh(
        name, "systemctl", "is-active", "bird", "ovn-controller", "network-lab-tenant", timeout=12
    )
    if services.split() != ["active", "active", "active"]:
        raise RuntimeError("Routing, OVN or endpoint service is not active")
    routes = json.loads(lab.ssh(name, "ip", "-j", "-4", "-N", "route", "show", "table", "4400"))
    learned = {r["dst"].removesuffix("/32"): r for r in routes if str(r.get("protocol")) == "12"}
    expected = general_paths(name)
    if set(learned) != set(expected):
        raise RuntimeError("General-purpose destinations differ from the declared topology")
    for address, devices in expected.items():
        route = learned[address]
        hops = route.get("nexthops", [route])
        if len(hops) != len(devices) or {h["dev"] for h in hops} != devices:
            raise RuntimeError("Declared general-purpose nexthops have not recovered")
    packets = lab.ssh(
        name,
        "sudo",
        "ip",
        "netns",
        "exec",
        "nlab-tenant",
        "ping",
        "-n",
        "-M",
        "do",
        "-s",
        "1372",
        "-c",
        "3",
        "-i",
        "0.2",
        "-W",
        "1",
        target,
    )
    if "3 packets transmitted, 3 received, 0% packet loss" not in packets:
        raise RuntimeError("Full-size tenant packet readiness failed")
    return {"services": services, "routes": routes, "tenant_packets": packets}


def service_order(name):
    raw = lab.ssh(
        name,
        "systemctl",
        "show",
        "bird",
        "ovn-controller",
        "network-lab-tenant",
        "-p",
        "Id",
        "-p",
        "ExecMainStartTimestampMonotonic",
        "-p",
        "ActiveEnterTimestampMonotonic",
    )
    units = {}
    for block in raw.split("\n\n"):
        row = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        units[row["Id"]] = {key: int(value) for key, value in row.items() if key != "Id"}
    for unit in ("bird.service", "ovn-controller.service", "network-lab-tenant.service"):
        if not all(units[unit].values()):
            raise RuntimeError("Missing native service-start timestamps")
    if (
        units["bird.service"]["ActiveEnterTimestampMonotonic"]
        > units["ovn-controller.service"]["ExecMainStartTimestampMonotonic"]
    ):
        raise RuntimeError("OVN started before BIRD's admitted activation")
    if (
        units["ovn-controller.service"]["ActiveEnterTimestampMonotonic"]
        > units["network-lab-tenant.service"]["ExecMainStartTimestampMonotonic"]
    ):
        raise RuntimeError("Tenant endpoint started before OVN activation")
    return units


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", choices=lab.WORKERS, required=True)
    parser.add_argument(
        "--reboot",
        action="store_true",
        help="Reboot and verify the running kernel, source guards and packet readiness",
    )
    args = parser.parse_args()
    identities = lab.owned()
    lab.require_source_guards()
    name = args.name
    os_release = json.loads(
        lab.ssh(
            name,
            "python3",
            "-c",
            "import platform,json; print(json.dumps(platform.freedesktop_os_release()))",
        )
    )
    if os_release.get("ID") != "ubuntu" or os_release.get("VERSION_CODENAME") != "noble":
        raise RuntimeError("This helper requires an owned Noble worker")
    if lab.ssh(name, "sudo", "cat", "/etc/openvswitch/system-id.conf") != identities[name]:
        raise RuntimeError("Persist the native OVN UUID before upgrading the worker")
    if lab.ssh(name, "systemctl", "is-enabled", "network-lab-tenant") != "enabled":
        raise RuntimeError("Install the persistent owned tenant endpoint before reboot")
    hosts = json.loads((lab.ROOT / "runtime/ovn/manifest.json").read_text())
    target = next(
        h["address"] for n, h in hosts.items() if lab.HOSTS[n]["rack"] != lab.HOSTS[name]["rack"]
    )
    receipt = {
        "name": name,
        "uuid": identities[name],
        "started": time.time(),
        "before_boot_id": lab.ssh(name, "cat", "/proc/sys/kernel/random/boot_id"),
        "before_kernel": lab.ssh(name, "uname", "-r"),
        "states": [],
    }
    output = lab.ROOT / "runtime" / f"{name}-hwe-{time.time_ns()}.json"
    try:
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
        receipt["expected_kernel"] = installed_kernel(name)
        print(name + ": installed HWE " + receipt["expected_kernel"], flush=True)
        if not args.reboot:
            receipt["running_kernel"] = lab.ssh(name, "uname", "-r")
            return
        try:
            lab.ssh(name, "sudo", "systemctl", "reboot", "--no-wall", timeout=15)
        except subprocess.CalledProcessError as error:
            if error.returncode != 255:
                raise
        deadline = time.monotonic() + 600
        while True:
            try:
                boot = lab.ssh(name, "cat", "/proc/sys/kernel/random/boot_id", timeout=12)
                if boot == receipt["before_boot_id"]:
                    raise RuntimeError("Waiting for a new native boot")
                receipt["after_boot_id"] = boot
                receipt["after_kernel"] = lab.ssh(name, "uname", "-r")
                if receipt["after_kernel"] != receipt["expected_kernel"]:
                    raise ValueError("The worker booted a different kernel")
                receipt["readiness"] = forwarding_ready(name, target)
                break
            except (RuntimeError, subprocess.SubprocessError, OSError) as error:
                receipt["states"].append({"at": time.time(), "state": type(error).__name__})
            if time.monotonic() >= deadline:
                raise TimeoutError("Worker did not regain native HWE packet readiness")
            time.sleep(2)
        lab.owned()
        lab.require_source_guards()
        receipt["service_order"] = service_order(name)
        receipt["completed"] = time.time()
        print(
            name
            + ": new HWE boot, admitted service order and full-size tenant readiness observed",
            flush=True,
        )
    finally:
        output.write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
