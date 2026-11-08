"""Send or capture nonce-tagged experimental Ethernet broadcasts in owned worker VMs.

The acceptance harness verifies LXD ownership and native identity before
executing this helper inside a worker. It changes no network configuration.
"""

import argparse
import json
import re
import selectors
import socket
import time
from pathlib import Path

ETHERTYPE = bytes.fromhex("88b5")


def interface(value):
    if re.fullmatch(r"enp[78]s0\.340[0-4]", value) is None:
        raise ValueError("Only the ten declared worker role interfaces are allowed")
    return value


def receive(token, interfaces):
    sockets = {}
    selector = selectors.DefaultSelector()
    counts = {name: {} for name in interfaces}
    try:
        for name in interfaces:
            descriptor = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
            descriptor.bind((name, 0))
            descriptor.setblocking(False)
            sockets[name] = descriptor
            selector.register(descriptor, selectors.EVENT_READ, name)
        print(json.dumps({"ready": interfaces}), flush=True)
        # Cover the harness's ten-second startup gate and two ten-second send gates.
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            for key, _ in selector.select(timeout=0.1):
                packet, address = sockets[key.data].recvfrom(2048)
                if address[2] == socket.PACKET_OUTGOING:
                    continue
                offset = 18 if packet[12:14] == bytes.fromhex("8100") else 14
                if packet[offset - 2 : offset] != ETHERTYPE or not packet[offset:].startswith(
                    token
                ):
                    continue
                if packet[:6] != bytes.fromhex("ffffffffffff"):
                    raise RuntimeError("Probe frame is not an Ethernet broadcast")
                event = json.loads(packet[offset + len(token) :])
                counts[key.data].setdefault(event["case"], []).append(event["sequence"])
        print(json.dumps({"received": counts}), flush=True)
    finally:
        selector.close()
        for descriptor in sockets.values():
            descriptor.close()


def send(token, cases):
    sent = []
    for case in cases:
        name = interface(case["interface"])
        mac = bytes.fromhex(
            Path("/sys/class/net", name, "address").read_text().strip().replace(":", "")
        )
        with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)) as descriptor:
            descriptor.bind((name, 0))
            for sequence in range(3):
                body = token + json.dumps({"case": case["id"], "sequence": sequence}).encode()
                packet = bytes.fromhex("ffffffffffff") + mac + ETHERTYPE + body
                size = descriptor.send(packet)
                if size != len(packet):
                    raise RuntimeError("Native packet socket did not send the complete probe")
                sent.append(
                    {"case": case["id"], "sequence": sequence, "interface": name, "bytes": size}
                )
                time.sleep(0.05)
    print(json.dumps({"sent": sent}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("send", "receive"))
    parser.add_argument("--token", required=True)
    parser.add_argument("--configuration", required=True)
    args = parser.parse_args()
    if re.fullmatch("[0-9a-f]{32}", args.token) is None:
        raise ValueError("Probe nonce must contain sixteen random bytes")
    token = b"NLAB" + bytes.fromhex(args.token)
    configuration = json.loads(args.configuration)
    if args.mode == "receive":
        if len(configuration) != 10 or len(set(configuration)) != 10:
            raise ValueError("Capture requires the ten unique role interfaces")
        receive(token, [interface(name) for name in configuration])
    else:
        send(token, configuration)


if __name__ == "__main__":
    main()
