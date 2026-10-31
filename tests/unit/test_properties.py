"""Generated invariants complement the native namespace examples."""

import json
from ipaddress import IPv4Network
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from maas_bird.interfaces import decode, encode
from maas_bird.models import Advertisement, Advertisements, MultiDomainPlan
from maas_bird.policy import PolicyError, PolicyRouting, layout
from maas_bird.resolver import InvalidIntent, resolve

from tests.factories import make_inputs, with_numa_contexts, with_routing_domains
from tests.policy_model import InjectedFailure, KernelModel


def fresh_domain_inputs():
    return with_routing_domains(with_numa_contexts(make_inputs()))


@st.composite
def advertisement_documents(draw):
    if draw(st.booleans()):
        return Advertisements()
    advertisements = draw(
        st.lists(
            st.builds(
                Advertisement,
                machine_id=st.one_of(st.none(), st.text(max_size=20)),
                domain=st.one_of(st.none(), st.text(max_size=20)),
                context=st.one_of(st.none(), st.text(max_size=20)),
                prefix=st.ip_addresses(v=4).map(lambda ip: IPv4Network(f"{ip}/32")),
                ready=st.one_of(st.none(), st.booleans()),
            ),
            max_size=12,
        )
    )
    return Advertisements(advertisements=tuple(advertisements))


@given(document=advertisement_documents(), data=st.data())
def test_relation_canonicalization_is_idempotent_and_order_independent(document, data):
    bag = encode(document)
    assert encode(decode(Advertisements, bag | {"future-field": "not-json"})) == bag
    if document.advertisements is None:
        assert "advertisements" not in bag
        return
    members = list(document.advertisements)
    shuffled = data.draw(st.permutations(members))
    duplicated = tuple(shuffled) + tuple(members[: data.draw(st.integers(0, len(members)))])
    assert encode(Advertisements(advertisements=duplicated)) == bag
    assert (json.loads(bag["advertisements"]) == []) == (len(members) == 0)


@st.composite
def routing_inputs(draw):
    inputs = fresh_domain_inputs()
    domains = json.loads(inputs["config"]["routing-domains"])
    tables = draw(st.lists(st.integers(1000, 60000), min_size=4, max_size=4, unique=True))
    base = draw(st.integers(1000, 27000))
    bindings = {}
    for index, domain in enumerate(domains):
        count = draw(st.integers(1, 3))
        domain["import_prefixes"] = [f"10.{30 + index}.{n}.0/24" for n in range(count)]
        domain["export_allowlist"] = [f"10.60.{index}.0/24"]
        swap = draw(st.booleans())
        for node, context in enumerate(domain["contexts"]):
            source_count = draw(st.integers(1, 3))
            sources = [f"10.60.{index}.{10 + node * 10 + n}/32" for n in range(source_count)]
            context.update(
                table=tables[index * 2 + node],
                rule_priority=base + index * 1000 + node * 100,
                sources=sources,
                numa_node=node ^ swap,
            )
            bindings[(domain["id"], context["id"])] = sources[0]
    ads = inputs["advertisements"][0].model_dump(mode="json")
    for advertisement in ads["advertisements"]:
        advertisement["prefix"] = bindings[(advertisement["domain"], advertisement["context"])]
        advertisement["ready"] = draw(st.booleans())
    inputs["advertisements"] = [Advertisements.model_validate(ads)]
    inputs["config"]["routing-domains"] = json.dumps(domains)
    return inputs


@settings(max_examples=60)
@given(inputs=routing_inputs(), data=st.data())
def test_generated_domain_policy_preserves_ownership_and_isolation(inputs, data):
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
                selected = [rule for rule in policy["rules"] if rule["src"] == str(source)]
                assert {rule["dst"] for rule in selected} == destinations
                assert {rule["table"] for rule in selected} == {context.table}
                assert not {rule["table"] for rule in selected} & other_tables
                for protected in inputs["config"]["protected-prefixes"].split(","):
                    assert all(
                        not IPv4Network(rule["dst"]).overlaps(IPv4Network(protected))
                        for rule in selected
                    )
    domains = json.loads(inputs["config"]["routing-domains"])
    for domain in domains:
        domain["import_prefixes"] = list(data.draw(st.permutations(domain["import_prefixes"])))
        for context in domain["contexts"]:
            context["sources"] = list(data.draw(st.permutations(context["sources"])))
        domain["contexts"] = list(data.draw(st.permutations(domain["contexts"])))
    inputs["config"]["routing-domains"] = json.dumps(data.draw(st.permutations(domains)))
    assert layout(resolve(**inputs)) == policy


@settings(max_examples=40)
@given(
    inputs=routing_inputs(),
    collision=st.sampled_from(["table", "priority", "destination", "source"]),
)
def test_generated_cross_domain_collisions_are_rejected(inputs, collision):
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


# Filesystem scheduling is not a performance assertion; journals use real fsyncs.
@settings(max_examples=30, deadline=None)
@given(fail_at=st.integers(1, 12), numeric=st.booleans())
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
        kernel.fail_at = None
        policy.ensure(plan)
        installed = kernel.snapshot()
        policy.ensure(plan)
        assert kernel.snapshot() == installed
        policy.remove()
        policy.remove()
        assert kernel.snapshot() == baseline


@settings(max_examples=30, deadline=None)
@given(fail_at=st.integers(1, 12), numeric=st.booleans())
def test_uncertain_operation_is_never_adopted_or_removed(fail_at, numeric):
    plan = resolve(**fresh_domain_inputs())
    kernel = KernelModel(numeric)
    kernel.fail_at, kernel.lose_ack = fail_at, True
    with TemporaryDirectory() as directory:
        policy = PolicyRouting(Path(directory), "test-owner", kernel.execute)
        with pytest.raises(InjectedFailure):
            policy.ensure(plan)
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


@settings(max_examples=20, stateful_step_count=20, deadline=None)
class PolicyLifecycle(RuleBasedStateMachine):
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
        self.policy.ensure(self.plan)
        self.active = True

    @rule()
    def remove(self):
        self.policy.remove()
        self.active = False

    @precondition(lambda self: not self.active)
    @rule(fail_at=st.integers(1, 12))
    def failed_install(self, fail_at):
        self.kernel.mutations, self.kernel.fail_at = 0, fail_at
        with pytest.raises(InjectedFailure):
            self.policy.ensure(self.plan)
        self.kernel.fail_at = None

    @invariant()
    def owned_state_is_exact_and_foreign_state_is_preserved(self):
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
