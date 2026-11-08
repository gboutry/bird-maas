"""Configure and deploy the commissioned probe through native MAAS APIs.

Deploy interface /32s, a /31 baseline or unnumbered links with a loopback /32.
Run only after commission.py reached Ready. No host BIRD configuration is injected.
"""

import argparse
import base64
import json
import time

from commission import HOSTS, PROJECT, ROOT, command, maas
from generate_roles import netplan, roles


def fast_address(target, model, index):
    return HOSTS[target][model][index]


def loopback_user_data(target, runtime, *, logical_roles=False):
    """Use MAAS's supported deployment hook; its API has no loopback action."""
    address = HOSTS[target]["loopback"]
    fragment = {
        "network": {
            "version": 2,
            "ethernets": {
                "lo": {
                    "match": {"name": "lo"},
                    "accept-ra": False,
                    "link-local": [],
                    "optional": True,
                    "addresses": [address],
                    "routes": [
                        {"to": "0.0.0.0/0", "type": "unreachable", "table": 4400, "metric": 42760}
                    ],
                    "routing-policy": [{"from": address, "table": 4400, "priority": 14000}],
                },
                **{
                    name: {"accept-ra": False, "link-local": [], "optional": True}
                    for name in ("enp7s0", "enp8s0")
                },
            },
            "vlans": {
                name + ".3404": {
                    "accept-ra": False,
                    "link-local": ["ipv6"],
                    "optional": True,
                }
                for name in ("enp7s0", "enp8s0")
            },
        }
    }
    if logical_roles:
        fragment = netplan(target)
    content = json.dumps(fragment, indent=2) + "\n"
    (runtime / "loopback-netplan.yaml").write_text(content)
    config = {
        "write_files": [
            {
                "path": "/etc/netplan/99-zzzz-network-lab.yaml",
                "permissions": "0600",
                "owner": "root:root",
                "content": content,
            }
        ],
        "runcmd": [["netplan", "apply"]],
    }
    if logical_roles:
        config["packages"] = ["lldpd"]
        config["write_files"].append(
            {
                "path": "/etc/default/lldpd",
                "permissions": "0644",
                "owner": "root:root",
                "content": (ROOT / "juju/bird/lldpd.default").read_text(),
            }
        )
        config["runcmd"].extend(
            [["systemctl", "enable", "lldpd"], ["systemctl", "restart", "lldpd"]]
        )
    user_data = "#cloud-config\n" + json.dumps(config, indent=2) + "\n"
    (runtime / "user-data.yaml").write_text(user_data)
    return "user_data=" + base64.b64encode(user_data.encode()).decode()


def deployment_user_data(target, runtime, model, *, role_loopbacks=False):
    """Compose the /31 compatibility variant without changing BGP peer intent."""
    if role_loopbacks and model != "point31":
        raise ValueError("Role loopbacks are an explicit point31 comparison option")
    if model == "roles" or role_loopbacks:
        return [loopback_user_data(target, runtime, logical_roles=True)]
    if model == "loopback32":
        return [loopback_user_data(target, runtime)]
    return []


def unlink(system_id, interface):
    for link in interface["links"]:
        maas("interface", "unlink-subnet", system_id, str(interface["id"]), f"id={link['id']}")


def configure_roles(system_id, *, preserve_general=False):
    """Create role VLANs while optionally retaining configured GP addressing."""
    machine = maas("machine", "read", system_id)
    if machine["status_name"] != "Ready":
        raise RuntimeError("Role interfaces require a Ready machine")
    interfaces = {i["name"]: i for i in machine["interface_set"]}
    for parent in ("enp7s0", "enp8s0"):
        fabric_id = interfaces[parent]["vlan"]["fabric_id"]
        vlans = maas("vlans", "read", str(fabric_id))
        for role in roles():
            vid = role["vlan"]
            vlan = next((v for v in vlans if v["vid"] == vid), None)
            if vlan is None:
                vlan = maas(
                    "vlans",
                    "create",
                    str(fabric_id),
                    f"vid={vid}",
                    f"name={role['id']}",
                    "mtu=1500",
                )
                vlans.append(vlan)
            name = f"{parent}.{vid}"
            child = interfaces.get(name)
            if child is None:
                child = maas(
                    "interfaces",
                    "create-vlan",
                    system_id,
                    f"parent={interfaces[parent]['id']}",
                    f"vlan={vlan['id']}",
                )
            if child["parents"] != [parent] or child["vlan"]["id"] != vlan["id"]:
                raise RuntimeError("Role VLAN belongs to a different parent or fabric")
            if preserve_general and role["id"] == "general":
                if len(child["links"]) != 1 or child["links"][0]["mode"] != "static":
                    raise RuntimeError(
                        "The numbered GP comparison requires its existing static link"
                    )
                continue
            unlink(system_id, child)
            maas("interface", "link-subnet", system_id, str(child["id"]), "mode=LINK_UP")


