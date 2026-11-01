"""Adversarial regressions for R1–R9; generated faults retain the ownership oracle.

The kernel model checks journal transitions, not forwarding. Namespace and native
BIRD tests exercise those real boundaries separately.
"""

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pytest
import scenario as testing
from hypothesis import given, settings
from hypothesis import strategies as st
from maas_bird import rack_runtime
from maas_bird.interfaces import encode
from maas_bird.inventory import response_data
from maas_bird.models import Inventory, UnitInventory

from tests.factories import consumer_config, inventory_response, make_inputs
from tests.policy_model import InjectedFailure, KernelModel
from tests.prototype import runtime
from tests.prototype.policy import PolicyError, PolicyRouting, guard_key
from tests.prototype.resolver import AuthorizationWithdrawn, NotReady, resolve
from tests.prototype.service_units import bird_unit, policy_unit
from tests.strategies import fresh_domain_inputs


@settings(max_examples=30, deadline=None)
@given(fail_at=st.integers(1, 12), numeric=st.booleans(), lost_ack=st.booleans())
def test_reboot_restore_never_adopts_uncertain_objects(fail_at, numeric, lost_ack):
    """Generate failures at every guard/rule boundary, on both JSON wire forms.

    Known failures retain acknowledged guards and can resume. Lost acknowledgements
    require inspection; neither retry nor cleanup is allowed to claim ownership.
    """
    kernel = KernelModel(numeric)
    baseline = kernel.snapshot()
    boot = ["before"]
    with TemporaryDirectory() as directory:
        policy = PolicyRouting(Path(directory), "owner", kernel.execute, lambda: boot[0])
        plan = resolve(**fresh_domain_inputs())
        policy.ensure(plan)
        installed = kernel.snapshot()
        kernel.rules, kernel.routes = deepcopy(baseline)
        kernel.mutations, kernel.fail_at, kernel.lose_ack = 0, fail_at, lost_ack
        boot[0] = "after"
        with pytest.raises(InjectedFailure):
            policy.ensure(plan)
        failed = kernel.snapshot()
        # No restoration failure deletes an acknowledged guard or foreign object.
        assert kernel.routes[: len(baseline[1])] == baseline[1]
        assert kernel.rules[: len(baseline[0])] == baseline[0]
        kernel.fail_at = None
        if lost_ack:
            with pytest.raises(PolicyError, match="Unacknowledged"):
                policy.ensure(plan)
            with pytest.raises(PolicyError):
                policy.remove()
            assert kernel.snapshot() == failed
        else:
            policy.ensure(plan)
            assert kernel.snapshot() == installed
            policy.ensure(plan)
            assert json.loads(policy.path.read_text())["boot_id"] == "after"
            policy.remove()
            assert kernel.snapshot() == baseline


@pytest.mark.parametrize("reboot", [False, True])
def test_drift_and_foreign_reboot_objects_are_not_adopted(tmp_path, reboot):
    kernel = KernelModel()
    boot = ["first"]
    policy = PolicyRouting(tmp_path, "owner", kernel.execute, lambda: boot[0])
    plan = resolve(**fresh_domain_inputs())
    policy.ensure(plan)
    if reboot:
        boot[0] = "second"  # Exact-looking objects are still foreign on a new boot.
    else:
        kernel.routes.pop()
    before = kernel.snapshot()
    with pytest.raises(PolicyError):
        policy.ensure(plan)
    assert kernel.snapshot() == before
    if reboot:
        with pytest.raises(PolicyError, match="current-boot"):
            policy.remove()
        assert kernel.snapshot() == before


@settings(max_examples=25, deadline=None)
@given(tos=st.integers(1, 255), numeric=st.booleans())
def test_tos_replacement_blocks_reconcile_and_cleanup_before_mutation(tos, numeric):
    """A selector-restricted route is not the fail-closed catch-all we own."""
    kernel = KernelModel(numeric)
    with TemporaryDirectory() as directory:
        policy = PolicyRouting(Path(directory), "owner", kernel.execute)
        plan = resolve(**fresh_domain_inputs())
        policy.ensure(plan)
        kernel.routes[-1]["tos"] = hex(tos)
        before = kernel.snapshot()
        with pytest.raises(PolicyError):
            policy.ensure(plan)
        with pytest.raises(PolicyError, match="Foreign route"):
            policy.remove()
        assert kernel.snapshot() == before
        assert guard_key(kernel.routes[-1]) is None


