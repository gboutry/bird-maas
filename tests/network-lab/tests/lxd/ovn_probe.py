"""Bounded raw-Ethernet test process; installs no service or network policy."""

import json
import socket
import struct
import sys
import time


def main():
    mode, interface, value = sys.argv[1:]
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)) as descriptor:
        descriptor.bind((interface, 0))
        if mode == "send":
            frame = bytes.fromhex(value)
            for _ in range(3):
                assert descriptor.send(frame) == len(frame)
                time.sleep(0.05)
            return
        assert mode == "receive"
        descriptor.setsockopt(263, 8, 1)  # SOL_PACKET, PACKET_AUXDATA
        token = bytes.fromhex(value)
        descriptor.settimeout(0.1)
        print(json.dumps({"ready": True}), flush=True)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                packet, ancillary, _, address = descriptor.recvmsg(4096, 1024)
                if address[2] != socket.PACKET_OUTGOING and token in packet:
                    vlan = (
                        int.from_bytes(packet[14:16], "big") & 4095
                        if packet[12:14] == b"\x81\x00"
                        else None
                    )
                    for level, kind, data in ancillary:
                        if level == 263 and kind == 8 and len(data) >= 20:
                            status, _, _, _, _, tci, _ = struct.unpack("=IIIHHHH", data[:20])
                            if status & 16:
                                vlan = tci & 4095
                    print(
                        json.dumps({"received": True, "frame": packet.hex(), "vlan": vlan}),
                        flush=True,
                    )
                    return
            except TimeoutError:
                pass
        print(json.dumps({"received": False}), flush=True)


if __name__ == "__main__":
    main()
