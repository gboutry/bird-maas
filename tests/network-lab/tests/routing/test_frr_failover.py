"""Exercise surviving forwarding and kernel ECMP restoration after spine loss."""

import json
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TORS = {"r1a": 11, "r1b": 12, "r2a": 13, "r2b": 14}
pytestmark = pytest.mark.integration


def paths(router):
    return {
        name: json.loads(router(name, "ip", "-j", "route", "show", "protocol", "bgp"))
        for name in TORS
    }


def paths_match(snapshot, devices, host_manifest=None):
    for name, routes in snapshot.items():
        installed = {route["dst"].removesuffix("/32"): route for route in routes}
        for other, last in TORS.items():
            if other == name:
                continue
            route = installed.get(f"10.205.250.{last}")
            if route is None:
                return False
            if {hop["dev"] for hop in route.get("nexthops", [route])} != devices:
                return False
        if host_manifest:
            for address, rack in host_manifest["origin_racks"].items():
                if rack == name[:2]:
                    continue
                route = installed.get(address)
                if (
                    route is None
                    or {hop["dev"] for hop in route.get("nexthops", [route])} != devices
                ):
                    return False
    return True


def wait_paths(router, devices, timeout=45, host_manifest=None):
    deadline = time.monotonic() + timeout
    while True:
        snapshot = paths(router)
        if paths_match(snapshot, devices, host_manifest):
            return snapshot
        assert time.monotonic() < deadline, snapshot
        time.sleep(0.5)


def assert_protocols(router, host_manifest=None):
    for name in ("spine0", "spine1", *TORS):
        peers = json.loads(router(name, "vtysh", "-c", "show bgp summary json"))["ipv4Unicast"][
            "peers"
        ]
        bfd = json.loads(router(name, "vtysh", "-c", "show bfd peers json"))
        expected = (
            len(host_manifest["peers"][name])
            if host_manifest
            else (4 if name.startswith("spine") else 2)
        )
        assert len(peers) == expected
        if host_manifest:
            assert set(peers) == set(host_manifest["peers"][name])
            assert all(
                peers[ip]["remoteAs"] == p["asn"] for ip, p in host_manifest["peers"][name].items()
            )
            assert {p["peer"] for p in bfd} == set(host_manifest["peers"][name])
        assert all(peer["state"] == "Established" for peer in peers.values())
        assert len(bfd) == expected
        assert all(peer["status"] == "up" for peer in bfd)


@pytest.mark.parametrize("spine,surviving", [("spine0", "eth2"), ("spine1", "eth1")])
def test_spine_link_failure(spine, surviving, router, request, host_manifest, host_packets):
    before = wait_paths(router, {"eth1", "eth2"}, host_manifest=host_manifest)
    assert_protocols(router, host_manifest)
    result = {"before": before, "started": time.time(), "pings": []}
    output = ROOT / "runtime" / "frr-underlay" / request.config.getoption("--frr-lab")
    output.mkdir(parents=True, exist_ok=True)
    try:
        try:
            for index in range(1, 5):
                router(spine, "ip", "link", "set", f"eth{index}", "down")
            result["all_links_down_at"] = time.time()
            result["surviving_paths"] = wait_paths(
                router, {surviving}, host_manifest=host_manifest
            )
            result["surviving_paths_observed_at"] = time.time()
            for name, last in TORS.items():
                for other, target in TORS.items():
                    if other == name:
                        continue
                    text = router(
                        name,
                        "ping",
                        "-n",
                        "-I",
                        f"10.205.250.{last}",
                        "-c",
                        "2",
                        "-W",
                        "1",
                        f"10.205.250.{target}",
                    )
                    result["pings"].append({"from": name, "to": other, "stdout": text})
                    assert ", 0% packet loss" in text
            if host_packets is not None:
                result["host_pings"] = host_packets()
        finally:
            errors = []
            for index in range(1, 5):
                try:
                    router(spine, "ip", "link", "set", f"eth{index}", "up")
                except (OSError, subprocess.SubprocessError) as error:
                    errors.append(error)
            result["links_restored_at"] = time.time()
            if errors:
                raise ExceptionGroup("Failed to restore spine links", errors)
        result["recovered_paths"] = wait_paths(
            router, {"eth1", "eth2"}, host_manifest=host_manifest
        )
        result["recovered_paths_observed_at"] = time.time()
        assert_protocols(router, host_manifest)
        if host_packets is not None:
            result["recovered_host_pings"] = host_packets()
    finally:
        (output / f"{spine}-fault.json").write_text(json.dumps(result, indent=2) + "\n")


def test_kernel_ecmp_survives_nexthop_ageing(router, request, host_manifest):
    result = {"started": time.time(), "samples": []}
    output = ROOT / "runtime" / "frr-underlay" / request.config.getoption("--frr-lab")
    output.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 240
    try:
        while True:
            snapshot = {"at": time.time(), "paths": paths(router)}
            result["samples"].append(snapshot)
            assert paths_match(snapshot["paths"], {"eth1", "eth2"}, host_manifest), snapshot
            if time.monotonic() >= deadline:
                break
            time.sleep(5)
        assert_protocols(router, host_manifest)
    finally:
        (output / "ecmp-ageing.json").write_text(json.dumps(result, indent=2) + "\n")