def test_boot_units_gate_bird_and_restore_before_network():
    early = policy_unit("model:bird/0")
    workload = bird_unit("model:bird/0")
    assert "Before=network-pre.target" in early
    assert "DefaultDependencies=no" in early
    assert "Requires=maas-bird-policy.service" in workload
    assert "ExecStartPre=+/usr/bin/python3 /etc/maas-bird/policy_journal.py" in workload
    assert "ReadWritePaths=/etc/maas-bird" in workload
    with pytest.raises(ValueError, match="Unsafe"):
        bird_unit("bad%owner")


@pytest.fixture
def service(tmp_path, monkeypatch):
    """Keep the filesystem real; explicitly double only package/systemd boundaries."""
    service = runtime.BirdService(tmp_path)
    service.directory.mkdir(parents=True)
    service.marker.write_text("development\n")
    monkeypatch.setattr(service, "_prepare", lambda _: None)
    monkeypatch.setattr(runtime.os, "chown", lambda *args: None)
    monkeypatch.setattr(runtime.grp, "getgrnam", lambda _: Mock(gr_gid=100))
    monkeypatch.setattr(
        runtime, "run", Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    )
    monkeypatch.setattr(service, "_active", lambda: True)
    monkeypatch.setattr(runtime.uuid, "uuid4", lambda: Mock(hex="test"))
    monkeypatch.setattr(runtime.time, "sleep", lambda _: None)
    return service


def native_reply(*args, **kwargs):
    if args[:2] == ("show", "symbols"):
        output = "mb_candidate_test constant\n"
    elif args == ("configure", "confirm"):
        output = "Reconfiguration confirmed\n"
    else:
        output = ""
    return subprocess.CompletedProcess(args, 0, output, "")


@pytest.mark.parametrize("fault", ["deadline", "no-op", "wrong-generation"])
def test_candidate_requires_timely_positive_generation_confirmation(service, monkeypatch, fault):
    service.config.write_text("previous")
    clock = [0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])

    def acceptance(_):
        if fault == "deadline":
            clock[0] = 61  # Previous generation might now look perfectly healthy.
        return True

    def control(*args, **kwargs):
        reply = native_reply(*args, **kwargs)
        if fault == "no-op" and args == ("configure", "confirm"):
            reply.stdout = "Nothing to do\n"  # Native BIRD exits zero here.
        if fault == "wrong-generation" and args[:2] == ("show", "symbols"):
            reply.stdout = "mb_candidate_old constant\n"
        return reply

    monkeypatch.setattr(service, "established", acceptance)
    monkeypatch.setattr(service, "_control", control)
    with pytest.raises(runtime.SessionsPending, match="previous configuration retained"):
        service.apply("candidate", 3100, 0)
    assert service.config.read_text() == "previous"
    assert not (service.directory / "candidate.conf").exists()


@settings(max_examples=20, deadline=None)
@given(remaining=st.sets(st.integers(1, 8), max_size=7))
def test_withdrawal_failure_cannot_restore_revoked_exports(remaining):
    """Every proper subset is restrictive, regardless of which prefix disappeared.

    A new generation must never recover the previous text after revocation, even
    if its parser, daemon start, or acceptance fails. These are real disk writes.
    """
    with TemporaryDirectory() as directory:
        service = runtime.BirdService(Path(directory))
        service.directory.mkdir(parents=True)
        service.config.write_text("revoked generation")
        all_exports = {f"192.0.2.{i}/32" for i in range(1, 9)}
        service._record_exports(all_exports)
        stops = []

        def fail(*args):
            raise runtime.SessionsPending("candidate unavailable")

        with (
            patch.object(service, "_prepare"),
            patch.object(service, "stop", side_effect=lambda: stops.append(True)),
            patch.object(service, "_apply_candidate", side_effect=fail),
            pytest.raises(runtime.SessionsPending),
        ):
            service.apply("restricted", 3100, 2, exports={f"192.0.2.{i}/32" for i in remaining})
        assert stops == [True]
        assert not service.config.exists()
        assert (
            set(json.loads((service.directory / "exports.json").read_text())["prefixes"])
            <= all_exports
        )


