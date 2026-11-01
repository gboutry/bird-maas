"""Filesystem and transaction failures against an independent native-command model.

These tests do not claim that systemd or BIRD ran. Native daemon and isolated
systemd tests cover those boundaries. The model rejects every unsupported command.
"""

import re
import subprocess
from copy import deepcopy

import pytest
from maas_bird import external_policy, runtime
from maas_bird.rack_runtime import MigrationRequired
from maas_bird.render import render_rack
from maas_bird.runtime import RuntimeErrorBase


def test_first_activation_commits_matching_startup_files_without_kernel_writes(rack_environment):
    _, plan, passwords, service, host = rack_environment
    baseline = host.kernel.snapshot()
    service.apply_plan(plan, passwords)
    assert service.state() == plan
    external_policy.startup_gate(service.directory)
    assert host.active
    bundle = external_policy.startup_gate(service.directory)
    assert service.config.read_text() == bundle["configuration"]
    assert bundle["configuration"] == (
        render_rack(plan, passwords) + f"\ndefine mb_candidate_{bundle['token']} = 1;\n"
    )
    assert host.kernel.snapshot() == baseline
    assert not (service.directory / "activation.json").exists()
    assert not (service.directory / "candidate.conf").exists()
    assert service.unit.exists()
    assert not service.policy_unit.exists()


def test_restrictive_withdrawal_keeps_daemon_and_other_domain_configured(rack_environment):
    _, plan, passwords, service, host = rack_environment
    service.apply_plan(plan, passwords)
    other = plan.domains[1]
    first = plan.domains[0].model_copy(update={"prefixes": ()})
    changed = plan.model_copy(update={"domains": (first, other)})
    host.calls.clear()
    service.apply_plan(changed, passwords, restrictive=True)
    assert host.active
    assert not any("disable" in call for call in host.calls)
    assert service.state().domains[1] == other
    assert service.state().domains[0].prefixes == ()
    text = service.config.read_text()
    vip = str(plan.domains[0].prefixes[0].network_address)
    assert not re.search(r"protocol static \w+origin_" + vip.replace(".", "_") + r"\s*\{", text)
    assert f'route {vip}/32 via "lo";' not in text
    external_policy.startup_gate(service.directory)


def test_unprovisioned_proposal_preserves_valid_accepted_routing(rack_environment):
    _, plan, passwords, service, host = rack_environment
    service.apply_plan(plan, passwords)
    original = service.config.read_bytes()
    first = plan.domains[0]
    context = first.contexts[0].model_copy(update={"table": 9000})
    changed = plan.model_copy(
        update={
            "domains": (
                first.model_copy(update={"contexts": (context, *first.contexts[1:])}),
                plan.domains[1],
            )
        }
    )
    with pytest.raises(MigrationRequired):
        service.apply_plan(changed, passwords)
    assert host.active
    assert service.config.read_bytes() == original
    assert service.state() == plan


def test_real_active_policy_drift_stops_owned_routing(rack_environment):
    _, plan, passwords, service, host = rack_environment
    service.apply_plan(plan, passwords)
    host.kernel.routes.pop()
    with pytest.raises(external_policy.PolicyError):
        service.apply_plan(plan, passwords)
    assert not host.active


def test_lost_restrictive_ack_never_restores_withdrawn_generation(rack_environment):
    _, plan, passwords, service, host = rack_environment
    service.apply_plan(plan, passwords)
    changed = plan.model_copy(
        update={"domains": tuple(d.model_copy(update={"prefixes": ()}) for d in plan.domains)}
    )
    host.fail_control = ("show", "symbols")
    with pytest.raises(RuntimeErrorBase):
        service.apply_plan(changed, passwords, restrictive=True)
    assert not host.active
    # The failed acknowledgement must not erase the restrictive startup floor.
    # A later process/reboot may load this floor, never the previous exports.
    bundle = external_policy.startup_gate(service.directory)
    assert bundle["phase"] == "authorization-floor"
    assert service.state() == changed
    assert not service.state().prefixes
    assert all(f'route {p} via "lo";' not in bundle["configuration"] for p in plan.prefixes)
    host.fail_control = None
    service._materialize(bundle)
    service._start()
    assert host.active
    assert host.generation == bundle["configuration"]


