"""Native local delivery satisfies an exact self-import without a learned route.

BIRD control replies are doubled here; loopback, RPDB and route reads are real.
"""

import json
import os
import subprocess

from maas_bird.rack_runtime import RackService
from maas_bird.resolver import resolve_rack

from tests.factories import ten_vf_inputs


def test_native_local_origin_health_without_a_self_bgp_route(monkeypatch, tmp_path):
    assert os.environ.get("MAAS_BIRD_ISOLATED_TESTS") == "1"

    def native(*args):
        return subprocess.run(args, capture_output=True, text=True, check=True)

    original = resolve_rack(**ten_vf_inputs())
    domain = original.domains[0]
    context = domain.contexts[0]
    own = context.sources[0]
    own_ip = str(own.network_address)
    path = domain.sessions[0].interface
    plan = original.model_copy(
        update={
            "domains": (domain.model_copy(update={"contexts": (context,), "imports": (own,)}),)
        }
    )
    lo = json.loads(native("ip", "-j", "link", "show", "lo").stdout)[0]
    native("ip", "link", "set", "lo", "up")
    native("ip", "link", "add", path, "type", "dummy")
    try:
        native("ip", "-4", "address", "add", str(own), "dev", "lo")
        native(
            "ip",
            "-4",
            "route",
            "add",
            "unreachable",
            "default",
            "table",
            str(context.table),
            "proto",
            "4",
            "metric",
            "42760",
        )
        native(
            "ip",
            "-4",
            "rule",
            "add",
            "from",
            str(own),
            "table",
            str(context.table),
            "priority",
            str(context.rule_priority),
            "protocol",
            "4",
        )
        try:
            selected = json.loads(
                native("ip", "-4", "-j", "route", "get", own_ip, "from", own_ip).stdout
            )
            assert selected[0]["dev"] == "lo"
            assert selected[0]["type"] == "local"
            routes = json.loads(
                native("ip", "-4", "-j", "route", "show", "table", str(context.table)).stdout
            )
            assert not any(route.get("dst") == str(own) for route in routes)
            service = RackService(tmp_path)
            monkeypatch.setattr(
                service, "_control", lambda *a, **kw: subprocess.CompletedProcess(a, 0, "", "")
            )
            result = service.health(plan)
            assert result["missing_routes"] == 0
            assert result["fallback_routes"] == 0
            assert result["established"] == 0  # This is not a BGP-session test.
        finally:
            native(
                "ip",
                "-4",
                "rule",
                "del",
                "from",
                str(own),
                "table",
                str(context.table),
                "priority",
                str(context.rule_priority),
                "protocol",
                "4",
            )
            native(
                "ip",
                "-4",
                "route",
                "del",
                "unreachable",
                "default",
                "table",
                str(context.table),
                "proto",
                "4",
                "metric",
                "42760",
            )
            native("ip", "-4", "address", "del", str(own), "dev", "lo")
    finally:
        native("ip", "link", "del", path)
        if "UP" not in lo["flags"]:
            native("ip", "link", "set", "lo", "down")
