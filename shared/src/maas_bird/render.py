"""Pure BIRD 2 renderer; no hardware discovery and no host mutations."""

import re

from maas_bird.models import MultiDomainPlan, Plan


def quoted(value: str) -> str:
    if any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise ValueError("BIRD strings must contain printable ASCII only")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(plan: Plan | MultiDomainPlan, passwords: dict[str, str]) -> str:
    if isinstance(plan, MultiDomainPlan):
        return render_domains(plan, passwords)
    if plan.contexts:
        return render_contexts(plan, passwords)
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


def render_contexts(
    plan: Plan,
    passwords: dict[str, str],
    namespace: str = "",
    offset: int = 0,
    include_common: bool = True,
) -> str:
    """Retain every adjacency's routes before making independent context decisions."""
    for session in plan.sessions:
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,15}", session.interface):
            raise ValueError("Unsafe interface name")
    exports = ", ".join(str(p) for p in plan.prefixes)
    imports = ", ".join(str(p) for p in plan.imports)
    local_filter = f"if net ~ [ {exports} ] then accept;" if exports else ""
    common = f"router id {plan.host.router_id};\nprotocol device devices {{ scan time 1; }};\n"
    if include_common:
        common += bfd_config((plan,))
    text = (
        (common if include_common else "")
        + f"""ipv4 table {namespace}origins4;
protocol direct {namespace}local_service {{
    interface "lo";
    ipv4 {{ table {namespace}origins4; import filter {{ {local_filter} reject; }}; }};
}}
"""
    )
    for c, context in enumerate(plan.contexts):
        text += f"""ipv4 table {namespace}context_{c};
protocol kernel {namespace}kernel_{c} {{
    kernel table {context.table};
    ipv4 {{ table {namespace}context_{c}; import none;
        export filter {{ if source = RTS_BGP then accept; reject; }};
    }};
}}
"""
    for n, session in enumerate(plan.sessions, start=offset):
        if session.id not in passwords or not passwords[session.id]:
            raise ValueError("Missing session authentication secret")
        text += f"""ipv4 table {namespace}path_{n};
protocol pipe {namespace}originate_{n} {{
    table {namespace}origins4;
    peer table {namespace}path_{n};
    import none;
    export filter {{ if proto = "{namespace}local_service" then accept; reject; }};
}}
protocol bgp uplink_{n} {{
    local {session.local} as {plan.host.local_asn};
    neighbor {session.peer}%{session.interface} as {session.remote_asn};
    interface {quoted(session.interface)};
    password {quoted(passwords[session.id])};
    direct;
    strict bind yes;
    check link yes;
    bfd yes;
    connect retry time 2;
    error wait time 1, 5;
    hold time {plan.hold_time};
    keepalive time {plan.keepalive_time};
    graceful restart off;
    ipv4 {{ table {namespace}path_{n}; extended next hop on;
        import filter {{ if net ~ [ {imports} ] then accept; reject; }};
        import limit 64 action disable;
        export filter {{ if proto = "{namespace}local_service" then accept; reject; }};
        export limit {max(1, len(plan.prefixes))} action disable;
    }};
}}
"""
        for c, context in enumerate(plan.contexts):
            preference = 200 if session.numa_node == context.numa_node else 100
            text += f"""protocol pipe {namespace}path_{n}_context_{c} {{
    table {namespace}path_{n};
    peer table {namespace}context_{c};
    import none;
    export filter {{
        if proto = "uplink_{n}" && source = RTS_BGP then {{
            preference = {preference}; accept;
        }}
        reject;
    }};
}}
"""
    return text


def bfd_config(domains: tuple[Plan, ...]) -> str:
    # A BFD instance accepts sessions outside its interface patterns too. Use one
    # instance with explicit per-domain interface timers, not competing instances.
    text = "protocol bfd fabric_bfd {\n"
    for domain in domains:
        interfaces = ", ".join(quoted(s.interface) for s in domain.sessions)
        text += f"""    interface {interfaces} {{
        min tx interval {domain.bfd_tx} ms;
        min rx interval {domain.bfd_rx} ms;
        multiplier {domain.bfd_multiplier};
    }};
"""
    return text + "}\n"


def render_domains(plan: MultiDomainPlan, passwords: dict[str, str]) -> str:
    text = f"router id {plan.host.router_id};\nprotocol device devices {{ scan time 1; }};\n"
    text += bfd_config(plan.domains)
    offset = 0
    for index, domain in enumerate(plan.domains):
        text += render_contexts(
            domain, passwords, namespace=f"d{index}_", offset=offset, include_common=False
        )
        offset += len(domain.sessions)
    return text