def test_noop_confirmation_is_rejected_without_persisting_candidate(rack_environment):
    _, plan, passwords, service, host = rack_environment
    service.apply_plan(plan, passwords)
    before = service.manifest.read_bytes(), service.config.read_bytes()
    first = plan.domains[0].model_copy(update={"bfd_tx": 300})
    changed = plan.model_copy(update={"domains": (first, plan.domains[1])})
    host.confirm = "Nothing to do"
    with pytest.raises(runtime.SessionsPending):
        service.apply_plan(changed, passwords)
    assert service.state() == plan
    assert (service.manifest.read_bytes(), service.config.read_bytes()) == before


def test_unchanged_reconcile_does_not_reload_systemd(rack_environment):
    _, plan, passwords, service, host = rack_environment
    service.apply_plan(plan, passwords)
    host.calls.clear()
    service.apply_plan(plan, passwords)
    assert not any("daemon-reload" in args for args in host.calls)


def test_decommission_leaves_all_static_host_policy_untouched(rack_environment):
    _, plan, passwords, service, host = rack_environment
    service.apply_plan(plan, passwords)
    baseline = deepcopy(host.kernel.snapshot())
    service.remove()
    assert host.kernel.snapshot() == baseline
    assert not service.directory.exists()
    assert not service.unit.exists()
    assert not host.active


def test_first_unconfirmed_grant_leaves_only_safe_zero_export_baseline(rack_environment):
    _, plan, passwords, service, host = rack_environment
    host.confirm = "Nothing to do"
    with pytest.raises(runtime.SessionsPending):
        service.apply_plan(plan, passwords)
    baseline = service.state()
    assert baseline is not None and baseline.prefixes == ()
    assert all(not context.exports for context in baseline.contexts)
    bundle = external_policy.startup_gate(service.directory)
    assert bundle["phase"] == "accepted"
    assert host.generation == bundle["configuration"]
    assert all(f'route {p} via "lo";' not in host.generation for p in plan.prefixes)


def test_authority_invalidation_denies_restart_before_stop(rack_environment):
    _, plan, passwords, service, host = rack_environment
    service.apply_plan(plan, passwords)
    service.invalidate()
    # Even if stop cannot reach systemd, durable authority has already disappeared.
    assert host.active
    assert not service.manifest.exists()
    with pytest.raises(OSError):
        external_policy.startup_gate(service.directory)


def test_corrupt_bundle_host_binding_never_starts(rack_environment):
    _, plan, passwords, service, host = rack_environment
    wrong = plan.model_copy(update={"host": plan.host.model_copy(update={"system_uuid": "wrong"})})
    with pytest.raises(RuntimeErrorBase, match="physical host"):
        service.apply_plan(wrong, passwords)
    assert not host.active
    assert not service.manifest.exists()


def test_failed_restrictive_bundle_write_denies_old_startup_authority(
    rack_environment, monkeypatch
):
    _, plan, passwords, service, host = rack_environment
    service.apply_plan(plan, passwords)
    changed = plan.model_copy(
        update={"domains": tuple(d.model_copy(update={"prefixes": ()}) for d in plan.domains)}
    )
    commit = service._commit

    def fail_floor(p, logical, token, phase="accepted"):
        if phase == "authorization-floor":
            raise OSError("simulated state write failure")
        return commit(p, logical, token, phase)

    monkeypatch.setattr(service, "_commit", fail_floor)
    with pytest.raises(OSError, match="simulated state write"):
        service.apply_plan(changed, passwords, restrictive=True)
    assert not host.active
    assert not service.manifest.exists()
    with pytest.raises(OSError):
        external_policy.startup_gate(service.directory)


def test_parser_failure_restores_previous_cache_without_native_reconfiguration(
    rack_environment, monkeypatch
):
    _, plan, passwords, service, host = rack_environment
    service.apply_plan(plan, passwords)
    before = service.manifest.read_bytes(), service.config.read_bytes(), host.generation
    changed = plan.model_copy(
        update={
            "domains": (
                plan.domains[0].model_copy(update={"bfd_tx": 300}),
                plan.domains[1],
            )
        }
    )
    execute = host.execute

    def fail_parser(args, check=True):
        if args[:2] == ["bird", "-p"]:
            raise RuntimeErrorBase("simulated parser failure")
        return execute(args, check)

    monkeypatch.setattr("maas_bird.rack_runtime.run", fail_parser)
    with pytest.raises(RuntimeErrorBase, match="simulated parser"):
        service.apply_plan(changed, passwords)
    assert host.active
    assert (service.manifest.read_bytes(), service.config.read_bytes(), host.generation) == before