def validate_roles(machine, *, preserve_general=False):
    interfaces = {i["name"]: i for i in machine["interface_set"]}
    for parent in ("enp7s0", "enp8s0"):
        for role in roles():
            child = interfaces[f"{parent}.{role['vlan']}"]
            if (
                child["type"] != "vlan"
                or child["parents"] != [parent]
                or child["vlan"]["vid"] != role["vlan"]
                or child["vlan"]["fabric_id"] != interfaces[parent]["vlan"]["fabric_id"]
                or len(child["links"]) != 1
            ):
                raise RuntimeError("Role interface intent differs from the deployment")
            link = child["links"][0]
            if preserve_general and role["id"] == "general":
                if link["mode"] != "static" or not link.get("ip_address"):
                    raise RuntimeError("The numbered GP comparison requires its static address")
            elif link["mode"] != "link_up" or link.get("ip_address"):
                raise RuntimeError("Role interface intent differs from the deployment")


def configure(system_id, machine, runtime, target, model, management_ip):
    interfaces = {i["name"]: i for i in machine["interface_set"]}
    management = next(s for s in maas("subnets", "read") if s["cidr"] == "10.203.240.0/24")
    bond = interfaces.get("bond0")
    if bond is None:
        for name in ("enp5s0", "enp6s0"):
            unlink(system_id, interfaces[name])
            maas(
                "interface",
                "update",
                system_id,
                str(interfaces[name]["id"]),
                f"vlan={management['vlan']['id']}",
            )
        bond = maas(
            "interfaces",
            "create-bond",
            system_id,
            "name=bond0",
            "bond_mode=active-backup",
            "bond_miimon=100",
            f"vlan={management['vlan']['id']}",
            f"parents={interfaces['enp5s0']['id']}",
            f"parents={interfaces['enp6s0']['id']}",
        )
    if (
        bond["type"] != "bond"
        or bond["params"]["bond_mode"] != "active-backup"
        or set(bond["parents"]) != {"enp5s0", "enp6s0"}
        or bond["vlan"]["id"] != management["vlan"]["id"]
    ):
        raise RuntimeError("Existing management bond does not match the lab")
    if [link.get("ip_address") for link in bond["links"]] != [management_ip]:
        unlink(system_id, bond)
        maas(
            "interface",
            "link-subnet",
            system_id,
            str(bond["id"]),
            "mode=STATIC",
            f"subnet={management['id']}",
            f"ip_address={management_ip}",
            "default_gateway=true",
        )
    fabrics = maas("fabrics", "read")
    for index, name in enumerate(("enp7s0", "enp8s0")):
        tor = "a" if index == 0 else "b"
        rack = HOSTS[target]["rack"]
        fabric = next(f for f in fabrics if f["name"].endswith(f"-r{rack}-{tor}"))
        untagged = next(v for v in fabric["vlans"] if v["vid"] == 0)
        tagged = next(v for v in fabric["vlans"] if v["vid"] == 3404)
        child = interfaces.get(name + ".3404")
        if child is None:
            unlink(system_id, interfaces[name])
            maas(
                "interface",
                "update",
                system_id,
                str(interfaces[name]["id"]),
                f"vlan={untagged['id']}",
            )
            child = maas(
                "interfaces",
                "create-vlan",
                system_id,
                f"parent={interfaces[name]['id']}",
                f"vlan={tagged['id']}",
            )
        if child["parents"] != [name] or child["vlan"]["id"] != tagged["id"]:
            raise RuntimeError("Existing fast VLAN does not match the lab fabric")
        if model == "loopback32":
            unlink(system_id, child)
            maas("interface", "link-subnet", system_id, str(child["id"]), "mode=LINK_UP")
            continue
        cidr = fast_address(target, model, index)
        address = cidr.split("/")[0]
        subnet = next((s for s in maas("subnets", "read") if s["cidr"] == cidr), None)
        if subnet is None:
            subnet = maas(
                "subnets",
                "create",
                f"cidr={cidr}",
                f"vlan={tagged['id']}",
                f"name={target}-{model}-{tor}",
            )
        if subnet["vlan"]["id"] != tagged["id"]:
            raise RuntimeError("Existing CIDR belongs to another VLAN")
        unlink(system_id, child)
        maas(
            "interface",
            "link-subnet",
            system_id,
            str(child["id"]),
            "mode=STATIC",
            f"subnet={subnet['id']}",
            f"ip_address={address}",
        )
    configured = maas("machine", "read", system_id)
    (runtime / "probe-configured.json").write_text(json.dumps(configured, indent=2) + "\n")


