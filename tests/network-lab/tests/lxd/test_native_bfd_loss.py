"""Drop only one owned core BFD session's packets and verify real forwarding."""

import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from maas_bird.runtime import write_atomic

PROJECT = "maas-bird-network-lab-tf"
TORS = ("r1a", "r1b", "r2a", "r2b")
ROOT = Path(__file__).resolve().parents[2]


def execute(node, *args):
    return subprocess.check_output(
        ["lxc", "exec", "--project", PROJECT, node, "--", *args],
        text=True,
        timeout=30,
    )


def health(node, selected, disabled):
    deadline = time.monotonic() + 20
    destination = "10.205.250.13" if node.startswith("r1") else "10.205.250.11"
    while True:
        try:
            summary = json.loads(execute(node, "vtysh", "-c", "show bgp vrf all summary json"))
            sessions = json.loads(execute(node, "vtysh", "-c", "show bfd peers json"))
            for vrf, suffix in (("default", ""), ("access", ".3420"), ("replication", ".3430")):
                for peer in ("spine0", "spine1"):
                    interface = peer + suffix
                    failed = disabled and vrf == "default" and peer == selected
                    established = (
                        summary[vrf]["ipv4Unicast"]["peers"][interface]["state"] == "Established"
                    )
                    assert established != failed, (vrf, interface)
                    bfd = next(
                        row
                        for row in sessions
                        if row.get("interface") == interface and row["vrf"] == vrf
                    )
                    assert bfd["status"] in ({"down", "init"} if failed else {"up"}), bfd
            link = json.loads(execute(node, "ip", "-j", "link", "show", selected))[0]
            assert "UP" in link["flags"] and "LOWER_UP" in link["flags"]
            routes = json.loads(execute(node, "ip", "-j", "route", "show", f"{destination}/32"))
            assert len(routes) == 1 and routes[0]["protocol"] == "bgp"
            hops = routes[0].get("nexthops", [routes[0]])
            expected = {"spine0", "spine1"} - ({selected} if disabled else set())
            assert {hop["dev"] for hop in hops} == expected
            assert all(hop["via"]["family"] == "inet6" for hop in hops)
            return {"bgp": summary, "bfd": sessions, "route": routes, "link": link}
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


