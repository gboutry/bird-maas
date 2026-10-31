"""Hypothesis input construction, kept separate from the properties being checked.

Relation documents may be incomplete: parsing is intentionally more permissive than
routing activation. Routing inputs, by contrast, are complete and valid by design;
negative tests mutate one part of that valid intent to identify the failing invariant.
Every example starts with fresh builders, never a shared function-scoped pytest fixture.
"""

import json
from ipaddress import IPv4Network

from hypothesis import strategies as st
from maas_bird.models import Advertisement, Advertisements

from tests.factories import make_inputs, with_numa_contexts, with_routing_domains


def fresh_domain_inputs():
    """Return the ordinary two-domain, two-NUMA fixture with no shared mutable state."""
    return with_routing_domains(with_numa_contexts(make_inputs()))


@st.composite
def advertisement_documents(draw):
    """Generate unknown snapshots as well as complete snapshots, including empty ones."""
    if draw(st.booleans()):
        return Advertisements()
    advertisements = draw(st.lists(advertisement_records(), max_size=12))
    return Advertisements(advertisements=tuple(advertisements))


def advertisement_records():
    """Exercise wire parsing independently of operational completeness checks."""
    return st.builds(
        Advertisement,
        machine_id=st.one_of(st.none(), st.text(max_size=20)),
        domain=st.one_of(st.none(), st.text(max_size=20)),
        context=st.one_of(st.none(), st.text(max_size=20)),
        prefix=st.ip_addresses(v=4).map(lambda ip: IPv4Network(f"{ip}/32")),
        ready=st.one_of(st.none(), st.booleans()),
    )


@st.composite
def routing_inputs(draw):
    """Vary valid addresses, tables, priorities, source counts and NUMA preferences."""
    inputs = fresh_domain_inputs()
    domains = json.loads(inputs["config"]["routing-domains"])
    # Four unique tables model two contexts per domain. Leave headroom below the
    # 32000 priority limit for the second domain, its contexts, and expanded rules.
    tables = draw(st.lists(st.integers(1000, 60000), min_size=4, max_size=4, unique=True))
    base = draw(st.integers(1000, 27000))
    bindings = {}
    for index, domain in enumerate(domains):
        bindings.update(configure_domain(draw, domain, index, tables, base))
    bind_advertisements(draw, inputs, bindings)
    inputs["config"]["routing-domains"] = json.dumps(domains)
    return inputs


def configure_domain(draw, domain, index, tables, base):
    """Allocate disjoint address ranges while retaining realistic per-domain sharing."""
    count = draw(st.integers(1, 3))
    # Different second/third octets keep domains apart; contexts in one domain
    # intentionally share an export allowlist, just as two service VIPs would.
    domain["import_prefixes"] = [f"10.{30 + index}.{n}.0/24" for n in range(count)]
    domain["export_allowlist"] = [f"10.60.{index}.0/24"]
    swap_numa_nodes = draw(st.booleans())
    bindings = {}
    for node, context in enumerate(domain["contexts"]):
        sources = configure_context(draw, context, index, node, tables, base, swap_numa_nodes)
        bindings[(domain["id"], context["id"])] = sources[0]
    return bindings


def configure_context(draw, context, domain_index, node, tables, base, swap_numa_nodes):
    """Reserve enough priority space for every source × managed-destination rule."""
    source_count = draw(st.integers(1, 3))
    sources = [f"10.60.{domain_index}.{10 + node * 10 + n}/32" for n in range(source_count)]
    # A context needs at most 3 sources × 6 destinations = 18 priorities here.
    # Gaps of 100 per context and 1000 per domain keep generated intent valid.
    context.update(
        table=tables[domain_index * 2 + node],
        rule_priority=base + domain_index * 1000 + node * 100,
        sources=sources,
        numa_node=1 - node if swap_numa_nodes else node,
    )
    return sources


def bind_advertisements(draw, inputs, bindings):
    """Advertise a reserved source per context, independently varying readiness."""
    ads = inputs["advertisements"][0].model_dump(mode="json")
    for advertisement in ads["advertisements"]:
        advertisement["prefix"] = bindings[(advertisement["domain"], advertisement["context"])]
        advertisement["ready"] = draw(st.booleans())
    inputs["advertisements"] = [Advertisements.model_validate(ads)]
