"""Native OVSDB CLI acceptance on a private database, without a datapath.

Run explicitly with OVS_BINARY_DIR pointing to a complete installed OVS toolset.
No default OVS socket, kernel bridge, vswitchd or production database is used.
"""

import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from maas_bird import ovsdb


@pytest.fixture
def database():
    tools = Path(os.environ["OVS_BINARY_DIR"])
    schema = Path(os.environ["OVS_SCHEMA"])
    with tempfile.TemporaryDirectory(prefix="maas-bird-ovs-") as directory:
        root = Path(directory)
        db = root / "db"
        socket = root / "db.sock"
        subprocess.run([str(tools / "ovsdb-tool"), "create", str(db), str(schema)], check=True)
        process = subprocess.Popen(
            [
                str(tools / "ovsdb-server"),
                str(db),
                f"--remote=punix:{socket}",
                f"--unixctl={root / 'control.sock'}",
                f"--pidfile={root / 'pid'}",
                "--no-chdir",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5
            while not socket.exists():
                assert process.poll() is None, "Private OVSDB server exited"
                assert time.monotonic() < deadline, "Private OVSDB socket unavailable"
                time.sleep(0.02)

            def execute(args):
                # Even write-only test setup must name this private socket. The
                # production parser's argv is used unchanged after this prefix.
                assert args[0] == "ovs-vsctl"
                return subprocess.run(
                    [
                        str(tools / "ovs-vsctl"),
                        f"--db=unix:{socket}",
                        "--timeout=5",
                        "--no-wait",
                        *args[1:],
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                ).stdout

            execute(["ovs-vsctl", "init"])
            yield execute
        finally:
            process.terminate()
            process.communicate(timeout=5)


def test_actual_ovsdb_snapshot_and_recreated_port_identity(database):
    database(["ovs-vsctl", "add-br", "br-test"])
    create = [
        "ovs-vsctl",
        "add-port",
        "br-test",
        "svi3500",
        "--",
        "set",
        "Interface",
        "svi3500",
        "type=internal",
        "ofport=7",
        "ifindex=31",
        "--",
        "set",
        "Port",
        "svi3500",
        "tag=3500",
    ]
    database(create)
    old = ovsdb.internal_port(ovsdb.snapshot(database), "br-test", "svi3500")
    assert old["vlan"] == 3500 and old["ifindex"] == 31
    database(["ovs-vsctl", "del-port", "br-test", "svi3500"])
    database(create)
    new = ovsdb.internal_port(ovsdb.snapshot(database), "br-test", "svi3500")
    assert old["bridge_uuid"] == new["bridge_uuid"]
    assert old["port_uuid"] != new["port_uuid"]
    assert old["interface_uuid"] != new["interface_uuid"]
    assert old["port"] == new["port"] and old["vlan"] == new["vlan"]