@pytest.mark.parametrize("node", TORS)
@pytest.mark.parametrize("peer", ("spine0", "spine1"))
def test_native_bfd_packet_loss_with_physical_link_up(node, peer):
    instance = json.loads(
        subprocess.check_output(
            ["lxc", "query", f"/1.0/instances/{node}?project={PROJECT}"],
            text=True,
            timeout=30,
        )
    )
    assert instance["type"] == "container" and instance["status"] == "Running"
    assert instance["config"]["user.maas-bird-owner"] == "network-lab"
    assert instance["config"]["limits.cpu"] == "1"
    assert instance["config"]["limits.memory"] == "768MiB"
    assert instance["expanded_config"].get("security.privileged", "false") == "false"
    before = health(node, peer, False)
    session = next(
        row for row in before["bfd"] if row.get("interface") == peer and row["vrf"] == "default"
    )
    original = execute(node, "ip6tables", "-w", "5", "-S", "OUTPUT")
    marker = "nlab-bfd-" + uuid.uuid4().hex[:12]
    rule = [
        "-o",
        peer,
        "-d",
        session["peer"],
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
    destination = "10.205.250.13" if node.startswith("r1") else "10.205.250.11"
    source = f"10.205.250.{11 + TORS.index(node)}"
    receipt = {
        "node": node,
        "peer": peer,
        "marker": marker,
        "rule": rule,
        "before": before,
        "original_output_rules": original,
    }
    process = None
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                [
                    "lxc",
                    "exec",
                    "--project",
                    PROJECT,
                    node,
                    "--",
                    "stdbuf",
                    "-oL",
                    "ping",
                    "-n",
                    "-D",
                    "-O",
                    "-i",
                    "0.1",
                    "-w",
                    "30",
                    "-W",
                    "1",
                    "-M",
                    "do",
                    "-s",
                    "1472",
                    "-I",
                    source,
                    destination,
                ],
                text=True,
                stdout=output,
                stderr=errors,
            )
            ready = time.monotonic() + 10
            while True:
                text = os.pread(output.fileno(), 1_000_000, 0).decode()
                sequences = re.findall(r"bytes from .*: icmp_seq=(\d+)", text)
                if len(sequences) >= 3:
                    break
                assert process.poll() is None and time.monotonic() < ready, text
                time.sleep(0.1)
            receipt["baseline_last_sequence"] = int(sequences[-1])
            receipt["fault_started"] = time.monotonic()
            execute(node, "ip6tables", "-w", "5", "-I", "OUTPUT", "1", *rule)
            receipt["surviving"] = health(node, peer, True)
            receipt["forwarding_state_observed"] = time.monotonic()
            counters = execute(node, "ip6tables", "-w", "5", "-L", "OUTPUT", "-n", "-v", "-x")
            matching = [line for line in counters.splitlines() if marker in line]
            assert len(matching) == 1 and int(matching[0].split()[0]) > 0
            receipt["drop_counter"] = matching[0]
            receipt["link_local_packets"] = execute(
                node,
                "ping",
                "-6",
                "-n",
                "-I",
                peer,
                "-c",
                "2",
                "-i",
                "0.2",
                "-W",
                "1",
                session["peer"],
            )
            assert "2 packets transmitted, 2 received" in receipt["link_local_packets"]
            stable = execute(
                node,
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
            assert "3 packets transmitted, 3 received" in stable
            receipt["stable_packets"] = stable
            current = os.pread(output.fileno(), 1_000_000, 0).decode()
            cutoff = int(re.findall(r"bytes from .*: icmp_seq=(\d+)", current)[-1])
            process.communicate(timeout=40)
            trace = os.pread(output.fileno(), 1_000_000, 0).decode()
            receipt["trace"] = trace
            receipt["stderr"] = os.pread(errors.fileno(), 1_000_000, 0).decode()
            rows = [
                (float(stamp), int(sequence))
                for stamp, sequence in re.findall(
                    r"(?m)^\[(\d+\.\d+)\].* bytes from .*: icmp_seq=(\d+)", trace
                )
            ]
            assert process.returncode == 0 and len(rows) >= 20
            assert rows[0][1] <= receipt["baseline_last_sequence"]
            assert rows[-1][0] - rows[0][0] >= 25
            assert sum(sequence > cutoff for _, sequence in rows) >= 5
            counts = re.search(r"(\d+) packets transmitted, (\d+) received", trace)
            assert counts is not None
            receipt["transmitted"], receipt["received"] = int(counts[1]), int(counts[2])
            receipt["max_inter_reply_gap_seconds"] = max(
                b[0] - a[0] for a, b in zip(rows, rows[1:], strict=False)
            )
            assert len({sequence for _, sequence in rows}) == len(rows)
            assert all(b[1] > a[1] for a, b in zip(rows, rows[1:], strict=False))
            receipt["held_fault_state"] = health(node, peer, True)
            receipt["fault_assertions_passed"] = True
        except BaseException as error:
            receipt["failure"] = {"type": type(error).__name__, "message": str(error)}
            receipt["failure_bgp"] = json.loads(
                execute(node, "vtysh", "-c", "show bgp vrf all summary json")
            )
            receipt["failure_bfd"] = json.loads(
                execute(node, "vtysh", "-c", "show bfd peers json")
            )
            receipt["failure_rules"] = execute(
                node, "ip6tables", "-w", "5", "-L", "OUTPUT", "-n", "-v", "-x"
            )
            raise
        finally:
            try:
                present = subprocess.run(
                    [
                        "lxc",
                        "exec",
                        "--project",
                        PROJECT,
                        node,
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
                    execute(node, "ip6tables", "-w", "5", "-D", "OUTPUT", *rule)
                assert execute(node, "ip6tables", "-w", "5", "-S", "OUTPUT") == original
                receipt["restored"] = health(node, peer, False)
            finally:
                if process is not None and process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=10)
                directory = ROOT / "runtime/terraform/acceptance/bfd-loss"
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                write_atomic(
                    directory / f"{node}-{peer}-{time.time_ns()}.json",
                    json.dumps(receipt, indent=2) + "\n",
                    0o600,
                )
