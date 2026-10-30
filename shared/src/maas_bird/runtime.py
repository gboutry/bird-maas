"""Owned BIRD service with native parse checks and confirmed reconfiguration.

The host network owner supplies addresses and interfaces. Opt-in NUMA contexts own
only journaled public policy rules and guards. Dedicated tables must be unused.
"""

import fcntl
import grp
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from maas_bird.models import Plan
from maas_bird.policy import PolicyRouting
from maas_bird.render import quoted


class RuntimeErrorBase(RuntimeError):
    pass


class SessionsPending(RuntimeErrorBase):
    pass


def run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeErrorBase(f"Workload command failed: {args[0]}") from exc
    if check and result.returncode:
        raise RuntimeErrorBase(f"Workload command failed: {args[0]}")
    return result


class BirdService:
    def __init__(self, root: Path = Path("/"), owner: str = "development"):
        self.owner = owner
        self.root = root
        self.directory = root / "etc/maas-bird"
        self.config = self.directory / "bird.conf"
        self.marker = self.directory / "owner"
        self.unit = root / "etc/systemd/system/maas-bird.service"
        self.socket = "/run/maas-bird/bird.ctl"

    def _control(self, *args, check=True):
        return run(["birdc", "-s", self.socket, *args], check=check)

    def _prepare(self, table: int | tuple[int, ...]):
        tables = (table,) if isinstance(table, int) else table
        table_record = ",".join(str(value) for value in sorted(tables))
        if self.marker.exists():
            if self.marker.read_text() != self.owner + "\n":
                raise RuntimeErrorBase("Unknown service ownership marker")
            recorded = self.directory / "kernel-table"
            if recorded.exists() and recorded.read_text().strip() != table_record:
                raise RuntimeErrorBase("Changing the owned kernel table requires decommissioning")
            return
        if self.directory.exists() or self.unit.exists():
            raise RuntimeErrorBase("Refusing to adopt existing BIRD configuration")
        if run(["systemctl", "is-active", "bird.service"], check=False).returncode == 0:
            raise RuntimeErrorBase("Another BIRD service is active")
        routes = json.loads(run(["ip", "-j", "-N", "route", "show", "table", "all"]).stdout)
        if any(str(route.get("table")) in {str(t) for t in tables} for route in routes):
            raise RuntimeErrorBase("Dedicated kernel table is already populated")
        if not shutil.which("bird"):
            from charmlibs import apt

            apt.add_package("bird2", update_cache=True)
            # Only a package installed by this invocation may have its vendor service stopped.
            run(["systemctl", "disable", "--now", "bird.service"])
        self.directory.mkdir(mode=0o750, parents=True)
        self.marker.write_text(self.owner + "\n")
        self.marker.chmod(0o600)
        (self.directory / "kernel-table").write_text(table_record + "\n")
        self.unit.write_text("""[Unit]
Description=MAAS BIRD routing
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=bird
Group=bird
RuntimeDirectory=maas-bird
ExecStart=/usr/sbin/bird -f -c /etc/maas-bird/bird.conf -s /run/maas-bird/bird.ctl
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ProtectKernelTunables=true
ProtectControlGroups=true
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_BIND_SERVICE CAP_NET_RAW
[Install]
WantedBy=multi-user.target
""")
        self.unit.chmod(0o644)
        run(["systemctl", "daemon-reload"])

    def apply(
        self, contents: str, table: int | tuple[int, ...], session_count: int, route_check=None
    ):
        self._prepare(table)
        if self.config.exists() and self.config.read_text() == contents:
            if run(["systemctl", "is-active", "maas-bird.service"], check=False).returncode:
                run(["systemctl", "enable", "--now", "maas-bird.service"])
            if self.established(session_count) and (route_check is None or route_check()):
                return
            raise SessionsPending("Waiting for BGP sessions to establish")
        candidate = self.directory / "candidate.conf"
        candidate.write_text(contents)
        candidate.chmod(0o640)
        gid = grp.getgrnam("bird").gr_gid
        os.chown(self.directory, 0, gid)
        os.chown(candidate, 0, gid)
        try:
            run(["bird", "-p", "-c", str(candidate)])
            initial = not self.config.exists()
            if initial:
                candidate.replace(self.config)
                run(["systemctl", "enable", "--now", "maas-bird.service"])
            else:
                self._control("configure", quoted(str(candidate)), "timeout", "60")
            for _ in range(20):
                if self.established(session_count) and (route_check is None or route_check()):
                    if not initial:
                        self._control("configure", "confirm")
                        candidate.replace(self.config)
                    return
                time.sleep(2)
            if initial:
                self.stop()
                self.config.unlink(missing_ok=True)
            else:
                self._control("configure", "undo")
            raise SessionsPending("BGP acceptance failed; previous configuration retained")
        finally:
            candidate.unlink(missing_ok=True)

    def apply_contexts(self, contents: str, plan: Plan):
        tables = tuple(sorted(c.table for c in plan.contexts))
        self._prepare(tables)
        lock = self.directory / "context.lock"
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            policy = PolicyRouting(self.directory, self.owner, run)
            new = not policy.path.exists()
            policy.ensure(plan)
            try:
                self.apply(
                    contents,
                    tables,
                    len(plan.sessions),
                    route_check=lambda: policy.verify_routes(plan),
                )
            except Exception:
                if new:
                    self.stop()
                    policy.remove()
                raise

    def established(self, count: int) -> bool:
        protocols = self._control("show", "protocols", "all", check=False)
        bfd = self._control("show", "bfd", "sessions", check=False)
        return accepted(protocols.stdout, bfd.stdout, count) and not (
            protocols.returncode or bfd.returncode
        )

    def stop(self):
        if self.marker.exists() and self.marker.read_text() == self.owner + "\n":
            run(["systemctl", "disable", "--now", "maas-bird.service"])

    def remove(self):
        if not self.marker.exists() or self.marker.read_text() != self.owner + "\n":
            return
        self.stop()
        PolicyRouting(self.directory, self.owner, run).remove()
        for name in ["bird.conf", "candidate.conf", "kernel-table", "owner", "context.lock"]:
            (self.directory / name).unlink(missing_ok=True)
        self.unit.unlink(missing_ok=True)
        self.directory.rmdir()
        run(["systemctl", "daemon-reload"])


def accepted(protocols: str, bfd: str, count: int) -> bool:
    """Verify actual BGP, IPv4 extended-next-hop capability and BFD, including on BIRD 2.14."""
    sections = re.split(
        r"(?m)^(?=[A-Za-z_][A-Za-z0-9_]*\s+(?:BGP|Pipe|Kernel|Direct|Device|Static)\s)",
        protocols,
    )
    blocks = [section for section in sections if re.match(r"uplink_[0-9]+\s+BGP\s", section)]
    if len(blocks) != count:
        return False
    for block in blocks:
        if "Established" not in block.splitlines()[0]:
            return False
        if "Neighbor capabilities" not in block:
            return False
        neighbor = block.split("Neighbor capabilities", 1)[1].split("Session:", 1)[0]
        if "Extended next hop" not in neighbor or not re.search(
            r"IPv6 nexthop:\s+ipv4(?:\s|$)", neighbor
        ):
            return False
        if not re.search(r"Channel ipv4\s+State:\s+UP", block):
            return False
    rows = [line.split() for line in bfd.splitlines() if line.strip().startswith("fe80:")]
    return len(rows) == count and all("Up" in row for row in rows)
