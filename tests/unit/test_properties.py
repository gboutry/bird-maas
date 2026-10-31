"""Properties describe invariants; tests.strategies describes generated inputs.

Hypothesis explores inputs and shrinks failures to smaller counterexamples. Valid
domain strategies reserve disjoint resources; negative tests introduce one fault.
Journal tests use real files with a deliberately limited kernel-command model.
They complement, rather than replace, the actual forwarding namespace tests.
"""

import json
from ipaddress import IPv4Network
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from maas_bird.interfaces import decode, encode
from maas_bird.models import Advertisements, MultiDomainPlan
from maas_bird.policy import PolicyError, PolicyRouting, layout
from maas_bird.resolver import InvalidIntent, resolve

from tests.policy_model import InjectedFailure, KernelModel
from tests.strategies import advertisement_documents, fresh_domain_inputs, routing_inputs

# Four guards plus eight source/destination rules in the fixed journal fixture.
# Keep all failure tests on the same operation range when that fixture changes.
INSTALL_OPERATION_COUNT = 12


@given(document=advertisement_documents(), data=st.data())
def test_relation_canonicalization_is_idempotent_and_order_independent(document, data):
    """Wire collections are sets of records, while None and empty remain distinct."""
    bag = encode(document)
    # An unknown relation key must not even be parsed as JSON by the consumer.
    assert encode(decode(Advertisements, bag | {"future-field": "not-json"})) == bag
    if document.advertisements is None:
        assert "advertisements" not in bag
        return
    members = list(document.advertisements)
    shuffled = data.draw(st.permutations(members))
    # Duplicating records or changing order must not trigger a relation update.
    duplicated = tuple(shuffled) + tuple(members[: data.draw(st.integers(0, len(members)))])
    assert encode(Advertisements(advertisements=duplicated)) == bag
    assert (json.loads(bag["advertisements"]) == []) == (len(members) == 0)


def assert_source_isolation(policy, source, table, destinations, other_tables, protected_prefixes):
    """A VIP uses its own table even for destinations belonging to another domain.

    Covering a foreign destination is deliberate: the VIP's own table has no route
    there, so its unreachable guard blocks traffic instead of falling through to
    main. Omitting that source/destination rule would silently bypass the guard.
    """
    selected = [rule for rule in policy["rules"] if rule["src"] == str(source)]
    assert {rule["dst"] for rule in selected} == destinations
    assert {rule["table"] for rule in selected} == {table}
    assert not {rule["table"] for rule in selected} & other_tables
    for protected in protected_prefixes:
        assert all(
            not IPv4Network(rule["dst"]).overlaps(IPv4Network(protected)) for rule in selected
        )


def permute_domain_inputs(inputs, data):
    """Vary presentation order, preserving every configured resource and binding."""
    domains = json.loads(inputs["config"]["routing-domains"])
    for domain in domains:
        domain["import_prefixes"] = list(data.draw(st.permutations(domain["import_prefixes"])))
        for context in domain["contexts"]:
            context["sources"] = list(data.draw(st.permutations(context["sources"])))
        domain["contexts"] = list(data.draw(st.permutations(domain["contexts"])))
    inputs["config"]["routing-domains"] = json.dumps(data.draw(st.permutations(domains)))


@settings(max_examples=60)
@given(inputs=routing_inputs(), data=st.data())
def test_generated_domain_policy_preserves_ownership_and_isolation(inputs, data):
    """Check the source-to-table matrix, not merely that generated intent parses."""
    plan = resolve(**inputs)
    assert isinstance(plan, MultiDomainPlan)
    policy = layout(plan)
    destinations = {str(prefix) for domain in plan.domains for prefix in domain.imports}
    assert len({rule["priority"] for rule in policy["rules"]}) == len(policy["rules"])
    assert len(set(policy["tables"])) == len(plan.contexts)
    for domain in plan.domains:
        other_tables = {
            c.table for other in plan.domains if other != domain for c in other.contexts
        }
        for context in domain.contexts:
            for source in context.sources:
                assert_source_isolation(
                    policy,
                    source,
                    context.table,
                    destinations,
                    other_tables,
                    inputs["config"]["protected-prefixes"].split(","),
                )
    # Reordering operator intent must not look like a change of owned resources.
    permute_domain_inputs(inputs, data)
    assert layout(resolve(**inputs)) == policy


@settings(max_examples=40)
@given(
    inputs=routing_inputs(),
    collision=st.sampled_from(["table", "priority", "destination", "source"]),
)
def test_generated_cross_domain_collisions_are_rejected(inputs, collision):
    # Start from valid intent and introduce exactly one cross-domain collision.
    domains = json.loads(inputs["config"]["routing-domains"])
    first, second = domains
    if collision == "table":
        second["contexts"][0]["table"] = first["contexts"][0]["table"]
    elif collision == "priority":
        second["contexts"][0]["rule_priority"] = first["contexts"][0]["rule_priority"]
    elif collision == "destination":
        second["import_prefixes"] = first["import_prefixes"][:1]
    else:
        second["contexts"][0]["sources"] = first["contexts"][0]["sources"][:1]
    inputs["config"]["routing-domains"] = json.dumps(domains)
    with pytest.raises(InvalidIntent):
        resolve(**inputs)


