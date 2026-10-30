"""Pure BIRD 2 renderer; no hardware discovery and no host mutations."""

import re

from maas_bird.models import Plan


def quoted(value: str) -> str:
    if any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise ValueError("BIRD strings must contain printable ASCII only")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(plan: Plan, passwords: dict[str, str]) -> str:
    imports = ", ".join(str(p) for p in plan.imports)
    exports = ", ".join(str(p) for p in plan.prefixes)
    allow_import = f"if net ~ [ {imports} ] then accept;" if imports else ""
    allow_export = (
        f'if (proto = "local_service") && (net ~ [ {exports} ]) then accept;' if exports else ""
    )
    local = f"if net ~ [ {exports} ] then accept;" if exports else ""
    interfaces = ", ".join(quoted(s.interface) for s in plan.sessions)
    text = f"""# Managed by maas-bird. Routing policy rules are owned by the host network manager.
router id {plan.host.router_id};
ipv4 table fabric4;
protocol device devices {{ scan time 5; }}
protocol direct local_service {{
    ipv4 {{ table fabric4; import filter {{ {local} reject; }}; }};
    interface "lo";
}}
filter fabric_import {{ {allow_import} reject; }}
filter host_export {{ {allow_export} reject; }}
protocol kernel fabric_kernel {{
    kernel table {plan.kernel_table};
    ipv4 {{ table fabric4; import none;
        export filter {{ if source = RTS_BGP then accept; reject; }};
    }};
}}
protocol bfd fabric_bfd {{
    interface {interfaces} {{
        min tx interval {plan.bfd_tx} ms;
        min rx interval {plan.bfd_rx} ms;
        multiplier {plan.bfd_multiplier};
    }};
}}
template bgp host_uplink {{
    local as {plan.host.local_asn};
    direct;
    strict bind yes;
    check link yes;
    bfd yes;
    hold time {plan.hold_time};
    keepalive time {plan.keepalive_time};
    graceful restart off;
    ipv4 {{
        table fabric4;
        extended next hop on;
        import filter fabric_import;
        import limit 64 action disable;
        export filter host_export;
        export limit {max(1, len(plan.prefixes))} action disable;
    }};
}}
"""
    for n, session in enumerate(plan.sessions):
        if session.id not in passwords or not passwords[session.id]:
            raise ValueError("Missing session authentication secret")
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,15}", session.interface):
            raise ValueError("Unsafe interface name")
        text += f"""protocol bgp uplink_{n} from host_uplink {{
    interface {quoted(session.interface)};
    source address {session.local};
    neighbor {session.peer}%{session.interface} as {session.remote_asn};
    password {quoted(passwords[session.id])};
}}
"""
    return text
