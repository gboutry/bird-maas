"""Native OVN forwarding through Terraform-created temporary instance NICs."""

import json
import subprocess
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "maas-bird-network-lab"
pytestmark = pytest.mark.integration
MACS = {
    "fabric": "02:00:00:fa:fe:01",
    "probe": "02:00:00:fa:fe:02",
    "probe-alt": "02:00:00:fa:fe:03",
}
NS = "nlab-ovn-probe"


def cli(*args):
    return subprocess.check_output(args, text=True, timeout=15)


def guest(name, *args):
    return cli("lxc", "exec", "--project", PROJECT, name, "--", *args)


@pytest.fixture(scope="module")
def endpoints():
    selected = {}
    moved = []
    created = []
    try:
        for name, mac in MACS.items():
            instance = json.loads(cli("lxc", "query", f"/1.0/instances/{name}?project={PROJECT}"))
            assert instance["config"]["user.maas-bird-owner"] == "network-lab"
            assert instance["config"]["limits.cpu"] == "2"
            assert instance["config"]["limits.memory"] == ("8GiB" if name == "fabric" else "4GiB")
            assert NS not in {
                n["name"] for n in json.loads(guest(name, "ip", "-j", "netns", "list"))
            }
            links = json.loads(guest(name, "ip", "-j", "link", "show"))
            matches = [link for link in links if link.get("address") == mac]
            assert len(matches) == 1 and "master" not in matches[0]
            interface = matches[0]["ifname"]
            selected[name] = interface
            guest(name, "ip", "netns", "add", NS)
            created.append(name)
            guest(name, "ip", "link", "set", interface, "netns", NS)
            moved.append(name)
            guest(name, "ip", "-n", NS, "link", "set", interface, "up")
        yield selected
    finally:
        for name in reversed(moved):
            guest(name, "ip", "-n", NS, "link", "set", selected[name], "down")
            guest(name, "ip", "-n", NS, "link", "set", selected[name], "address", MACS[name])
            guest(name, "ip", "-n", NS, "link", "set", selected[name], "netns", "1")
        for name in reversed(created):
            guest(name, "ip", "netns", "delete", NS)


def exchange(
    endpoints,
    source,
    destination,
    *,
    source_mac=None,
    destination_mac=None,
    lldp=False,
    vlan=False,
):
    token = uuid.uuid4().bytes
    src = bytes.fromhex((source_mac or MACS[source]).replace(":", ""))
    dst = bytes.fromhex((destination_mac or MACS[destination]).replace(":", ""))
    if lldp:
        body = b"\x02\x07\x04" + src + b"\x04\x02\x07p\x06\x02\x00\x78"
        body += (5 * 512 + len(token)).to_bytes(2, "big") + token + b"\x00\x00"
        frame = dst + src + b"\x88\xcc" + body
    else:
        frame = dst + src + (b"\x81\x00\x0d\x4c" if vlan else b"") + b"\x88\xb5" + token
    program = (Path(__file__).with_name("ovn_probe.py")).read_text()

    def argv(name, mode, value):
        return [
            "lxc",
            "exec",
            "--project",
            PROJECT,
            name,
            "--",
            "ip",
            "netns",
            "exec",
            NS,
            "python3",
            "-u",
            "-c",
            program,
            mode,
            endpoints[name],
            value,
        ]

    with subprocess.Popen(
        argv(destination, "receive", token.hex()), stdout=subprocess.PIPE, text=True
    ) as receiver:
        assert receiver.stdout is not None
        assert json.loads(receiver.stdout.readline()) == {"ready": True}
        cli(*argv(source, "send", frame.hex()))
        output, _ = receiver.communicate(timeout=10)
        assert receiver.returncode == 0
    result = json.loads(output)
    if result["received"]:
        assert bytes.fromhex(result["frame"])[:12] == dst + src
        if vlan:
            assert result["vlan"] == 3404
    return result["received"]


@pytest.mark.parametrize(
    "source,destination",
    (
        ("fabric", "probe-alt"),
        ("probe-alt", "fabric"),
        ("probe", "probe-alt"),
        ("probe-alt", "probe"),
    ),
)
def test_configured_mac_positive_control(endpoints, source, destination):
    assert exchange(endpoints, source, destination)


def test_broadcast(endpoints):
    assert exchange(endpoints, "fabric", "probe-alt", destination_mac="ff:ff:ff:ff:ff:ff")


def test_lldp(endpoints):
    assert exchange(
        endpoints, "fabric", "probe-alt", destination_mac="01:80:c2:00:00:0e", lldp=True
    )


def test_vlan_trunk(endpoints):
    assert exchange(endpoints, "fabric", "probe-alt", vlan=True)


def test_downstream_mac_source(endpoints):
    assert exchange(endpoints, "fabric", "probe-alt", source_mac="02:00:00:fa:fe:99")


def test_downstream_mac_return(endpoints):
    assert exchange(endpoints, "probe-alt", "fabric", destination_mac="02:00:00:fa:fe:99")


def test_primary_mac_move_has_return_delivery(endpoints):
    guest("fabric", "ip", "-n", NS, "link", "set", endpoints["fabric"], "down")
    guest("probe", "ip", "-n", NS, "link", "set", endpoints["probe"], "address", MACS["fabric"])
    try:
        assert exchange(endpoints, "probe", "probe-alt", source_mac=MACS["fabric"])
        assert exchange(endpoints, "probe-alt", "probe", destination_mac=MACS["fabric"])
    finally:
        guest("probe", "ip", "-n", NS, "link", "set", endpoints["probe"], "address", MACS["probe"])
        guest("fabric", "ip", "-n", NS, "link", "set", endpoints["fabric"], "up")


def test_positive_control_after_mac_restore(endpoints):
    assert exchange(endpoints, "fabric", "probe-alt")
    assert exchange(endpoints, "probe-alt", "fabric")