@pytest.mark.parametrize("error", [runtime.RuntimeErrorBase("query failed"), OSError("lost")])
def test_initial_legacy_failure_invalidates_config_before_stop(service, monkeypatch, error):
    monkeypatch.setattr(service, "established", Mock(side_effect=error))
    monkeypatch.setattr(service, "_control", native_reply)
    stopped = []

    def stop():
        assert not service.config.exists()
        stopped.append(True)

    monkeypatch.setattr(service, "stop", stop)
    with pytest.raises(type(error)):
        service.apply("initial", 3100, 2)
    assert stopped == [True]
    assert not service.config.exists()


def test_initial_failure_status_does_not_claim_a_retained_configuration(service, monkeypatch):
    monkeypatch.setattr(service, "established", lambda _: False)
    monkeypatch.setattr(service, "_control", native_reply)
    with pytest.raises(runtime.SessionsPending, match="no previous configuration retained"):
        service.apply("initial", 3100, 2)
    assert not service.config.exists()


def test_changed_configuration_restarts_a_disabled_daemon(service, monkeypatch):
    service.config.write_text("old")
    monkeypatch.setattr(service, "_active", lambda: False)
    monkeypatch.setattr(service, "established", lambda _: True)
    control = Mock(side_effect=native_reply)
    start = Mock()
    monkeypatch.setattr(service, "_control", control)
    monkeypatch.setattr(service, "_start", start)
    service.apply("new", 3100, 2)
    start.assert_called_once()
    assert all(call.args[0] != "configure" for call in control.call_args_list)
    assert service.config.read_text() == "new"


