"""Run one explicitly supplied command with an expiring stdin lease.

Used by a guarded controller after batch approval, independently on each endpoint.
This helper does not decide Ceph health. EOF, cancellation or a missing heartbeat
terminates only the new process group it owns. It never reconnects or retries.
"""

import json
import math
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


class Inbox:
    """Avoid TextIO read-ahead hiding heartbeats from select()."""

    def __init__(self, fd):
        self.fd = fd
        self.buffer = b""

    def read(self, timeout):
        if b"\n" not in self.buffer:
            if not select.select([self.fd], [], [], timeout)[0]:
                return None
            chunk = os.read(self.fd, 4096)
            if not chunk:
                raise EOFError("lease input closed")
            self.buffer += chunk
        if len(self.buffer) > 16384:
            raise ValueError("Oversize lease message")
        if b"\n" not in self.buffer:
            return None
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)


def emit(value):
    print(json.dumps(value), flush=True)


def exited(process):
    # Keep the group leader unreaped until the last group signal: its PID/PGID
    # cannot be recycled while it is a zombie. poll()/wait() would release it.
    return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def stop(process):
    os.killpg(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + 1
    while not exited(process) and time.monotonic() < deadline:
        time.sleep(0.02)
    # Include descendants even if the group leader exited first.
    os.killpg(process.pid, signal.SIGKILL)
    return process.wait(timeout=2)


def run(request, inbox):
    if Path("/etc/machine-id").read_text().strip() != request["machine_id"]:
        raise ValueError("Machine identity mismatch")
    lease = request["lease_seconds"]
    maximum = request["maximum_seconds"]
    for value, lower, upper in ((lease, 0.1, 8), (maximum, 0.1, 92)):
        if isinstance(value, bool) or not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError("Lease/watchdog outside bounds")
    argv = request["argv"]
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("Expected explicit argv")
    emit(
        {
            "event": "command",
            "argv": argv,
            "time": time.time(),
            "lease_seconds": lease,
            "maximum_seconds": maximum,
        }
    )
    with tempfile.TemporaryFile(mode="w+") as out, tempfile.TemporaryFile(mode="w+") as err:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
            text=True,
        )
        reason = "completed"
        started = time.monotonic()
        expiry = started + lease
        try:
            emit(
                {
                    "event": "started",
                    "pid": process.pid,
                    "pgid": process.pid,
                    "start_ticks": Path(f"/proc/{process.pid}/stat")
                    .read_text()
                    .rsplit(") ", 1)[1]
                    .split()[19],
                    "time": time.time(),
                }
            )
            while not exited(process):
                now = time.monotonic()
                if now >= min(expiry, started + maximum):
                    reason = "lease expired" if now >= expiry else "watchdog expired"
                    break
                message = inbox.read(min(0.05, expiry - now, started + maximum - now))
                if message is None:
                    continue
                if message != {"action": "renew"}:
                    reason = "cancelled" if message == {"action": "cancel"} else "invalid lease"
                    break
                expiry = time.monotonic() + lease
        except (EOFError, ValueError) as error:
            reason = str(error)
        finally:
            # Always clean the group, including on controller disconnect or exception.
            returncode = stop(process)
        out.seek(0)
        err.seek(0)
        return {
            "event": "finished",
            "reason": reason,
            "returncode": returncode,
            "stdout": out.read(),
            "stderr": err.read(),
            "time": time.time(),
        }


def main():
    inbox = Inbox(sys.stdin.fileno())
    request = inbox.read(5)
    if request is None:
        raise ValueError("Missing command request")
    result = run(request, inbox)
    emit(result)
    return 0 if result["reason"] == "completed" and result["returncode"] == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        emit({"error": str(error)})
        sys.exit(1)
