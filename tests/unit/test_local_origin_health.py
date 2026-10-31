"""Exact local node origins must not require a route learned back from BGP."""

import json
import subprocess
from ipaddress import IPv4Network

import pytest
from maas_bird import rack_runtime
from maas_bird.resolver import resolve_rack

from tests.factories import ten_vf_inputs


@pytest.mark.parametrize("address_prefix,missing", [(32, 0), (24, 1), (None, 1)])
def test_only_native_exact_context_source_satisfies_local_import(
    monkeypatch, tmp_path, address_prefix, missing
):
    original = resolve_rack(**ten_vf_inputs())
    domain = original.domains[0]
    context = domain.contexts[0]
    own = context.sources[0]
    remote = IPv4Network("198.18.248.1/32")
    domain = domain.model_copy(update={"contexts": (context,), "imports": (own, remote)})
    plan = original.model_copy(update={"domains": (domain,)})
    service = rack_runtime.RackService(tmp_path)
    monkeypatch.setattr(
        service, "_control", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "", "")
    )

    def native(args):
        if args == ["ip", "-4", "-j", "address", "show", "dev", "lo"]:
            addresses = (
                [{"local": str(own.network_address), "prefixlen": address_prefix}]
                if address_prefix is not None
                else []
            )
            data = [{"ifname": "lo", "addr_info": addresses}]
        else:
            assert args == ["ip", "-4", "-j", "-N", "route", "show", "table", str(context.table)]
            path = next(
                s
                for s in domain.sessions
                if context.numa_node is None or s.numa_node == context.numa_node
            )
            data = [{"dst": str(remote), "dev": path.interface}]
        return subprocess.CompletedProcess(args, 0, json.dumps(data), "")

    monkeypatch.setattr(rack_runtime, "run", native)
    result = service.health(plan)
    assert result["missing_routes"] == missing
    assert result["fallback_routes"] == 0


def test_local_origin_does_not_exempt_an_import_aggregate(monkeypatch, tmp_path):
    original = resolve_rack(**ten_vf_inputs())
    domain = original.domains[0]
    context = domain.contexts[0]
    aggregate = context.sources[0].supernet(new_prefix=24)
    domain = domain.model_copy(update={"contexts": (context,), "imports": (aggregate,)})
    plan = original.model_copy(update={"domains": (domain,)})
    service = rack_runtime.RackService(tmp_path)
    monkeypatch.setattr(
        service, "_control", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "", "")
    )

    def native(args):
        assert args == ["ip", "-4", "-j", "-N", "route", "show", "table", str(context.table)]
        return subprocess.CompletedProcess(args, 0, "[]", "")

    monkeypatch.setattr(rack_runtime, "run", native)
    assert service.health(plan)["missing_routes"] == 1