@pytest.mark.parametrize(
    "failed_write",
    ["kernel-table", "policy_journal.py", "maas-bird-policy.service", "maas-bird.service"],
)
def test_partial_preparation_is_completed_on_retry(tmp_path, monkeypatch, failed_write):
    service = runtime.BirdService(tmp_path)
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/sbin/bird")
    monkeypatch.setattr(
        runtime,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(
            args, 1 if "is-active" in args else 0, "[]", ""
        ),
    )
    original = runtime.write_atomic
    failed = []

    def write(path, *args, **kwargs):
        if path.name == failed_write and not failed:
            failed.append(True)
            raise OSError("injected file failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(runtime, "write_atomic", write)
    with pytest.raises(OSError):
        service._prepare(3100)
    service._prepare(3100)
    assert service.unit.read_text() == bird_unit(service.owner)
    assert service.policy_unit.read_text() == policy_unit(service.owner)
    assert (service.directory / "policy_journal.py").is_file()
    assert (service.directory / "kernel-table").read_text() == "3100\n"
    with pytest.raises(runtime.RuntimeErrorBase, match="kernel table"):
        service._prepare(3101)


def test_complete_empty_inventory_revokes_but_unknown_inventory_waits():
    inputs = make_inputs()
    inputs["inventory"] = Inventory(hosts=())
    with pytest.raises(AuthorizationWithdrawn):
        resolve(**inputs)
    inputs["inventory"] = Inventory()
    with pytest.raises(NotReady):
        resolve(**inputs)


def test_inventory_withdrawal_invalidates_before_stopping(charm_context, monkeypatch):
    service = Mock()
    sequence = []
    service.invalidate.side_effect = lambda: sequence.append("invalidate")
    service.stop.side_effect = lambda: sequence.append("stop")
    monkeypatch.setattr(rack_runtime, "RackService", lambda **kwargs: service)
    monkeypatch.setattr("maas_bird.discovery.host_uuid", lambda: "host-uuid")
    relation = testing.Relation(
        "inventory",
        remote_app_data=response_data(
            UnitInventory(
                unit_name="bird/0", system_uuid="host-uuid", rack="a", scope_state="conflict"
            )
        ),
    )
    ctx = charm_context("bird")
    out = ctx.run(
        ctx.on.relation_changed(relation),
        testing.State(config={"enabled": True}, relations={relation}),
    )
    assert sequence == ["invalidate", "stop"]
    assert isinstance(out.unit_status, testing.BlockedStatus)
    service.apply_plan.assert_not_called()


def test_inventory_removal_stops_even_without_successful_activation(charm_context, monkeypatch):
    service = Mock()
    monkeypatch.setattr(rack_runtime, "RackService", lambda **kwargs: service)
    relation = testing.Relation("inventory")
    ctx = charm_context("bird")
    ctx.run(
        ctx.on.relation_broken(relation),
        testing.State(config={"enabled": True}, relations={relation}),
    )
    service.invalidate.assert_called_once()
    service.stop.assert_called_once()


@pytest.mark.parametrize("endpoint", ["inventory", "attachments", "advertisements"])
def test_failed_activation_then_producer_removal_cannot_resurrect_exports(
    rack_environment, charm_context, monkeypatch, endpoint
):
    """Use the actual filesystem transaction, not a mock that skips its cleanup.

    The new lifecycle may retain/start safe control-plane sessions with zero VIP
    origins. The invariant is no unaccepted or withdrawn export, not stopping all
    unrelated domains whenever a workload producer disappears.
    """
    inputs, plan, passwords, service, host = rack_environment
    baseline = host.kernel.snapshot()
    monkeypatch.setattr("maas_bird.discovery.host_uuid", lambda: "host-uuid")
    monkeypatch.setattr("maas_bird.discovery.links", lambda: inputs["local_links"])
    monkeypatch.setattr(
        "maas_bird.discovery.command",
        lambda _: json.dumps(
            [
                {
                    "addr_info": [
                        {"local": str(p.network_address), "prefixlen": 32}
                        for c in plan.contexts
                        for p in c.sources
                    ]
                }
            ]
        ),
    )
    monkeypatch.setattr(rack_runtime, "RackService", lambda **kwargs: service)
    relations = {
        "inventory": testing.Relation(
            "inventory", remote_app_data=inventory_response(inputs["inventory"])
        ),
        "attachments": testing.Relation(
            "attachments",
            remote_units_data={n: encode(doc) for n, doc in enumerate(inputs["attachments"])},
        ),
        "advertisements": testing.Relation(
            "advertisements",
            remote_units_data={n: encode(doc) for n, doc in enumerate(inputs["advertisements"])},
        ),
    }
    secret = testing.Secret(passwords)
    ctx = charm_context("bird")
    state = testing.State(
        config=consumer_config(inputs["config"]) | {"enabled": True, "bgp-credentials": secret.id},
        relations=set(relations.values()),
        secrets={secret},
    )
    host.fail_control = ("show", "symbols")
    failed = ctx.run(ctx.on.config_changed(), state)
    assert isinstance(failed.unit_status, testing.BlockedStatus)
    # The first managed startup was never acknowledged: restore the previous
    # inactive system service rather than retain managed routing authority.
    assert service.state() is None
    assert not host.active and not service.config.exists()
    assert not service.manifest.exists()
    assert not service.marker.exists()
    host.fail_control = None
    removed = ctx.run(ctx.on.relation_broken(failed.get_relation(relations[endpoint].id)), failed)
    if endpoint == "inventory":
        assert isinstance(removed.unit_status, testing.BlockedStatus)
        assert not host.active and not service.config.exists()
    elif service.config.exists():
        assert service.state().prefixes == ()
        assert all(
            f'route {prefix} via "lo";' not in service.config.read_text()
            for prefix in plan.prefixes
        )
    else:
        assert not host.active
    assert host.kernel.snapshot() == baseline


def test_preparation_retries_systemd_reload_after_written_units(tmp_path, monkeypatch):
    service = runtime.BirdService(tmp_path)
    monkeypatch.setattr(runtime.shutil, "which", lambda _: "/usr/sbin/bird")
    reloads = []

    def run(args, **kwargs):
        if args == ["systemctl", "daemon-reload"]:
            reloads.append(True)
            if len(reloads) == 1:
                raise runtime.RuntimeErrorBase("reload unavailable")
        return subprocess.CompletedProcess(args, 1 if "is-active" in args else 0, "[]", "")

    monkeypatch.setattr(runtime, "run", run)
    with pytest.raises(runtime.RuntimeErrorBase):
        service._prepare(3100)
    assert service.unit.exists()
    service._prepare(3100)
    assert reloads == [True, True]


def test_revocation_attempts_stop_even_when_disk_invalidation_fails(service, monkeypatch):
    service.config.write_text("old")
    service._record_exports(["192.0.2.1/32"])
    monkeypatch.setattr(service, "_discard_config", Mock(side_effect=OSError("disk failed")))
    stop = Mock()
    monkeypatch.setattr(service, "stop", stop)
    with pytest.raises(OSError):
        service.apply("restricted", 3100, 2, exports=())
    stop.assert_called_once()
