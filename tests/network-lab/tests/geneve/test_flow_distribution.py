"""Actual bounded UDP stream distribution and native receive-order counters."""

import importlib
import json
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.integration


def measurement(source, target, paths):
    name = f"native-{source}-{target}-{time.time_ns()}"
    subprocess.run(
        [
            "uv",
            "run",
            "--locked",
            "python",
            str(ROOT / "scripts/measure_geneve.py"),
            "--source",
            source,
            "--target",
            target,
            "--name",
            name,
            "--expect-paths",
            str(paths),
        ],
        check=True,
        text=True,
        timeout=60,
    )
    return json.loads((ROOT / "runtime/geneve-flows" / (name + ".json")).read_text())


def test_l3_hash_pins_one_vtep_pair_despite_two_installed_paths(overlay):
    deployment, _ = overlay
    routes = json.loads(
        deployment.ssh("probe", "ip", "-j", "-4", "-N", "route", "show", "table", "4400")
    )
    learned = {r["dst"].removesuffix("/32"): r for r in routes if str(r.get("protocol")) == "12"}
    expected = importlib.import_module("routing_expectations").general_paths("probe")
    assert set(learned) == set(expected)
    for destination, devices in expected.items():
        hops = learned[destination].get("nexthops", [learned[destination]])
        assert len(hops) == len(devices) and {h["dev"] for h in hops} == devices
    original = deployment.ssh("probe", "cat", "/proc/sys/net/ipv4/fib_multipath_hash_policy")
    assert original == "1"
    try:
        deployment.ssh("probe", "sudo", "sysctl", "-w", "net.ipv4.fib_multipath_hash_policy=0")
        result = measurement("probe", "probe-r2a", 1)
        assert result["hash_policy"] == "0"
    finally:
        deployment.ssh(
            "probe", "sudo", "sysctl", "-w", "net.ipv4.fib_multipath_hash_policy=" + original
        )


@pytest.mark.parametrize(
    ("source", "target"),
    (
        ("probe", "probe-r2a"),
        ("probe-alt", "probe-r2b"),
        ("probe-r2a", "probe"),
        ("probe-r2b", "probe-alt"),
    ),
)
def test_every_worker_distributes_stable_flows_without_observed_reordering(
    overlay, source, target
):
    result = measurement(source, target, 2)
    assert result["hash_policy"] == "1"
    assert result["devices"] == ["enp7s0.3404", "enp8s0.3404"]
    assert result["completed"] > result["started"]