def test_accepted_bundle_write_failure_rolls_back_confirmed_live_candidate(
    rack_environment, monkeypatch
):
    _, plan, passwords, service, host = rack_environment
    service.apply_plan(plan, passwords)
    before = service.manifest.read_bytes(), service.config.read_bytes(), host.generation
    changed = plan.model_copy(
        update={
            "domains": (
                plan.domains[0].model_copy(update={"bfd_tx": 300}),
                plan.domains[1],
            )
        }
    )

    def fail_commit(*args, **kwargs):
        raise OSError("simulated final commit failure")

    monkeypatch.setattr(service, "_commit", fail_commit)
    with pytest.raises(OSError, match="simulated final commit"):
        service.apply_plan(changed, passwords)
    assert host.active
    assert (service.manifest.read_bytes(), service.config.read_bytes(), host.generation) == before


def test_removed_accepted_loopback_vip_stops_routing_and_denies_startup(rack_environment):
    _, plan, passwords, service, host = rack_environment
    service.apply_plan(plan, passwords)
    host.sources.pop()
    with pytest.raises(external_policy.PolicyError, match="source VIP"):
        service.verify_current()
    assert not host.active
    bundle = external_policy.startup_gate(service.directory)
    with pytest.raises(external_policy.PolicyError, match="source VIP"):
        external_policy.validate(bundle["policy"], host.execute)


@pytest.mark.parametrize("changed", [{"local_asn": 4200009999}, {"router_id": "10.255.255.99"}])
def test_new_local_bgp_identity_requires_all_sessions_and_paths(rack_environment, changed):
    _, plan, _, service, _ = rack_environment
    updated_host = plan.host.model_copy(update=changed)
    proposed = plan.model_copy(
        update={
            "host": updated_host,
            "domains": tuple(d.model_copy(update={"host": updated_host}) for d in plan.domains),
        }
    )
    # Local ASN/router ID live on Host, so comparing Session objects alone misses
    # the native BGP restart and could confirm before replacement paths exist.
    required, needs_paths = service._requirements(plan, proposed)
    assert required == {s.id for s in proposed.sessions}
    assert needs_paths


def test_reauthorization_does_not_adopt_foreign_routes_after_invalidation(rack_environment):
    _, plan, passwords, service, host = rack_environment
    service.apply_plan(plan, passwords)
    service.invalidate()
    service.stop()
    host.kernel.routes.append(
        {
            "table": plan.contexts[0].table,
            "protocol": 197,
            "dst": "203.0.113.0/24",
            "dev": "foreign0",
        }
    )
    before = host.kernel.snapshot()
    with pytest.raises(RuntimeErrorBase, match="pre-existing routes"):
        service.apply_plan(plan, passwords)
    assert not host.active
    assert not service.manifest.exists()
    assert host.kernel.snapshot() == before


@pytest.mark.parametrize("failed", [False, True])
def test_owned_start_resets_only_a_failed_service(tmp_path, monkeypatch, failed):
    calls = []

    def run(args, check=True):
        calls.append((args, check))
        return subprocess.CompletedProcess(
            args, int(not failed) if "is-failed" in args else 0, "", ""
        )

    monkeypatch.setattr(runtime, "run", run)
    runtime.OwnedBird(tmp_path)._start()

    assert calls[0] == (["systemctl", "is-failed", "--quiet", "maas-bird.service"], False)
    if failed:
        assert calls[1] == (["systemctl", "reset-failed", "maas-bird.service"], True)
    else:
        assert len(calls) == 2
    assert calls[-1] == (["systemctl", "enable", "--now", "maas-bird.service"], True)


@pytest.mark.parametrize("fault", ["query", "reset", "enable"])
def test_owned_start_never_ignores_a_real_service_failure(tmp_path, monkeypatch, fault):
    calls = []

    def run(args, check=True):
        calls.append(args)
        if "is-failed" in args:
            return subprocess.CompletedProcess(args, 2 if fault == "query" else 0, "", "")
        if args[1] == {"reset": "reset-failed", "enable": "enable"}.get(fault):
            raise RuntimeErrorBase("native failure")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(runtime, "run", run)
    with pytest.raises(RuntimeErrorBase):
        runtime.OwnedBird(tmp_path)._start()

    if fault != "enable":
        assert not any("enable" in args for args in calls)
