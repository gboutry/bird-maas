import importlib
import json
import subprocess
from pathlib import Path

import jubilant
import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--host-routing",
        action="store_true",
        help="Require the native host peers and routes in runtime/host-fabric/public-plan.json.",
    )
    parser.addoption(
        "--frr-lab",
        choices=("maas-bird-fabric", "maas-bird-fabric-candidate", "maas-bird-fabric-noble"),
        default="maas-bird-fabric",
        help="Select the isolated FRR fabric inside the owned network-device VM.",
    )


@pytest.fixture(scope="module")
def host_manifest(request):
    if not request.config.getoption("--host-routing"):
        return None
    path = Path(__file__).resolve().parents[2] / "runtime/host-fabric/public-plan.json"
    return json.loads(path.read_text())


@pytest.fixture(scope="module")
def host_packets(host_manifest):
    if host_manifest is None:
        return None
    root = Path(__file__).resolve().parents[2]
    hosts = json.loads((root / "maas/network-model/hosts.json").read_text())
    placements = {"probe": "r1a", "probe-alt": "r1b", "probe-r2a": "r2a", "probe-r2b": "r2b"}
    juju = jubilant.Juju(model="network-lab:network-lab")
    with pytest.MonkeyPatch.context() as patch:
        patch.syspath_prepend(str(root / "scripts"))
        workers = importlib.import_module("juju_workers")
        bindings = {
            name: workers.worker_binding(juju, name, "bird-" + suffix)
            for name, suffix in placements.items()
        }
    for name in placements:
        info = json.loads(
            subprocess.check_output(
                ["lxc", "query", f"/1.0/instances/{name}?project=maas-bird-network-lab"], text=True
            )
        )
        assert info["config"]["user.maas-bird-owner"] == "network-lab"
        assert info["config"]["volatile.uuid"] == host_manifest["worker_uuids"][name]
        assert bindings[name].uuid == info["config"]["volatile.uuid"]
        native = juju.exec(
            "sudo", "cat", "/sys/class/dmi/id/product_uuid", unit=bindings[name].unit
        )
        assert native.stdout.strip() == info["config"]["volatile.uuid"]

    def packets():
        results = []
        for name in placements:
            source = hosts[name]["loopback"].split("/")[0]
            for other, target in hosts.items():
                if other == name:
                    continue
                address = target["loopback"].split("/")[0]
                result = juju.exec(
                    "ping",
                    "-n",
                    "-I",
                    source,
                    "-c",
                    "2",
                    "-W",
                    "1",
                    address,
                    unit=bindings[name].unit,
                )
                assert "2 packets transmitted, 2 received, 0% packet loss" in result.stdout
                results.append({"from": name, "to": other, "stdout": result.stdout})
        return results

    return packets


def fabric(*args):
    return subprocess.check_output(
        ["lxc", "exec", "fabric", "--project", "maas-bird-network-lab", "--", *args],
        text=True,
        timeout=60,
    )


@pytest.fixture(scope="module")
def router(request):
    owned_fabric()
    lab = request.config.getoption("--frr-lab")

    def node(name, *args):
        return fabric("docker", "exec", f"clab-{lab}-{name}", *args)

    return node


def owned_fabric():
    info = json.loads(
        subprocess.check_output(
            [
                "lxc",
                "query",
                "/1.0/instances/fabric?project=maas-bird-network-lab",
            ],
            text=True,
            timeout=30,
        )
    )
    assert info["config"]["user.maas-bird-owner"] == "network-lab"
    assert info["config"]["limits.cpu"] == "2"
    assert info["config"]["limits.memory"] == "8GiB"
