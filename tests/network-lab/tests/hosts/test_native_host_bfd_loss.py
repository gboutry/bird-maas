"""Host-edge BFD-only loss with physical and IPv6 neighbor forwarding retained."""

import json
import re
import subprocess
import time
import uuid
from ipaddress import ip_network
from typing import Any

import pytest
from maas_bird.runtime import write_atomic
from test_native_workers import PROJECT, ROOT, WORKERS, guest
from test_native_workers import ownership as ownership


@pytest.mark.parametrize("name", ("probe", "probe-r2a"))
@pytest.mark.parametrize("pf", (0, 1))
def test_native_host_bfd_loss_withholds_failed_general_path(name, pf, ownership):
    workers = json.loads(
        (ROOT / "runtime/terraform/lab/40-networking/terraform.tfstate").read_text()
    )["outputs"]["workers"]["value"]
    assert workers[name]["system_uuid"] == ownership["config"]["volatile.uuid"]
    remote = next(other for other in WORKERS if workers[other]["rack"] != workers[name]["rack"])
    interface = workers[name]["interfaces"][f"pf{pf}"]["name"] + ".3404"
    survivor = workers[name]["interfaces"][f"pf{1 - pf}"]["name"] + ".3404"
    roles = json.loads((ROOT / "maas/network-model/logical-roles.json").read_text())["roles"]
    role = next(role for role in roles if role["id"] == "general")
    network = ip_network(role["address_block"])
    source = str(network.network_address + 20 + WORKERS.index(name))
    destination = str(network.network_address + 20 + WORKERS.index(remote))
    marker = "nlab-edge-bfd-" + uuid.uuid4().hex[:12]
    rule = [
        "-o",
        interface,
        "-d",
        "fe80::1",
        "-p",
        "udp",
        "--dport",
        "3784",
        "-m",
        "comment",
        "--comment",
        marker,
        "-j",
        "DROP",
    ]
    original = guest(name, "ip6tables", "-w", "5", "-S", "OUTPUT")
    receipt: dict[str, Any] = {
        "worker": name,
        "interface": interface,
        "survivor": survivor,
        "rule": rule,
        "samples": [],
    }

    def snapshot():
        protocols = guest(name, "birdc", "show", "protocols", "all")
        blocks = re.split(r"(?m)^(?=\w+\s+(?:BGP|Pipe|Kernel|Direct|Device|Static)\s)", protocols)
        selected = [
            block
            for block in blocks
            if re.match(r"\w+\s+BGP\s", block)
            and re.search(rf"Neighbor address:\s+fe80::1%{re.escape(interface)}\s", block)
        ]
        assert len(selected) == 1, protocols
        bfd = guest(name, "birdc", "show", "bfd", "sessions")
        rows = [row.split() for row in bfd.splitlines() if row.strip().startswith("fe80:")]
        failed = [row for row in rows if row[1] == interface]
        assert len(failed) == 1, bfd
        routes = json.loads(
            guest(
                name, "ip", "-j", "route", "show", "table", str(role["table"]), f"{destination}/32"
            )
        )
        assert len(routes) == 1 and routes[0]["protocol"] == "bird", routes
        hops = routes[0].get("nexthops", [routes[0]])
        link = json.loads(guest(name, "ip", "-j", "link", "show", interface))[0]
        return {
            "at": time.monotonic(),
            "bgp": selected[0],
            "all_bgp_established": protocols.count("BGP state:          Established"),
            "bfd": bfd,
            "failed_bfd_state": failed[0][2],
            "other_bfd_up": sum(row[2] == "Up" for row in rows if row[1] != interface),
            "route": routes,
            "hops": hops,
            "link": link,
        }

    def state_matches(state, disabled):
        assert {"UP", "LOWER_UP"} <= set(state["link"]["flags"])
        assert state["other_bfd_up"] == 9, state["bfd"]
        assert state["all_bgp_established"] == (9 if disabled else 10)
        assert ("BGP state:          Established" in state["bgp"]) == (not disabled)
        assert state["failed_bfd_state"] in ({"Down", "Init"} if disabled else {"Up"})
        expected = {survivor} if disabled else {interface, survivor}
        assert len(state["hops"]) == len(expected)
        assert {hop["dev"] for hop in state["hops"]} == expected, state["route"]
        assert all(hop["via"] == {"family": "inet6", "host": "fe80::1"} for hop in state["hops"])

    def await_state(disabled):
        started = time.monotonic()
        while True:
            observed = snapshot()
            receipt["samples"].append(observed)
            try:
                state_matches(observed, disabled)
                return observed
            except AssertionError:
                if time.monotonic() - started >= 20:
                    raise
                time.sleep(0.2)

    def packets():
        output = guest(
            name,
            "ping",
            "-n",
            "-c",
            "3",
            "-i",
            "0.2",
            "-W",
            "1",
            "-M",
            "do",
            "-s",
            "1472",
            "-I",
            source,
            destination,
        )
        assert "3 packets transmitted, 3 received" in output
        overlay = guest(
            name,
            "ping",
            "-n",
            "-c",
            "3",
            "-i",
            "0.2",
            "-W",
            "1",
            "-M",
            "do",
            "-s",
            "1372",
            "-I",
            "tenant0",
            f"172.30.10.{20 + WORKERS.index(remote)}",
        )
        assert "3 packets transmitted, 3 received" in overlay
        return {"routed": output, "overlay": overlay}

    receipt["before"] = await_state(False)
    receipt["before_packets"] = packets()
    try:
        receipt["fault_started"] = time.monotonic()
        guest(name, "ip6tables", "-w", "5", "-I", "OUTPUT", "1", *rule)
        receipt["initial_withdrawal"] = await_state(True)
        counters = guest(name, "ip6tables", "-w", "5", "-L", "OUTPUT", "-n", "-v", "-x")
        matches = [line for line in counters.splitlines() if marker in line]
        assert len(matches) == 1 and int(matches[0].split()[0]) > 0, counters
        receipt["drop_counter"] = matches[0]
        receipt["neighbor_packets"] = guest(
            name, "ping", "-6", "-n", "-c", "3", "-i", "0.2", "-W", "1", "-I", interface, "fe80::1"
        )
        assert "3 packets transmitted, 3 received" in receipt["neighbor_packets"]
        receipt["held_packets"] = packets()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            observed = snapshot()
            receipt["samples"].append(observed)
            state_matches(observed, True)
            time.sleep(0.5)
        receipt["held_fault_verified"] = True
    except BaseException as error:
        receipt["failure"] = {"type": type(error).__name__, "message": str(error)}
        receipt["failure_counters"] = guest(
            name, "ip6tables", "-w", "5", "-L", "OUTPUT", "-n", "-v", "-x"
        )
        neighbor = subprocess.run(
            [
                "lxc",
                "exec",
                "--project",
                PROJECT,
                name,
                "--",
                "ping",
                "-6",
                "-n",
                "-c",
                "3",
                "-i",
                "0.2",
                "-W",
                "1",
                "-I",
                interface,
                "fe80::1",
            ],
            text=True,
            capture_output=True,
            timeout=10,
        )
        receipt["failure_neighbor_packets"] = {
            "returncode": neighbor.returncode,
            "stdout": neighbor.stdout,
            "stderr": neighbor.stderr,
        }
        raise
    finally:
        try:
            present = subprocess.run(
                [
                    "lxc",
                    "exec",
                    "--project",
                    PROJECT,
                    name,
                    "--",
                    "ip6tables",
                    "-w",
                    "5",
                    "-C",
                    "OUTPUT",
                    *rule,
                ],
                text=True,
                capture_output=True,
                timeout=30,
            )
            assert present.returncode in (0, 1), present.stderr
            if present.returncode == 0:
                guest(name, "ip6tables", "-w", "5", "-D", "OUTPUT", *rule)
            assert guest(name, "ip6tables", "-w", "5", "-S", "OUTPUT") == original
            receipt["restored"] = await_state(False)
            receipt["restored_packets"] = packets()
        finally:
            directory = ROOT / "runtime/terraform/acceptance/host-bfd-loss"
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_atomic(
                directory / f"{name}-pf{pf}-{time.time_ns()}.json",
                json.dumps(receipt, indent=2) + "\n",
                0o600,
            )
