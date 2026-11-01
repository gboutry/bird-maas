"""Transactional ownership of the machine's system BIRD service.

The operator owns one daemon. Existing host configuration stays untouched; only
systemd's local unit/overrides are saved and replaced. An interrupted first
activation retains its journal so a later hook can recover it.
"""

import base64
import hashlib
import json
import os
from pathlib import Path

from maas_bird.runtime import RuntimeErrorBase, run, write_atomic


class SystemBird:
    def __init__(self, root: Path, directory: Path):
        self.root = root
        self.directory = directory
        self.unit = root / "etc/systemd/system/bird.service"
        self.journal = directory / "system-service.json"
        self.reload_pending = directory / "systemd-reload-pending"

    def pending(self):
        return self.journal.exists() and not self._read()["committed"]

    def _read(self):
        if (
            self.journal.is_symlink()
            or self.journal.stat().st_mode & 0o077
            or self.journal.stat().st_uid != os.geteuid()
        ):
            raise RuntimeErrorBase("System BIRD takeover journal must be private")
        record = json.loads(self.journal.read_text())
        if record["owner"] != (self.directory / "owner").read_text().strip():
            raise RuntimeErrorBase("System BIRD takeover belongs to another unit")
        for saved in record["files"]:
            self._saved_path(saved["path"])
        return record

    def _saved_path(self, name):
        relative = Path(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not (
                relative == Path("etc/systemd/system/bird.service")
                or relative.parent == Path("etc/systemd/system/bird.service.d")
            )
        ):
            raise RuntimeErrorBase("Invalid BIRD takeover backup path")
        return self.root / relative

    def _paths(self):
        """Only persistent local overrides are adopted, never transient units."""
        for name in ("bird.service", "bird.service.d"):
            path = self.root / "run/systemd/system" / name
            if path.exists() or path.is_symlink():
                raise RuntimeErrorBase("Remove transient BIRD service overrides before takeover")
        for prefix in ("usr/lib", "lib"):
            vendor = self.root / prefix / "systemd/system/bird.service.d"
            if vendor.exists() and any(vendor.iterdir()):
                raise RuntimeErrorBase("Review vendor BIRD drop-ins before takeover")
        dropins = self.unit.with_name("bird.service.d")
        if dropins.is_symlink():
            raise RuntimeErrorBase("BIRD override directory must not be a symlink")
        paths = [self.unit]
        if dropins.exists():
            paths.extend(sorted(dropins.iterdir()))
        return paths

    def install(self, contents):
        if not self.journal.exists():
            files = []
            for path in self._paths():
                if path.is_symlink():
                    files.append(
                        {"path": str(path.relative_to(self.root)), "link": os.readlink(path)}
                    )
                elif path.exists():
                    if not path.is_file():
                        raise RuntimeErrorBase("Unknown BIRD systemd override object")
                    files.append(
                        {
                            "path": str(path.relative_to(self.root)),
                            "data": base64.b64encode(path.read_bytes()).decode(),
                            "mode": path.stat().st_mode & 0o777,
                        }
                    )
            enabled = run(["systemctl", "is-enabled", "bird.service"], check=False)
            active = run(["systemctl", "is-active", "bird.service"], check=False)
            enabled_state = enabled.stdout.strip()
            if enabled.returncode == 4 and not enabled_state:
                enabled_state = "not-found"
            if enabled.returncode not in {0, 1, 3, 4} or enabled_state not in {
                "enabled",
                "disabled",
                "masked",
                "static",
                "not-found",
            }:
                raise RuntimeErrorBase("Cannot establish original BIRD enablement")
            if active.returncode not in {0, 3, 4} or active.stdout.strip() not in {
                "active",
                "inactive",
                "failed",
                "unknown",
            }:
                raise RuntimeErrorBase("Cannot establish original BIRD activity")
            record = {
                "owner": (self.directory / "owner").read_text().strip(),
                "committed": False,
                "enabled": enabled_state,
                "active": active.stdout.strip() == "active",
                "files": files,
            }
            write_atomic(self.journal, json.dumps(record), 0o600)
        record = self._read()
        if not record["committed"]:
            # The snapshot is durable before stopping the old daemon. Repeating
            # this after an interrupted hook cannot overwrite the original state.
            if record["enabled"] != "not-found":
                run(["systemctl", "stop", "bird.service"])
            for path in self._paths():
                path.unlink(missing_ok=True)
        if not self.unit.exists() or self.unit.read_text() != contents:
            self.unit.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(self.reload_pending, "pending\n", 0o600)
            write_atomic(self.unit, contents, 0o644)
        if self.reload_pending.exists():
            run(["systemctl", "daemon-reload"])
            self.reload_pending.unlink()

    def commit(self):
        record = self._read()
        if not record["committed"]:
            record["committed"] = True
            write_atomic(self.journal, json.dumps(record), 0o600)

    def rollback(self):
        record = self._read()
        # A lost unit must never resurrect unvalidated vendor advertisements.
        # Only failure of the FIRST managed startup may restore previous routing.
        if record["committed"]:
            raise RuntimeErrorBase("Cannot roll back completed system BIRD takeover")
        run(["systemctl", "disable", "--now", "bird.service"])
        for path in self._paths():
            path.unlink(missing_ok=True)
        for saved in record["files"]:
            path = self._saved_path(saved["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            if "link" in saved:
                path.symlink_to(saved["link"])
            else:
                write_atomic(path, base64.b64decode(saved["data"]).decode(), saved["mode"])
        run(["systemctl", "daemon-reload"])
        if record["enabled"] == "enabled":
            run(["systemctl", "enable", "bird.service"])
        if record["active"]:
            run(["systemctl", "start", "bird.service"])
        self.journal.unlink()
        self.reload_pending.unlink(missing_ok=True)

    def decommission(self):
        """Withdraw service authority permanently, retaining the original backup."""
        self._read()
        snapshot = self.journal.read_text()
        backups = self.root / "var/backups/maas-bird"
        backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        archive = backups / (hashlib.sha256(snapshot.encode()).hexdigest() + ".json")
        write_atomic(archive, snapshot, 0o600)
        run(["systemctl", "disable", "--now", "bird.service"])
        temporary = self.unit.with_name("bird.service.mask-tmp")
        if not temporary.is_symlink():
            temporary.symlink_to("/dev/null")
        elif os.readlink(temporary) != "/dev/null":
            raise RuntimeErrorBase("Unknown BIRD decommission mask")
        temporary.replace(self.unit)
        run(["systemctl", "daemon-reload"])
        self.journal.unlink()
        self.reload_pending.unlink(missing_ok=True)
