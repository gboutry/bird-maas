"""Owned BIRD service with native parse checks and confirmed reconfiguration.

The host network owner supplies addresses and interfaces. Opt-in NUMA contexts own
only journaled managed-domain policy rules and guards. Dedicated tables must be unused.
"""

import fcntl
import grp
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from charmlibs import apt

from maas_bird import policy_journal
from maas_bird.models import MultiDomainPlan, Plan
from maas_bird.policy import PolicyRouting
from maas_bird.render import quoted
from maas_bird.service_units import bird_unit, policy_unit


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


def write_atomic(path: Path, contents: str, mode: int, group: int | None = None):
    """Leave either the old complete file or the new one after an interrupted write."""
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        if group is not None:
            os.chown(temporary, 0, group)
        temporary.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


class BirdService:
    def __init__(self, root: Path = Path("/"), owner: str = "development"):
        self.owner = owner
        self.root = root
        self.directory = root / "etc/maas-bird"
        self.config = self.directory / "bird.conf"
        self.marker = self.directory / "owner"
        self.unit = root / "etc/systemd/system/maas-bird.service"
        self.socket = "/run/maas-bird/bird.ctl"
        self.policy_unit = self.unit.with_name("maas-bird-policy.service")

    def _control(self, *args, check=True):
        return run(["birdc", "-s", self.socket, *args], check=check)

    def _prepare(self, table: int | tuple[int, ...]):
        """Claim ownership separately from completing idempotent preparation."""
        tables = (table,) if isinstance(table, int) else table
        record = ",".join(str(value) for value in sorted(tables)) + "\n"
        self._claim(tables)
        registry = self.directory / "kernel-table"
        if registry.exists() and registry.read_text() != record:
            raise RuntimeErrorBase("Changing the owned kernel table requires decommissioning")
        if not registry.exists():
            if self.config.exists() or (self.directory / "policy.json").exists():
                raise RuntimeErrorBase(
                    "Missing configured kernel-table ownership; inspect before recovery"
                )
            write_atomic(registry, record, 0o600)
        if not shutil.which("bird"):
            apt.add_package("bird2", update_cache=True)
            run(["systemctl", "disable", "--now", "bird.service"])
        self._prepare_files()
        # A prior hook may have failed AFTER writing units but BEFORE reload.
        # File equality cannot prove that systemd consumed the completed files.
        run(["systemctl", "daemon-reload"])
        # Enable the early boot restorer, but do not start it before initial policy
        # installation. BIRD's Requires/ExecStartPre validates it on every start.
        run(["systemctl", "enable", "maas-bird-policy.service"])

    def _claim(self, tables):
        if self.marker.exists():
            if self.marker.read_text() != self.owner + "\n":
                raise RuntimeErrorBase("Unknown service ownership marker")
            return
        if self.directory.exists() or self.unit.exists() or self.policy_unit.exists():
            raise RuntimeErrorBase("Refusing to adopt existing BIRD configuration")
        if run(["systemctl", "is-active", "bird.service"], check=False).returncode == 0:
            raise RuntimeErrorBase("Another BIRD service is active")
        routes = json.loads(run(["ip", "-j", "-N", "route", "show", "table", "all"]).stdout)
        if any(str(route.get("table")) in {str(t) for t in tables} for route in routes):
            raise RuntimeErrorBase("Dedicated kernel table is already populated")
        self.directory.mkdir(mode=0o750, parents=True)
        write_atomic(self.marker, self.owner + "\n", 0o600)

    def _prepare_files(self):
        files = [
            (
                self.directory / "policy_journal.py",
                Path(policy_journal.__file__).read_text(),
                0o600,
            ),
            (self.policy_unit, policy_unit(self.owner), 0o644),
            (self.unit, bird_unit(self.owner), 0o644),
        ]
        changed = False
        for path, contents, mode in files:
            if not path.exists() or path.read_text() != contents:
                path.parent.mkdir(parents=True, exist_ok=True)
                write_atomic(path, contents, mode)
                changed = True
        return changed

    @contextmanager
    def _policy_lock(self):
        fd = os.open(self.directory / "context.lock", os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def apply(
        self,
        contents: str,
        table: int | tuple[int, ...],
        session_count: int,
        route_check=None,
        *,
        exports=None,
    ):
        """Accept one generation; a revoked export is never a rollback target.

        The charm supplies ``exports`` from its latest complete relation snapshot.
        Raw parser/control tests may omit authorization metadata.
        """
        self._prepare(table)
        previous = self.config.read_text() if self.config.exists() else None
        if self._restrictive_change(contents, previous, exports):
            try:
                self._discard_config()
            finally:
                self.stop()
            previous = None
        self._record_exports(exports)
        if previous == contents:
            if not self._active():
                self._start()
            if self.established(session_count) and (route_check is None or route_check()):
                return
            raise SessionsPending("Waiting for BGP sessions to establish")
        self._apply_candidate(contents, previous, session_count, route_check)

    def _restrictive_change(self, contents, previous, exports):
        if exports is None or previous is None:
            return False
        path = self.directory / "exports.json"
        if not path.exists():
            # A matching rendered generation proves its exports during migration.
            # Otherwise the old authorization set is unknown: do not roll it back.
            return previous != contents
        try:
            record = json.loads(path.read_text())
            if record["owner"] != self.owner:
                return True
            return bool(set(record["prefixes"]) - {str(prefix) for prefix in exports})
        except (ValueError, KeyError, TypeError):
            return True

    def _record_exports(self, exports):
        if exports is None:
            return
        # Persist the possible live export set BEFORE loading a generation. Since
        # removals first invalidate the previous config, rollback can only restore
        # an equal or smaller set, including across a process interruption.
        record = {"owner": self.owner, "prefixes": sorted(str(prefix) for prefix in exports)}
        write_atomic(self.directory / "exports.json", json.dumps(record), 0o600)

    def _discard_config(self):
        self.config.unlink(missing_ok=True)
        (self.directory / "candidate.conf").unlink(missing_ok=True)
        directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        self._record_exports(())

    def invalidate(self):
        """Make an authoritative revocation durable before attempting process stop."""
        if self.marker.exists() and self.marker.read_text() == self.owner + "\n":
            self._discard_config()

    def _active(self):
        return run(["systemctl", "is-active", "maas-bird.service"], check=False).returncode == 0

    def _start(self):
        run(["systemctl", "reset-failed", "maas-bird.service"])
        run(["systemctl", "enable", "--now", "maas-bird.service"])

    def _write_config(self, path, contents):
        group = grp.getgrnam("bird").gr_gid
        os.chown(self.directory, 0, group)
        write_atomic(path, contents, 0o640, group)

    def _apply_candidate(self, contents, previous, session_count, route_check):
        candidate = self.directory / "candidate.conf"
        symbol = "mb_candidate_" + uuid.uuid4().hex
        self._write_config(candidate, contents + f"\ndefine {symbol} = 1;\n")
        cold = False
        reconfigured = False
        try:
            run(["bird", "-p", "-c", str(candidate)])
            cold = previous is None or not self._active()
            deadline = time.monotonic() + 40
            if cold:
                candidate.replace(self.config)
                self._start()
            else:
                # A lost configure acknowledgement is ambiguous. Stop instead of
                # risking undo of an unrelated older generation on a fresh retry.
                try:
                    self._control("configure", quoted(str(candidate)), "timeout", "60")
                except Exception:
                    self.stop()
                    raise
                reconfigured = True
            if not self._wait_accepted(session_count, route_check, symbol, deadline):
                raise SessionsPending("BGP acceptance failed")
            if not cold:
                reply = self._control("configure", "confirm")
                confirmed = isinstance(reply.stdout, str) and any(
                    line.strip() == "Reconfiguration confirmed"
                    for line in reply.stdout.splitlines()
                )
                if not confirmed or time.monotonic() >= deadline:
                    raise SessionsPending(
                        "BIRD candidate confirmation expired or was not acknowledged"
                    )
            self._write_config(self.config, contents)
        except Exception as exc:
            if cold or reconfigured:
                self._recover(previous, cold)
            if isinstance(exc, SessionsPending):
                outcome = (
                    "routing stopped; no previous configuration retained"
                    if previous is None
                    else "previous configuration retained"
                )
                raise SessionsPending(f"{exc}; {outcome}") from exc
            raise
        finally:
            candidate.unlink(missing_ok=True)

    def _wait_accepted(self, session_count, route_check, symbol, deadline):
        for _ in range(20):
            if time.monotonic() >= deadline:
                return False
            ready = self.established(session_count) and (route_check is None or route_check())
            generation = self._control("show", "symbols", symbol, check=False)
            matches = generation.returncode == 0 and any(
                line.split() and line.split()[0] == symbol
                for line in generation.stdout.splitlines()
            )
            # Re-check time AFTER potentially slow native queries; healthy peers
            # from an automatically restored generation are not acceptance proof.
            if time.monotonic() >= deadline:
                return False
            if ready and matches:
                return True
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        return False

    def _recover(self, previous, cold):
        # Invalidate disk before a possibly failing stop: an initial generation
        # must not survive for systemd to restart after acceptance raised.
        if cold:
            try:
                if previous is None:
                    self._discard_config()
                else:
                    self._write_config(self.config, previous)
            finally:
                self.stop()
            return
        try:
            self._write_config(self.config, previous)
            self._control("configure", "undo")
        except Exception:
            self.stop()
            raise

    def apply_contexts(self, contents: str, plan: Plan | MultiDomainPlan):
        tables = tuple(sorted(c.table for c in plan.contexts))
        self._prepare(tables)
        policy = PolicyRouting(self.directory, self.owner, run)
        new = not policy.path.exists()
        try:
            with self._policy_lock():
                policy.ensure(plan)
        except Exception:
            self.stop()
            raise
        # systemd's pre-start restorer takes the same lock. Never hold it while
        # waiting for systemctl to start the service, or the two processes deadlock.
        try:
            self.apply(
                contents,
                tables,
                len(plan.sessions),
                route_check=lambda: policy.verify_routes(plan),
                exports=plan.prefixes,
            )
        except Exception:
            if new:
                self.stop()
                with self._policy_lock():
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
        with self._policy_lock():
            PolicyRouting(self.directory, self.owner, run).remove()
        run(["systemctl", "disable", "--now", "maas-bird-policy.service"])
        for name in [
            "bird.conf",
            "candidate.conf",
            "kernel-table",
            "owner",
            "context.lock",
            "policy_journal.py",
            "exports.json",
        ]:
            (self.directory / name).unlink(missing_ok=True)
        self.unit.unlink(missing_ok=True)
        self.policy_unit.unlink(missing_ok=True)
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
