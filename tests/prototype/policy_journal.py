"""Standard-library-only ownership journal, also installed as the boot restorer.

No Juju, Pydantic or charm-venv imports are allowed here: restoration must work
before the agent or BIRD starts. The plan adapter lives in policy.py.
"""

import argparse
import fcntl
import json
import os
import subprocess
from ipaddress import IPv4Network
from pathlib import Path

PROTOCOL = 196
GUARD_METRIC = 42760


class PolicyError(RuntimeError):
    pass


def rule_network(rule: dict, field: str):
    value = rule.get(field, "all")
    if value == "all":
        return IPv4Network("0.0.0.0/0")
    # iproute2 versions may serialize a prefix as address plus a separate length.
    if "/" not in value and field + "len" in rule:
        value += "/" + str(rule[field + "len"])
    return IPv4Network(value)


def rule_key(rule: dict):
    if set(rule) - {
        "src",
        "srclen",
        "dst",
        "dstlen",
        "table",
        "priority",
        "protocol",
        "flags",
    } or rule.get("flags"):
        return None
    try:
        return (
            str(rule_network(rule, "src")),
            str(rule_network(rule, "dst")),
            int(rule["table"]),
            int(rule["priority"]),
            int(rule.get("protocol", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def guard_key(route: dict):
    """Recognize the whole catch-all identity, never a restricted foreign route."""
    if set(route) - {"type", "dst", "table", "protocol", "metric", "flags", "scope", "tos"}:
        return None
    if route.get("flags") or str(route.get("scope", "global")) not in {"global", "0"}:
        return None
    if (
        str(route.get("type")) not in {"unreachable", "7"}
        or route.get("dst", "default") != "default"
    ):
        return None
    try:
        tos = route.get("tos", 0)
        if (int(tos, 0) if isinstance(tos, str) else int(tos)) != 0:
            return None
        return int(route["table"]), int(route["protocol"]), int(route["metric"])
    except (KeyError, TypeError, ValueError):
        return None


def read_boot_id() -> str:
    value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if not value:
        raise PolicyError("Cannot determine the kernel boot identity")
    return value


class PolicyJournal:
    def __init__(self, directory: Path, owner: str, execute, boot_id=read_boot_id):
        self.directory, self.owner, self.execute = directory, owner, execute
        self.path = directory / "policy.json"
        self.boot_id = boot_id

    def _snapshot(self):
        rules = json.loads(self.execute(["ip", "-j", "-N", "rule", "show"]).stdout)
        routes = json.loads(
            self.execute(["ip", "-4", "-j", "-N", "route", "show", "table", "all"]).stdout
        )
        return rules, routes

    def _save(self, value):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _load(self):
        value = json.loads(self.path.read_text())
        if value.get("owner") != self.owner or value.get("version") != 1:
            raise PolicyError("Unknown policy routing ownership")
        return value

    def _command(self, operation, verb):
        kind, value = operation
        if kind == "guard":
            return [
                "ip",
                "route",
                verb,
                "unreachable",
                "default",
                "table",
                str(value),
                "proto",
                str(PROTOCOL),
                "metric",
                str(GUARD_METRIC),
            ]
        return [
            "ip",
            "rule",
            verb,
            "priority",
            str(value["priority"]),
            "from",
            value["src"],
            "to",
            value["dst"],
            "table",
            str(value["table"]),
            "protocol",
            str(PROTOCOL),
        ]

    def _present(self, operation, rules, routes):
        kind, value = operation
        if kind == "guard":
            return any(guard_key(route) == (value, PROTOCOL, GUARD_METRIC) for route in routes)
        return any(rule_key(rule) == rule_key(value) for rule in rules)

    def _verify_removable(self, operation, rules, routes):
        kind, value = operation
        if kind == "rule":
            if any(
                r.get("priority") == value["priority"] and rule_key(r) != rule_key(value)
                for r in rules
            ):
                raise PolicyError("Foreign rule replaced an owned priority; refusing removal")
        elif any(
            str(r.get("table")) == str(value)
            and r.get("dst", "default") == "default"
            and r.get("metric") == GUARD_METRIC
            and guard_key(r) != (value, PROTOCOL, GUARD_METRIC)
            for r in routes
        ):
            raise PolicyError("Foreign route replaced an owned guard; refusing removal")

    def _preflight(self, desired, rules, routes):
        if any(str(route.get("table")) in {str(t) for t in desired["tables"]} for route in routes):
            raise PolicyError("A context routing table is already populated")
        priorities = {rule["priority"] for rule in desired["rules"]}
        for rule in rules:
            key = rule_key(rule)
            default = key is not None and key[:4] in [
                ("0.0.0.0/0", "0.0.0.0/0", 255, 0),
                ("0.0.0.0/0", "0.0.0.0/0", 254, 32766),
                ("0.0.0.0/0", "0.0.0.0/0", 253, 32767),
            ]
            if rule.get("priority") in priorities or str(rule.get("table")) in {
                str(t) for t in desired["tables"]
            }:
                raise PolicyError("A context rule priority or table is already in use")
            if default:
                continue
            src = rule_network(rule, "src")
            dst = rule_network(rule, "dst")
            if any(
                src.overlaps(IPv4Network(r["src"])) and dst.overlaps(IPv4Network(r["dst"]))
                for r in desired["rules"]
            ):
                raise PolicyError("A foreign policy rule overlaps the requested managed traffic")

    def ensure_layout(self, desired: dict):
        rules, routes = self._snapshot()
        if self.path.exists():
            state = self._load()
            if state["layout"] != desired:
                raise PolicyError("Policy selectors/tables are immutable until decommissioned")
            if state["phase"] == "restoring" or (
                state.get("boot_id") is not None and state["boot_id"] != self.boot_id()
            ):
                self.restore()
                state = self._load()
                rules, routes = self._snapshot()
            if state.get("pending") and self._present(state["pending"], rules, routes):
                raise PolicyError("Interrupted unacknowledged policy operation needs inspection")
            if state["phase"] != "active":
                self.remove()
                rules, routes = self._snapshot()
            else:
                if not all(self._present(op, rules, routes) for op in state["done"]):
                    raise PolicyError("Owned routing policy drift detected")
                for op in state["done"]:
                    self._verify_removable(op, rules, routes)
                owned = {rule_key(rule) for rule in desired["rules"]}
                self._preflight(
                    desired, [rule for rule in rules if rule_key(rule) not in owned], []
                )
                # Adopt only metadata for an older same-boot journal, never absent
                # kernel objects. The verified live ledger is its migration proof.
                if "boot_id" not in state:
                    state["boot_id"] = self.boot_id()
                    self._save(state)
                return
        self._preflight(desired, rules, routes)
        state = {
            "version": 1,
            "boot_id": self.boot_id(),
            "owner": self.owner,
            "layout": desired,
            "phase": "installing",
            "pending": None,
            "done": [],
        }
        self._save(state)
        operations = [("guard", table) for table in desired["tables"]]
        operations += [("rule", rule) for rule in desired["rules"]]
        try:
            for operation in operations:
                state["pending"] = operation
                self._save(state)
                self.execute(self._command(operation, "add"))
                state["done"].append(operation)
                state["pending"] = None
                self._save(state)
            state["phase"] = "active"
            self._save(state)
        except Exception:
            # Roll back only acknowledged objects; an uncertain operation is not adopted.
            rules, routes = self._snapshot()
            uncertain = state["pending"] and self._present(state["pending"], rules, routes)
            if not uncertain:
                state["pending"] = None
            self._save(state)
            if not uncertain:
                self.remove()
            raise

    def restore(self):
        """Restore only a committed ledger after proven kernel boot identity change.

        Same-boot drift and lost acknowledgements remain errors. During restoration
        acknowledged guards are retained on a known failure, rather than removed
        to reopen the main-table fallback before a safe retry.
        """
        state = self._load()
        current_boot = self.boot_id()
        if state["phase"] == "active" and state.get("boot_id") == current_boot:
            self.ensure_layout(state["layout"])
            return
        if state["phase"] == "active":
            if state.get("boot_id") is None or state.get("pending"):
                raise PolicyError("Unverified legacy or pending policy cannot be restored")
            expected = [["guard", t] for t in state["layout"]["tables"]]
            expected += [["rule", r] for r in state["layout"]["rules"]]
            if state["done"] != expected:
                raise PolicyError("Incomplete committed policy ledger")
            rules, routes = self._snapshot()
            self._preflight(state["layout"], rules, routes)
            state.update(
                phase="restoring", restoring_boot_id=current_boot, restored=[], pending=None
            )
            self._save(state)
        if state["phase"] != "restoring" or state["restoring_boot_id"] != current_boot:
            raise PolicyError("Interrupted policy restoration needs inspection")
        self._resume_restoration(state)

    def _resume_restoration(self, state):
        rules, routes = self._snapshot()
        pending = state.get("pending")
        if pending and self._present(pending, rules, routes):
            raise PolicyError("Unacknowledged restore operation needs inspection")
        for operation in state["restored"]:
            self._verify_removable(operation, rules, routes)
            if not self._present(operation, rules, routes):
                raise PolicyError("Restoration policy drift detected")
        foreign_rules = [
            r
            for r in rules
            if not any(
                op[0] == "rule" and rule_key(r) == rule_key(op[1]) for op in state["restored"]
            )
        ]
        foreign_routes = [
            r
            for r in routes
            if not any(
                op[0] == "guard" and guard_key(r) == (op[1], PROTOCOL, GUARD_METRIC)
                for op in state["restored"]
            )
        ]
        self._preflight(state["layout"], foreign_rules, foreign_routes)
        for operation in state["done"]:
            if operation in state["restored"]:
                continue
            state["pending"] = operation
            self._save(state)
            # On exception keep this pending record. A retry checks actual presence
            # before deciding whether the acknowledgement was lost.
            self.execute(self._command(operation, "add"))
            state["restored"].append(operation)
            state["pending"] = None
            self._save(state)
        state.update(phase="active", boot_id=self.boot_id(), pending=None)
        state.pop("restoring_boot_id")
        state.pop("restored")
        self._save(state)

    def remove(self):
        if not self.path.exists():
            return
        state = self._load()
        if state.get("boot_id", self.boot_id()) != self.boot_id():
            raise PolicyError("Restore and verify current-boot ownership before removal")
        if state["phase"] == "restoring":
            raise PolicyError("Complete or inspect restoration before decommissioning")
        rules, routes = self._snapshot()
        if state.get("pending") and self._present(state["pending"], rules, routes):
            raise PolicyError("Unacknowledged policy operation cannot be removed automatically")
        for operation in state["done"]:
            self._verify_removable(operation, rules, routes)
        for operation in reversed(state["done"]):
            rules, routes = self._snapshot()
            if self._present(operation, rules, routes):
                self.execute(self._command(operation, "del"))
            state["done"].remove(operation)
            state["pending"] = None
            self._save(state)
        self.path.unlink()


def restore_command(args):
    """Run a bounded native IPv4 command; never echo command output or credentials."""
    result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise PolicyError("Boot policy command failed")
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Restore owned routing policy before BIRD startup"
    )
    parser.add_argument("directory", type=Path)
    parser.add_argument("owner")
    args = parser.parse_args()
    if (args.directory / "owner").read_text() != args.owner + "\n":
        raise PolicyError("Boot policy owner mismatch")
    fd = os.open(args.directory / "context.lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        journal = PolicyJournal(args.directory, args.owner, restore_command)
        if journal.path.exists():
            journal.restore()


if __name__ == "__main__":
    main()