def validate_configuration(machine, target, model, management_ip):
    interfaces = {i["name"]: i for i in machine["interface_set"]}
    bond = interfaces["bond0"]
    if (
        bond["type"] != "bond"
        or bond["params"]["bond_mode"] != "active-backup"
        or set(bond["parents"]) != {"enp5s0", "enp6s0"}
        or [link.get("ip_address") for link in bond["links"]] != [management_ip]
    ):
        raise RuntimeError("Management bond intent differs from the test")
    fabrics = set()
    for index, parent in enumerate(("enp7s0", "enp8s0")):
        child = interfaces[parent + ".3404"]
        fabrics.add(child["vlan"]["fabric_id"])
        if (
            child["type"] != "vlan"
            or child["vlan"]["vid"] != 3404
            or child["parents"] != [parent]
            or len(child["links"]) != 1
        ):
            raise RuntimeError("Fast-interface intent differs from the test")
        link = child["links"][0]
        if model == "loopback32":
            if link["mode"] != "link_up" or link.get("ip_address"):
                raise RuntimeError("Unnumbered fast interface must have LINK_UP only")
        elif link.get("ip_address") != fast_address(target, model, index).split("/")[0] or link[
            "subnet"
        ]["cidr"] != fast_address(target, model, index):
            raise RuntimeError("Fast-interface addressing differs from the test")
    if len(fabrics) != 2:
        raise RuntimeError("Fast links must belong to two isolated MAAS fabrics")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", choices=tuple(HOSTS), default="probe")
    parser.add_argument(
        "--kernel",
        choices=("ga-24.04", "hwe-24.04"),
        help="New deployments default to Noble HWE; resume retains the recorded kernel",
    )
    parser.add_argument(
        "--model", choices=("interface32", "point31", "loopback32", "roles"), default="interface32"
    )
    parser.add_argument(
        "--role-loopbacks",
        action="store_true",
        help="With --model point31, retain role loopback sources and IPv6-link-local BGP",
    )
    continuation = parser.add_mutually_exclusive_group()
    continuation.add_argument(
        "--deploy-configured",
        action="store_true",
        help="Validate and deploy the already configured Ready probe",
    )
    continuation.add_argument(
        "--resume", action="store_true", help="Observe a recorded deployment"
    )
    args = parser.parse_args()
    if args.role_loopbacks and args.model != "point31":
        parser.error("--role-loopbacks requires --model point31")
    target = args.name
    node_runtime = ROOT / "runtime"
    if target != "probe":
        node_runtime = node_runtime / target
    recipe = "point31-role-loopbacks" if args.role_loopbacks else args.model
    runtime = node_runtime if args.model == "interface32" else node_runtime / recipe
    runtime.mkdir(parents=True, exist_ok=True)
    management_ip = HOSTS[target]["management_ip"]
    commissioning = json.loads((node_runtime / "commission.json").read_text())
    system_id = commissioning["system_id"]
    machine = maas("machine", "read", system_id)
    instance = json.loads(command("lxc", "query", f"/1.0/instances/{target}?project={PROJECT}"))
    if (
        instance["config"].get("user.maas-bird-owner") != "network-lab"
        or instance["config"]["volatile.uuid"] != commissioning["lxd_uuid"]
        or machine["hardware_uuid"] != instance["config"]["volatile.uuid"]
        or machine["hostname"] != target
        or machine["power_type"] != "manual"
        or machine["status_name"] != ("Deploying" if args.resume else "Ready")
    ):
        raise RuntimeError("Deployment requires the owned VM in its expected MAAS state")
    addressing_model = "loopback32" if args.model == "roles" else args.model
    if not args.deploy_configured and not args.resume:
        configure(system_id, machine, runtime, target, addressing_model, management_ip)
        if args.model == "roles" or args.role_loopbacks:
            configure_roles(system_id, preserve_general=args.role_loopbacks)
    configured = maas("machine", "read", system_id)
    validate_configuration(configured, target, addressing_model, management_ip)
    if args.model == "roles" or args.role_loopbacks:
        validate_roles(configured, preserve_general=args.role_loopbacks)
    output = runtime / f"deploy-{args.model}.json"
    if args.resume:
        receipt = json.loads(output.read_text())
        if (
            receipt["system_id"] != system_id
            or receipt["model"] != args.model
            or receipt.get("role_loopbacks", False) != args.role_loopbacks
        ):
            raise RuntimeError("Recorded deployment does not match the selected machine/model")
        kernel = receipt.get("kernel", "ga-24.04")
        if args.kernel is not None and args.kernel != kernel:
            raise RuntimeError("Cannot change the kernel selection of an in-flight deployment")
        receipt.setdefault("resumed", []).append(time.time())
    else:
        kernel = args.kernel or "hwe-24.04"
        maas("machines", "allocate", f"system_id={system_id}")
        receipt = {
            "system_id": system_id,
            "started": time.time(),
            "model": args.model,
            "kernel": kernel,
            "lxd_uuid": instance["config"]["volatile.uuid"],
            "states": [],
        }
    if args.role_loopbacks:
        receipt["role_loopbacks"] = True
    states = receipt["states"]
    if not isinstance(states, list):
        raise RuntimeError("Recorded deployment states must be a list")
    try:
        if not args.resume:
            extra = deployment_user_data(
                target, runtime, args.model, role_loopbacks=args.role_loopbacks
            )
            maas(
                "machine",
                "deploy",
                system_id,
                "distro_series=noble",
                f"hwe_kernel={kernel}",
                *extra,
            )
        live = json.loads(command("lxc", "query", f"/1.0/instances/{target}?project={PROJECT}"))
        if (
            live["config"].get("user.maas-bird-owner") != "network-lab"
            or live["config"]["volatile.uuid"] != commissioning["lxd_uuid"]
        ):
            raise RuntimeError("VM ownership changed before the power operation")
        if live["status"] == "Stopped":
            command("lxc", "start", "--project", PROJECT, target)
            receipt["power_action"] = "start"
        elif live["status"] == "Running":
            if not args.resume:
                command("lxc", "restart", "--project", PROJECT, target, "--force")
            receipt["power_action"] = "observe" if args.resume else "restart"
        else:
            raise RuntimeError("Unexpected VM state; refusing a power operation")
        previous = None
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            machine = maas("machine", "read", system_id)
            state = machine["status_name"]
            states.append({"timestamp": time.time(), "state": state})
            output.write_text(json.dumps(receipt, indent=2) + "\n")
            if state != previous:
                print(f"{target} ({args.model}): {state}", flush=True)
                previous = state
            if state == "Deployed":
                receipt["completed"] = time.time()
                receipt["machine"] = machine
                return
            if state.startswith("Failed"):
                raise RuntimeError(f"Deployment entered {state}")
            time.sleep(10)
        raise TimeoutError("MAAS deployment did not finish")
    finally:
        output.write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    main()
