"""Candidate failures against the independent native-command/state model.

These tests exercise the production transaction and durable bundle, not a real
BIRD process or systemd. The existing native/namespace tests cover those paths.
"""

import subprocess

import pytest
from maas_bird import external_policy
from maas_bird.runtime import RuntimeErrorBase, SessionsPending


def changed_timer(plan):
    first = plan.domains[0]
    assert first.bfd_tx != 300  # A genuine candidate, not a no-op reconciliation.
    return plan.model_copy(
        update={"domains": (first.model_copy(update={"bfd_tx": 300}), *plan.domains[1:])}
    )


def test_missing_candidate_token_restores_exact_previous_generation(rack_environment, monkeypatch):
    _, plan, passwords, service, host = rack_environment
    service.apply_plan(plan, passwords)
    previous = service.manifest.read_bytes(), service.config.read_bytes(), host.generation
    kernel = host.kernel.snapshot()
    control = host.control
    calls = []
    accepted_symbol = "mb_candidate_" + service._bundle()["token"]
    candidate_loaded = False

    def missing_token(*args, check=True):
        nonlocal candidate_loaded
        calls.append(args)
        if args[0] == "configure" and "timeout" in args:
            candidate_loaded = True
        if candidate_loaded and args[:2] == ("show", "symbols") and args[-1] != accepted_symbol:
            return subprocess.CompletedProcess(args, 0, "", "")
        return control(*args, check=check)

    monkeypatch.setattr(service, "_control", missing_token)
    with pytest.raises(SessionsPending, match="intended BIRD generation"):
        service.apply_plan(changed_timer(plan), passwords)
    assert host.clock >= 35
    assert host.active
    assert (
        service.manifest.read_bytes(),
        service.config.read_bytes(),
        host.generation,
    ) == previous
    assert external_policy.startup_gate(service.directory)["plan"] == plan.model_dump(mode="json")
    assert host.kernel.snapshot() == kernel
    assert ("configure", "confirm") not in calls


def test_failed_restore_stops_instead_of_running_unaccepted_candidate(
    rack_environment, monkeypatch
):
    _, plan, passwords, service, host = rack_environment
    service.apply_plan(plan, passwords)
    previous = service.manifest.read_bytes(), service.config.read_bytes()
    kernel = host.kernel.snapshot()
    control = host.control
    configure_count = 0

    def failed_restore(*args, check=True):
        nonlocal configure_count
        if configure_count and args[:2] == ("show", "symbols"):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "configure":
            configure_count += 1
            if configure_count == 2:
                raise RuntimeErrorBase("injected rollback command failure")
        return control(*args, check=check)

    monkeypatch.setattr(service, "_control", failed_restore)
    with pytest.raises(SessionsPending, match="intended BIRD generation"):
        service.apply_plan(changed_timer(plan), passwords)
    assert configure_count == 2
    assert not host.active
    assert (service.manifest.read_bytes(), service.config.read_bytes()) == previous
    assert external_policy.startup_gate(service.directory)["plan"] == plan.model_dump(mode="json")
    assert host.kernel.snapshot() == kernel


def test_policy_drift_during_acceptance_stops_routing_even_if_rollback_loads(
    rack_environment, monkeypatch
):
    _, plan, passwords, service, host = rack_environment
    service.apply_plan(plan, passwords)
    previous = service.manifest.read_bytes()
    control = host.control
    drifted = False
    candidate_loaded = False

    def drift_during_poll(*args, check=True):
        nonlocal drifted, candidate_loaded
        if args[0] == "configure" and "timeout" in args:
            candidate_loaded = True
        if candidate_loaded and args[:2] == ("show", "symbols") and not drifted:
            # Drift occurs AFTER apply_plan's initial validation, so a passing
            # preflight cannot substitute for checks while the candidate waits.
            host.kernel.rules.pop()
            drifted = True
        return control(*args, check=check)

    monkeypatch.setattr(service, "_control", drift_during_poll)
    with pytest.raises(SessionsPending, match="intended BIRD generation"):
        service.apply_plan(changed_timer(plan), passwords)
    assert drifted
    assert service.manifest.read_bytes() == previous
    assert not host.active
    # A durable old bundle is still unusable with missing static policy; the
    # runtime must not mistake reloading that configuration for safe recovery.
    bundle = external_policy.startup_gate(service.directory)
    with pytest.raises(external_policy.PolicyError, match="source rules"):
        external_policy.validate(bundle["policy"], host.execute)


def test_failed_restore_materialization_stops_and_preserves_original_failure(
    rack_environment, monkeypatch
):
    _, plan, passwords, service, host = rack_environment
    service.apply_plan(plan, passwords)
    previous = service.manifest.read_bytes()
    accepted = service._bundle()
    accepted_symbol = "mb_candidate_" + accepted["token"]
    control = host.control
    restored = []

    def missing_candidate(*args, check=True):
        if args[:2] == ("show", "symbols") and args[-1] != accepted_symbol:
            return subprocess.CompletedProcess(args, 0, "", "")
        return control(*args, check=check)

    def fail_materialization(bundle):
        restored.append(bundle)
        raise OSError("injected rollback materialization failure")

    monkeypatch.setattr(service, "_control", missing_candidate)
    monkeypatch.setattr(service, "_materialize", fail_materialization)
    # The caller must still see why acceptance failed. A secondary restore error
    # must stop the daemon, never leave the unaccepted live candidate running.
    with pytest.raises(SessionsPending, match="intended BIRD generation"):
        service.apply_plan(changed_timer(plan), passwords)
    assert restored == [accepted]
    assert not host.active
    assert service.manifest.read_bytes() == previous
    assert external_policy.startup_gate(service.directory)["plan"] == plan.model_dump(mode="json")
