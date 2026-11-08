"""Resolve lab units through native Juju placement and owned guest identities."""

import json
from dataclasses import dataclass

import jubilant
from commission import HOSTS, PROJECT, command


@dataclass(frozen=True)
class WorkerBinding:
    unit: str
    machine: str
    uuid: str


def worker_binding(juju: jubilant.Juju, name: str, application: str) -> WorkerBinding:
    instance = json.loads(command("lxc", "query", f"/1.0/instances/{name}?project={PROJECT}"))
    if (
        instance["type"] != "virtual-machine"
        or instance["config"].get("user.maas-bird-owner") != "network-lab"
        or instance["config"]["limits.cpu"] != "2"
        or instance["config"]["limits.memory"] != "4GiB"
    ):
        raise RuntimeError("Expected the owned worker with its 2-CPU / 4-GiB cap")
    status = json.loads(juju.cli("status", "--format=json"))
    units = status["applications"][application].get("units", {})
    if len(units) != 1:
        raise RuntimeError("Expected exactly one unit of the worker application")
    unit, info = next(iter(units.items()))
    machine = info["machine"]
    expected_instance = "manual:" + HOSTS[name]["management_ip"]
    if status["machines"][machine]["instance-id"] != expected_instance:
        raise RuntimeError("Juju placement differs from the worker's management endpoint")
    native = juju.exec("sudo", "cat", "/sys/class/dmi/id/product_uuid", unit=unit).stdout.strip()
    if native != instance["config"]["volatile.uuid"]:
        raise RuntimeError("Native unit UUID differs from the owned LXD worker")
    return WorkerBinding(unit, machine, native)