# The fixed fixture has four guards plus eight rules: each of the 12 additions can
# fail. Both symbolic and numeric iproute2 JSON forms exercise the same journal.
# Filesystem scheduling is not a performance assertion; journals use real fsyncs.
@settings(max_examples=30, deadline=None)
@given(fail_at=st.integers(1, INSTALL_OPERATION_COUNT), numeric=st.booleans())
def test_every_acknowledged_install_failure_restores_the_kernel_model(fail_at, numeric):
    plan = resolve(**fresh_domain_inputs())
    kernel = KernelModel(numeric)
    baseline = kernel.snapshot()
    kernel.fail_at = fail_at
    with TemporaryDirectory() as directory:
        policy = PolicyRouting(Path(directory), "test-owner", kernel.execute)
        with pytest.raises(InjectedFailure):
            policy.ensure(plan)
        assert kernel.snapshot() == baseline
        assert not policy.path.exists()
        # A known failure must leave a clean retry, not just an empty model.
        kernel.fail_at = None
        policy.ensure(plan)
        installed = kernel.snapshot()
        policy.ensure(plan)
        assert kernel.snapshot() == installed
        policy.remove()
        policy.remove()
        assert kernel.snapshot() == baseline


@settings(max_examples=30, deadline=None)
@given(fail_at=st.integers(1, INSTALL_OPERATION_COUNT), numeric=st.booleans())
def test_uncertain_operation_is_never_adopted_or_removed(fail_at, numeric):
    plan = resolve(**fresh_domain_inputs())
    kernel = KernelModel(numeric)
    kernel.fail_at, kernel.lose_ack = fail_at, True
    with TemporaryDirectory() as directory:
        policy = PolicyRouting(Path(directory), "test-owner", kernel.execute)
        with pytest.raises(InjectedFailure):
            policy.ensure(plan)
        # The model applied the mutation but the caller did not receive its ack.
        # Neither retry nor cleanup may infer ownership from that uncertain state.
        uncertain = kernel.snapshot()
        journal = policy.path.read_bytes()
        with pytest.raises(PolicyError):
            policy.ensure(plan)
        with pytest.raises(PolicyError):
            policy.remove()
        assert kernel.snapshot() == uncertain
        assert policy.path.read_bytes() == journal
        stranger = PolicyRouting(Path(directory), "different-owner", kernel.execute)
        with pytest.raises(PolicyError):
            stranger.remove()
        assert kernel.snapshot() == uncertain


# Here an example is a sequence of up to 20 commands, not a single configuration.
# Hypothesis can shrink both the sequence and a failing operation's position.
@settings(max_examples=20, stateful_step_count=20, deadline=None)
class PolicyLifecycle(RuleBasedStateMachine):
    """Generate operation sequences, checking disk/model agreement after every step.

    ``active`` is an independent oracle, not a value read from the production
    journal. Foreign objects are part of the baseline and must survive all rules.
    """

    def __init__(self):
        super().__init__()
        self.directory = TemporaryDirectory()
        self.plan = resolve(**fresh_domain_inputs())
        self.kernel = KernelModel()
        self.baseline = self.kernel.snapshot()
        self.policy = PolicyRouting(Path(self.directory.name), "test-owner", self.kernel.execute)
        self.active = False

    @rule()
    def ensure(self):
        """Installation and repeated reconciliation must be idempotent."""
        self.policy.ensure(self.plan)
        self.active = True

    @rule()
    def remove(self):
        """Removal is also valid when no policy has been installed."""
        self.policy.remove()
        self.active = False

    @precondition(lambda self: not self.active)
    @rule(fail_at=st.integers(1, INSTALL_OPERATION_COUNT))
    def failed_install(self, fail_at):
        """Only an inactive model can exercise a fresh installation failure."""
        self.kernel.mutations, self.kernel.fail_at = 0, fail_at
        with pytest.raises(InjectedFailure):
            self.policy.ensure(self.plan)
        self.kernel.fail_at = None

    @invariant()
    def owned_state_is_exact_and_foreign_state_is_preserved(self):
        """No partial policy may survive a known failure or successful removal."""
        if not self.active:
            assert self.kernel.snapshot() == self.baseline
            assert not self.policy.path.exists()
            return
        expected = layout(self.plan)
        assert self.kernel.rules == self.baseline[0] + expected["rules"]
        assert self.kernel.routes == self.baseline[1] + [
            {
                "type": "unreachable",
                "dst": "default",
                "table": table,
                "protocol": 196,
                "metric": 42760,
            }
            for table in expected["tables"]
        ]
        assert json.loads(self.policy.path.read_text())["phase"] == "active"

    def teardown(self):
        self.directory.cleanup()


TestPolicyLifecycle = PolicyLifecycle.TestCase
