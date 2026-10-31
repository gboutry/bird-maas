"""Executed native CLI checks: parser and real localhost BGP capability exchange.

No kernel protocol, interface changes, privileged ports or production peers are used.
These checks do not establish unnumbered dataplane/BFD failover performance.
"""

import os
import socket
import subprocess
import time
from pathlib import Path

import pytest
from maas_bird import runtime
from maas_bird.render import quoted, render
from maas_bird.resolver import resolve

BIRD = Path(os.environ["BIRD_BINARY"]).resolve()
BIRDC = BIRD.with_name("birdc")


@pytest.mark.parametrize("tx", [50, 300])
def test_packaged_bird_parser(inputs, tmp_path, tx):
    inputs["config"]["bfd-min-tx-ms"] = tx
    inputs["config"]["bfd-min-rx-ms"] = tx
    config = tmp_path / "bird.conf"
    config.write_text(render(resolve(**inputs), {"path1": "example-a", "path2": "example-b"}))
    subprocess.run([str(BIRD), "-p", "-c", str(config)], check=True, capture_output=True)


def test_real_bgp_exchange_and_withdrawal(tmp_path):
    sockets = []
    for _ in range(2):
        sock = socket.socket(socket.AF_INET6)
        sock.bind(("::1", 0))
        sockets.append(sock)
    ports = [sock.getsockname()[1] for sock in sockets]
    for sock in sockets:
        sock.close()
    processes = []
    try:
        for n in [0, 1]:
            conf = f"""router id 10.0.0.{n + 1};
protocol device {{}}
protocol static reachability {{ ipv6; route 2001:db8::/64 via "lo"; }}
protocol static owned {{ ipv4; route 192.0.2.{n + 1}/32 blackhole; }}
protocol bgp uplink {{
 local ::1 port {ports[n]} as {65001 + n};
 neighbor ::1 port {ports[1 - n]} as {65002 - n};
 multihop;
 connect retry time 1;
 hold time 3;
 keepalive time 1;
 ipv4 {{ extended next hop on; next hop address 2001:db8::{n + 1};
          gateway recursive; import all; export all; }};
}}
"""
            path = tmp_path / f"{n}.conf"
            path.write_text(conf)
            processes.append(
                subprocess.Popen(
                    [
                        str(BIRD),
                        "-f",
                        "-c",
                        str(path),
                        "-s",
                        str(tmp_path / f"{n}.ctl"),
                        "-P",
                        str(tmp_path / f"{n}.pid"),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            )

        def cli(*args):
            return subprocess.run(
                [str(BIRDC), "-s", str(tmp_path / "0.ctl"), *args],
                capture_output=True,
                text=True,
                timeout=5,
            )

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = cli("show", "protocols", "all")
            routes = cli("show", "route", "192.0.2.2/32", "all")
            if "Established" in result.stdout and "[uplink " in routes.stdout:
                break
            time.sleep(0.25)
        else:
            pytest.fail("Localhost BGP did not establish and exchange the test route")
        neighbor = result.stdout.split("Neighbor capabilities", 1)[1].split("Session:", 1)[0]
        assert "Extended next hop" in neighbor
        assert "IPv6 nexthop: ipv4" in neighbor
        processes[1].terminate()
        processes[1].wait(timeout=5)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if "[uplink " not in cli("show", "route", "192.0.2.2/32", "all").stdout:
                break
            time.sleep(0.25)
        else:
            pytest.fail("Peer shutdown did not withdraw its test route")
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)


def test_native_configuration_timeout_restores_previous_generation(tmp_path, monkeypatch):

    first = tmp_path / "first.conf"
    second = tmp_path / "second.conf"
    first.write_text("router id 10.0.0.1; protocol device {}\n")
    second.write_text("router id 10.0.0.2; protocol device {}\n")
    control = tmp_path / "bird.ctl"
    process = subprocess.Popen(
        [str(BIRD), "-f", "-c", str(first), "-s", str(control), "-P", str(tmp_path / "bird.pid")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    native_run = runtime.run

    def run(args, check=True):
        if args[0] == "birdc":
            args = [str(BIRDC), *args[1:]]
        return native_run(args, check=check)

    monkeypatch.setattr(runtime, "run", run)
    service = runtime.BirdService(tmp_path)
    service.socket = str(control)
    try:
        deadline = time.monotonic() + 5
        while not control.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        service._control("configure", quoted(str(second)), "timeout", "1")
        assert "10.0.0.2" in service._control("show", "status").stdout
        time.sleep(2)
        assert "10.0.0.1" in service._control("show", "status").stdout
        service._control("configure", quoted(str(second)), "timeout", "10")
        service._control("configure", "confirm")
        assert "10.0.0.2" in service._control("show", "status").stdout
    finally:
        process.terminate()
        process.communicate(timeout=5)


def test_multiple_domain_parser(domain_inputs, tmp_path):
    plan = resolve(**domain_inputs)
    config = tmp_path / "domains.conf"
    config.write_text(render(plan, {s.id: "example" for s in plan.sessions}))
    subprocess.run([str(BIRD), "-p", "-c", str(config)], check=True, capture_output=True)
