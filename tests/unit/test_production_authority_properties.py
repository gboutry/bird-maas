"""Generated restrictions against the supported production authority compiler."""

from hypothesis import given
from hypothesis import strategies as st
from maas_bird import authority
from maas_bird.resolver import resolve_rack

from tests.factories import rack_inputs


@given(
    selected=st.integers(min_value=0, max_value=1),
    ready=st.tuples(st.booleans(), st.booleans()),
    reverse_snapshots=st.booleans(),
)
def test_export_withdrawal_is_exact_domain_local_idempotent_and_irreversible(
    selected, ready, reverse_snapshots
):
    # Build a fresh independent plan for every generated example. Reusing a
    # mutable pytest fixture here could let a prior withdrawal affect the oracle.
    inputs = rack_inputs()
    previous = resolve_rack(**inputs)
    chosen = previous.domains[selected]
    other = previous.domains[1 - selected]
    prefixes = sorted(chosen.prefixes)
    assert len(prefixes) == len(ready) == 2
    expected = {prefix for prefix, permitted in zip(prefixes, ready, strict=True) if permitted}
    snapshots = [
        document.model_copy(
            update={
                "advertisements": tuple(
                    advertisement.model_copy(update={"ready": advertisement.prefix in expected})
                    for advertisement in document.advertisements
                )
            }
        )
        if document.domain == chosen.domain
        else document
        for document in inputs["advertisements"]
    ]
    if reverse_snapshots:
        snapshots.reverse()
    passwords = {session.id: "test-only-password" for session in previous.sessions}

    def restrict(plan, documents):
        return authority.restrict(
            plan,
            inputs["config"],
            inputs["inventory"],
            documents,
            passwords,
            inputs["attachments"],
            local_links=inputs["local_links"],
        )

    restricted = restrict(previous, snapshots)
    result = restricted.domains[selected]
    # Exact expected exports plus an unchanged other domain reject a broken
    # implementation that simply empties every domain to satisfy subset checks.
    assert set(result.prefixes) == expected
    assert restricted.domains[1 - selected] == other
    assert result.imports == chosen.imports
    assert result.sessions == chosen.sessions
    for before, after in zip(chosen.contexts, result.contexts, strict=True):
        assert set(after.exports) == set(before.exports) & expected
        assert after.model_copy(update={"exports": before.exports}) == before
    assert restrict(restricted, snapshots) == restricted
    # A later broad "ready" report is a proposed grant. The restrictive pass
    # cannot resurrect already withdrawn authority from its older accepted plan.
    assert restrict(restricted, inputs["advertisements"]) == restricted
