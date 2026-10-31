"""Owned BIRD lifecycle for rack-scoped, externally provisioned routing contexts.

The static network owner installs persistent source rules and guards. This runtime
only verifies them. Authoritative restrictions are committed without an undo target;
other changes retain BIRD's bounded native rollback. No host-wide rule/table flush.
"""

import grp
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from ipaddress import IPv4Interface
from pathlib import Path
from typing import override

from charmlibs import apt

from maas_bird import external_policy, ovsdb, policy_objects
from maas_bird import runtime as daemon_runtime
from maas_bird.models import MultiDomainPlan, sessions_for_context
from maas_bird.render import protocol_name, quoted, render_rack
from maas_bird.runtime import (
    OwnedBird,
    RuntimeErrorBase,
    SessionsPending,
    accepted,
    run,
    write_atomic,
)


class MigrationRequired(RuntimeErrorBase):
    """Proposed static context layout is not provisioned; preserve accepted routing."""


class RackService(OwnedBird):
    """One owned daemon, one atomic bundle, one supported policy backend."""

    def __init__(self, root=Path("/"), owner="development"):
        super().__init__(root, owner)
        self.config = root / "run/maas-bird-config/bird.conf"

    @property
    def manifest(self):
        return self.directory / "state.json"

    def state(self):
        if not self.marker.exists():
            return None
        if self.marker.read_text() != self.owner + "\n":
            raise RuntimeErrorBase("Unknown service ownership marker")
        if not self.manifest.exists():
            return None
        value = json.loads(self.manifest.read_text())
        if value.get("owner") != self.owner or value.get("version") != 2:
            raise RuntimeErrorBase("Unknown rack routing state ownership")
        return MultiDomainPlan.model_validate(value["plan"])

    def observations(self):
        if not self.manifest.exists():
            return {}
        self.state()  # Verify ownership before reading persisted producer bindings.
        return json.loads(self.manifest.read_text()).get("producers", {})

    def credentials_changed(self, plan, passwords):
        if not self.manifest.exists():
            return False
        previous = json.loads(self.manifest.read_text()).get("credentials", {})
        return previous != self._credential_hashes(plan, passwords)

    @staticmethod
    def _credential_hashes(plan, passwords):
        return {s.id: hashlib.sha256(passwords[s.id].encode()).hexdigest() for s in plan.sessions}

    def health(self, plan):
        protocols = self._control("show", "protocols", "all", check=False)
        bfd = self._control("show", "bfd", "sessions", check=False)
        blocks = re.split(
            r"(?m)^(?=\w+\s+(?:BGP|Pipe|Kernel|Direct|Device|Static)\s)", protocols.stdout
        )
        bgp = [block for block in blocks if re.match(r"\w+\s+BGP\s", block)]
        established = 0
        up_protocols = set()
        for block in bgp:
            interfaces = re.findall(r"Interface:\s+(\S+)", block)
            # Match the scoped neighbor from the native block, not another Up
            # session elsewhere in this host's BFD output.
            neighbors = re.findall(r"Neighbor address:\s+(\S+)", block)
            interfaces += [peer.rsplit("%", 1)[1] for peer in neighbors if "%" in peer]
            rows = [
                line
                for line in bfd.stdout.splitlines()
                if line.split()
                and any(line.split()[0].split("%")[0] == peer.split("%")[0] for peer in neighbors)
                and (not interfaces or line.split()[1] in interfaces)
            ]
            normalized = re.sub(r"^\w+", "uplink_0", block)
            if not (protocols.returncode or bfd.returncode) and accepted(
                normalized, "\n".join(rows), 1
            ):
                established += 1
                up_protocols.add(block.split()[0])
        # A node's own exact /32 is delivered by the kernel local table before
        # source-policy lookup. It need not be learned back through its peer
        # (and BGP AS-loop prevention normally rejects that advertisement).
        # Read native state only when this case occurs; an aggregate or another
        # context's source never earns this exemption.
        local_candidates = {
            prefix
            for domain in plan.domains
            for context in domain.contexts
            for prefix in domain.imports
            if prefix in context.sources
        }
        local_sources = set()
        if local_candidates:
            addresses = json.loads(run(["ip", "-4", "-j", "address", "show", "dev", "lo"]).stdout)
            local_sources = {
                IPv4Interface(f"{address['local']}/{address['prefixlen']}").network
                for interface in addresses
                for address in interface.get("addr_info", [])
            }
        missing, fallback = 0, 0
        for domain in plan.domains:
            for context in domain.contexts:
                routes = json.loads(
                    run(
                        ["ip", "-4", "-j", "-N", "route", "show", "table", str(context.table)]
                    ).stdout
                )
                local = {
                    s.interface
                    for s in sessions_for_context(domain, context)
                    if context.numa_node is None or s.numa_node == context.numa_node
                }
                for prefix in domain.imports:
                    if prefix in context.sources and prefix in local_sources:
                        continue
                    selected = [r for r in routes if r.get("dst") == str(prefix) and r.get("dev")]
                    missing += not selected
                    fallback += bool(selected) and not any(r["dev"] in local for r in selected)
        return {
            "configured": len(plan.sessions),
            "established": established,
            "missing_routes": missing,
            "fallback_routes": fallback,
            "up_sessions": [
                s.id
                for d in plan.domains
                for s in d.sessions
                if protocol_name(d.domain, s.id) in up_protocols
            ],
        }

    def verify_current(self, *, check_bindings=True):
        """Check accepted host prerequisites even when a new proposal is invalid."""
        current = self.state()
        if current is not None:
            if current.host.system_uuid != external_policy.host_uuid():
                self.stop()
                raise RuntimeErrorBase("Accepted routing belongs to another physical host")
            try:
                external_policy.validate(
                    external_policy.expected_policy(current),
                    run,
                    check_routes=self._bundle()["phase"] == "accepted",
                    check_bindings=check_bindings,
                )
            except Exception:
                self.stop()
                raise

    def _prepare_rack(self, plan):
        current = self.state()
        self.verify_current(check_bindings=False)
        # A rejected proposal is not evidence of drift in the active generation.
        try:
            external_policy.validate(
                external_policy.expected_policy(plan), run, check_routes=False
            )
        except external_policy.PolicyError as exc:
            raise MigrationRequired(str(exc)) from exc
        if not self.marker.exists():
            self._claim_rack(plan)
        elif self.marker.read_text() != self.owner + "\n":
            raise RuntimeErrorBase("Unknown service ownership marker")
        elif current is None:
            # A retained directory is not retained routing authority. After an
            # invalidation, quiesce any unknown owned process and repeat the
            # foreign-service/table admission check before establishing a floor.
            self.stop()
            self._check_unclaimed_network(plan)
        if (self.directory / "policy.json").exists():
            raise MigrationRequired("Decommission the prototype's owned policy before migration")
        if not bool(shutil.which("bird")):
            if (self.root / "etc/bird").exists():
                raise RuntimeErrorBase(
                    "Inspect existing vendor configuration before installing bird2"
                )
            # New package postinst must never start a vendor daemon outside our
            # policy gate. The intentionally retained mask survives interrupted
            # installation and reboot; decommission does not resurrect it.
            run(["systemctl", "mask", "bird.service"])
            write_atomic(self.directory / "vendor-mask-owned", "bird.service\n", 0o600)
            apt.add_package("bird2", update_cache=True)
        response = run(["bird", "--version"])
        version = response.stdout + response.stderr
        if not re.search(r"\b2\.14(?:\.\d+)?\b", version):
            raise RuntimeErrorBase("This release requires the validated BIRD 2.14 series")
        self._install_rack_files()

    def _claim_rack(self, plan):
        if self.directory.exists() or self.unit.exists() or self.policy_unit.exists():
            raise RuntimeErrorBase("Refusing to adopt existing BIRD configuration")
        self._check_unclaimed_network(plan)
        self.directory.mkdir(parents=True, mode=0o750)
        write_atomic(self.marker, self.owner + "\n", 0o600)

    @staticmethod
    def _check_unclaimed_network(plan):
        for verb in ("is-active", "is-enabled"):
            if run(["systemctl", verb, "bird.service"], check=False).returncode == 0:
                raise RuntimeErrorBase("Disable the foreign vendor BIRD service before activation")
        routes = json.loads(run(["ip", "-4", "-j", "-N", "route", "show", "table", "all"]).stdout)
        for route in routes:
            if str(route.get("table")) in {
                str(c.table) for c in plan.contexts
            } and policy_objects.guard_key(route) != (
                int(route["table"]),
                plan.policy_protocol,
                policy_objects.GUARD_METRIC,
            ):
                raise RuntimeErrorBase("Refusing to adopt pre-existing routes in dedicated tables")

    def _install_rack_files(self):
        # Install the minimal stdlib startup gate and this unit's service file.
        package = self.directory / "maas_bird"
        package.mkdir(exist_ok=True)
        files = {
            package / "__init__.py": "",
            package / "policy_objects.py": Path(policy_objects.__file__).read_text(),
            package / "runtime.py": Path(daemon_runtime.__file__).read_text(),
            package / "external_policy.py": Path(external_policy.__file__).read_text(),
            package / "ovsdb.py": Path(ovsdb.__file__).read_text(),
            self.directory / "check_policy.py": (
                "from maas_bird.external_policy import main\nmain()\n"
            ),
            self.unit: _unit(),
        }
        pending = self.directory / "reload-pending"
        changed = any(
            not path.exists() or path.read_text() != text for path, text in files.items()
        )
        if changed:
            write_atomic(pending, "pending\n", 0o600)
            for path, text in files.items():
                if not path.exists() or path.read_text() != text:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    write_atomic(path, text, 0o644 if path == self.unit else 0o600)
        if pending.exists():
            run(["systemctl", "daemon-reload"])
            pending.unlink()

    def apply_plan(self, plan, passwords, *, restrictive=False, observations=None):
        self._producers = observations if observations is not None else self.observations()
        self._credentials = self._credential_hashes(plan, passwords)
        if plan.host.system_uuid != external_policy.host_uuid():
            raise RuntimeErrorBase("Proposed routing belongs to another physical host")
        self._prepare_rack(plan)
        if not restrictive:
            # Grants retain full validation of the accepted generation. A
            # restriction instead validates only its own reduced binding set.
            self.verify_current()
        if self._bundle() is None:
            self._bootstrap(plan, passwords)
        bundle = self._bundle()
        assert bundle is not None
        logical = render_rack(plan, passwords)
        if restrictive and not self._active():
            # A changed logical attachment may make the previous generation
            # impossible to restart. Validate and persist the smaller authority
            # before cold-starting, rather than materializing that stale bundle.
            self._check_restriction(MultiDomainPlan.model_validate(bundle["plan"]), plan, True)
            self._write_config(self.config, logical)
            run(["bird", "-p", "-c", str(self.config)])
            bundle = self._commit(plan, logical, uuid.uuid4().hex, "authorization-floor")
        if not self._active():
            self._materialize(bundle)
            self._start()
            self._wait_live(bundle)
        if not restrictive and not self._live_matches(bundle):
            # Native confirmation followed by agent death before durable commit
            # can leave a live generation ahead of disk. Quiesce and reload the
            # known authorization floor rather than trusting healthy old peers.
            self.stop()
            self._materialize(bundle)
            self._start()
            self._wait_live(bundle)
        if (
            bundle["intent_sha256"] == hashlib.sha256(logical.encode()).hexdigest()
            and bundle["phase"] == "accepted"
        ):
            self._commit(plan, logical, bundle["token"])
            return
        self._transaction(plan, logical, restrictive or bundle["phase"] == "authorization-floor")

    def _bundle(self):
        if not self.manifest.exists():
            return None
        value = json.loads(self.manifest.read_text())
        if value.get("version") != 2 or value.get("owner") != self.owner:
            raise RuntimeErrorBase("Unknown routing state ownership")
        return value

    def _commit(self, plan, logical, token, phase="accepted"):
        configuration = logical + f"\ndefine mb_candidate_{token} = 1;\n"
        record = {
            "version": 2,
            "owner": self.owner,
            "phase": phase,
            "token": token,
            "plan": plan.model_dump(mode="json"),
            "configuration": configuration,
            "intent_sha256": hashlib.sha256(logical.encode()).hexdigest(),
            "config_sha256": hashlib.sha256(configuration.encode()).hexdigest(),
            "policy": external_policy.expected_policy(plan),
            "producers": self._producers,
            "credentials": self._credentials,
        }
        # One fsynced rename commits policy, configuration and provenance together.
        write_atomic(self.manifest, json.dumps(record), 0o600)
        return record

    def _materialize(self, bundle):
        self.config.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        group = grp.getgrnam("bird").gr_gid
        os.chown(self.config.parent, 0, group)
        self.config.parent.chmod(0o750)
        write_atomic(self.config, bundle["configuration"], 0o640, group)

    def _live_matches(self, bundle):
        reply = self._control("show", "symbols", "mb_candidate_" + bundle["token"], check=False)
        return reply.returncode == 0 and any(
            row.split()[:1] == ["mb_candidate_" + bundle["token"]]
            for row in reply.stdout.splitlines()
        )

    def _wait_live(self, bundle):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self._live_matches(bundle):
                return
            time.sleep(0.1)
        self.stop()
        raise SessionsPending("Accepted BIRD generation did not start")

    def _bootstrap(self, plan, passwords):
        # Never cold-start service VIPs without native rollback protection. A
        # zero-export baseline is safe across agent death before acceptance.
        baseline = plan.model_copy(
            update={
                "domains": tuple(
                    d.model_copy(
                        update={
                            "prefixes": (),
                            "contexts": tuple(
                                c.model_copy(update={"exports": ()}) for c in d.contexts
                            ),
                        }
                    )
                    for d in plan.domains
                )
            }
        )
        if self._active():
            self.stop()  # An owned process without an accepted bundle is unknown.
        bundle = self._commit(baseline, render_rack(baseline, passwords), uuid.uuid4().hex)
        self._materialize(bundle)
        self._start()
        self._wait_live(bundle)

    def _transaction(self, plan, logical, restrictive):
        previous = self._bundle()
        assert previous is not None  # Bootstrap always establishes a safe floor.
        old_plan = MultiDomainPlan.model_validate(previous["plan"])
        self._check_restriction(old_plan, plan, restrictive)
        token = uuid.uuid4().hex
        configuration = logical + f"\ndefine mb_candidate_{token} = 1;\n"
        self._write_config(self.config, configuration)
        try:
            run(["bird", "-p", "-c", str(self.config)])
        except Exception:
            self._materialize(previous)
            raise
        # A native rollback must never load pre-withdrawal exports after restart.
        if restrictive:
            try:
                self._commit(plan, logical, token, "authorization-floor")
            except Exception:
                # A failed durable revocation cannot leave its old authority live.
                # Unlinking the previous bundle also denies restart if a transient
                # full filesystem permits removal but not replacement writes.
                try:
                    self.invalidate()
                finally:
                    self.stop()
                raise
        deadline = time.monotonic() + 35
        try:
            args = ("configure", quoted(str(self.config)))
            self._control(*(args if restrictive else args + ("timeout", "60")))
            required, paths = self._requirements(old_plan, plan)
            self._accept(
                "mb_candidate_" + token, logical, plan, deadline, required, paths, restrictive
            )
            if not restrictive:
                reply = self._control("configure", "confirm")
                if (
                    "Reconfiguration confirmed" not in reply.stdout.splitlines()
                    or time.monotonic() >= deadline
                ):
                    raise SessionsPending("Candidate did not receive positive confirmation")
            self._commit(plan, logical, token)
        except Exception:
            if restrictive:
                self.stop()  # Durable floor survives; no stale undo or restart.
            else:
                self._restore(previous)
            raise

    def _restore(self, previous):
        """Restore accepted authority only while its native prerequisites hold."""
        try:
            self._materialize(previous)
            old = MultiDomainPlan.model_validate(previous["plan"])
            if old.host.system_uuid != external_policy.host_uuid():
                raise RuntimeErrorBase("Accepted routing belongs to another physical host")
            # A configure reload skips systemd ExecStartPre. Policy can disappear
            # while acceptance polls, or while the reload itself is in flight.
            external_policy.validate(previous["policy"], run)
            self._control("configure", quoted(str(self.config)))
            external_policy.validate(previous["policy"], run)
            if not self._live_matches(previous):
                raise RuntimeErrorBase("Accepted BIRD generation was not restored")
        except Exception:
            self.stop()

    @staticmethod
    def _requirements(old, proposed):
        sessions = {s.id: s for s in old.sessions}
        local_identity_changed = (
            old.host.local_asn != proposed.host.local_asn
            or old.host.router_id != proposed.host.router_id
        )
        required = {
            s.id for s in proposed.sessions if local_identity_changed or sessions.get(s.id) != s
        }
        paths = local_identity_changed or any(
            set(domain.imports)
            - set(next((d.imports for d in old.domains if d.domain == domain.domain), ()))
            for domain in proposed.domains
        )
        # The initial zero-export baseline still needs a usable path per domain
        # before granting any first service exports.
        paths = paths or (not old.prefixes and bool(proposed.prefixes))
        return required, paths

    @staticmethod
    def _check_restriction(old, proposed, restrictive):
        if not restrictive:
            return
        if (
            old.host != proposed.host
            or external_policy.expected_policy(old)["rules"]
            != external_policy.expected_policy(proposed)["rules"]
        ):
            raise RuntimeErrorBase(
                "A restrictive update cannot change static bindings or host identity"
            )
        prior = {d.domain: d for d in old.domains}
        for domain in proposed.domains:
            previous = prior.get(domain.domain)
            if (
                previous is None
                or not set(domain.prefixes) <= set(previous.prefixes)
                or not set(domain.imports) <= set(previous.imports)
                or not set(domain.sessions) <= set(previous.sessions)
            ):
                raise RuntimeErrorBase("A restrictive update cannot add routing authorization")

    def _accept(
        self, token, contents, plan, deadline, required_sessions, needs_paths, restrictive
    ):
        wanted = set(re.findall(r"protocol bgp ([a-zA-Z0-9_]+)", contents))
        while time.monotonic() < deadline:
            symbols = self._control("show", "symbols", token)
            protocols = self._control("show", "protocols", "all")
            loaded = set(re.findall(r"(?m)^([a-zA-Z0-9_]+)\s+BGP\s", protocols.stdout))
            if (
                any(row.split()[:1] == [token] for row in symbols.stdout.splitlines())
                and loaded == wanted
            ):
                try:
                    external_policy.validate(external_policy.expected_policy(plan), run)
                except external_policy.PolicyError:
                    time.sleep(0.2)
                    continue
                if not restrictive and (required_sessions or needs_paths):
                    health = self.health(plan)
                    up = set(health["up_sessions"])
                    domain_paths = all(
                        not domain.sessions or any(s.id in up for s in domain.sessions)
                        for domain in plan.domains
                    )
                    if not required_sessions <= up or (
                        needs_paths and (health["missing_routes"] or not domain_paths)
                    ):
                        time.sleep(0.2)
                        continue
                if time.monotonic() < deadline:
                    return
            time.sleep(0.2)
        raise SessionsPending("Waiting for the intended BIRD generation, not for every peer")

    @override
    def invalidate(self):
        """Durably deny restart before attempting to stop a running daemon."""
        if not self.marker.exists() or self.marker.read_text() != self.owner + "\n":
            return
        self.manifest.unlink(missing_ok=True)
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.config.unlink(missing_ok=True)

    def remove(self):
        if self.marker.exists() and self.marker.read_text() != self.owner + "\n":
            raise RuntimeErrorBase("Refusing to decommission another unit's service")
        if not self.marker.exists():
            return
        self.invalidate()
        self.stop()
        allowed = {
            "owner",
            "state.json",
            "bird.conf",
            "candidate.conf",
            "active-policy.json",
            "check_policy.py",
            "reload-pending",
            "exports.json",
            "maas_bird",
            "activation.json",
            "vendor-mask-owned",
        }
        if any(path.name not in allowed for path in self.directory.iterdir()):
            raise RuntimeErrorBase("Unknown files prevent decommission; inspect owned directory")
        for path in self.directory.iterdir():
            if path.name == "maas_bird":
                for name in (
                    "__init__.py",
                    "policy_objects.py",
                    "external_policy.py",
                    "ovsdb.py",
                    "runtime.py",
                ):
                    (path / name).unlink(missing_ok=True)
                path.rmdir()
            else:
                path.unlink()
        self.directory.rmdir()
        self.unit.unlink(missing_ok=True)
        run(["systemctl", "daemon-reload"])


def _unit():
    return """[Unit]
Description=Rack-scoped BIRD routing
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0
[Service]
Type=simple
User=bird
Group=bird
# Only the control socket directory belongs to the daemon user. Declaring the
# configuration here would make systemd recursively chown it before ExecStart.
RuntimeDirectory=maas-bird
ExecStartPre=+/usr/bin/python3 -B /etc/maas-bird/check_policy.py /etc/maas-bird
ExecStart=/usr/sbin/bird -f -c /run/maas-bird-config/bird.conf -s /run/maas-bird/bird.ctl
Restart=on-failure
RestartSec=5
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
"""
