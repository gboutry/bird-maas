"""Pure BIRD 2.14 renderer for the sole supported rack/domain routing model."""

import hashlib
import re

from maas_bird.models import MultiDomainPlan, Plan, Session, sessions_for_context


def quoted(value: str) -> str:
    if any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise ValueError("BIRD strings must contain printable ASCII only")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _tag(identity: str) -> str:
    return hashlib.sha256(identity.encode()).hexdigest()[:12]


def protocol_name(domain: str, session: str) -> str:
    return f"d_{_tag(domain)}_uplink_{_tag(session)}"


def render_rack(plan: MultiDomainPlan, passwords: dict[str, str]) -> str:
    domain_tags = [_tag(domain.domain) for domain in plan.domains]
    if len(set(domain_tags)) != len(domain_tags):
        raise ValueError("Routing domain symbol collision")
    text = f"router id {plan.host.router_id};\nprotocol device devices {{ scan time 1; }};\n"
    text += _link_local_discovery(plan)
    # One BFD instance avoids competing wildcard receivers. Interface-specific
    # timers preserve independent public/replication settings within that instance.
    text += "protocol bfd fabric_bfd {\n"
    for domain in plan.domains:
        for identities in ([s.id for s in domain.sessions], [c.id for c in domain.contexts]):
            tags = [_tag(identity) for identity in identities]
            if len(set(tags)) != len(tags):
                raise ValueError("Routing symbol collision")
        if domain.sessions:
            interfaces = ", ".join(quoted(s.interface) for s in domain.sessions)
            text += f""" interface {interfaces} {{
  min tx interval {domain.bfd_tx} ms; min rx interval {domain.bfd_rx} ms;
  multiplier {domain.bfd_multiplier};
 }};
"""
    text += "}\n"
    for domain in plan.domains:
        text += _domain(domain, passwords)
    return text


def _link_local_discovery(plan: MultiDomainPlan) -> str:
    """Let interface-based unnumbered peers discover our link-local addresses.

    These are discovery messages, not authority to configure clients: no default
    router, SLAAC prefixes, DNS data or learned routes are advertised. Never use
    wildcard interface patterns, including when every session is withdrawn.
    """
    interfaces = sorted({session.interface for session in plan.sessions})
    if not interfaces:
        return ""
    text = "protocol radv fabric_discovery {\n propagate routes no;\n"
    for interface in interfaces:
        text += f""" interface {quoted(interface)} {{
  min ra interval 3; max ra interval 5;
  default lifetime 0; current hop limit 0; link mtu 0;
  reachable time 0; retrans timer 0;
  managed no; other config no;
  rdnss local yes; dnssl local yes;
  prefix ::/0 {{ skip yes; }};
 }};
"""
    return text + "}\n"


def _domain(domain: Plan, passwords: dict[str, str]) -> str:
    namespace = f"d_{_tag(domain.domain)}_"
    text = f"ipv4 table {namespace}origins4;\n"
    # A route has its own stable origin protocol. Withdrawing a VIP removes only
    # that protocol; neither peer filters nor their limits change during VIP churn.
    for prefix in domain.prefixes:
        address = str(prefix.network_address).replace(".", "_")
        text += f"""protocol static {namespace}origin_{address} {{
 ipv4 {{ table {namespace}origins4; }};
 route {prefix} via "lo";
}}
"""
    for context in domain.contexts:
        tag = _tag(context.id)
        text += f"""ipv4 table {namespace}context_{tag};
protocol kernel {namespace}kernel_{tag} {{
 kernel table {context.table};
 ipv4 {{ table {namespace}context_{tag}; import none;
  export filter {{ if source = RTS_BGP then accept; reject; }};
 }};
}}
"""
    for session in domain.sessions:
        if not bool(passwords.get(session.id)):
            raise ValueError("Missing session authentication secret")
        text += _session(domain, session, passwords[session.id], namespace)
    return text


def _session(domain: Plan, session: Session, password: str, namespace: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,15}", session.interface):
        raise ValueError("Unsafe interface name")
    tag = _tag(session.id)
    bgp = protocol_name(domain.domain, session.id)
    imports = ", ".join(map(str, domain.imports))
    export_limit = max(1, sum(len(c.sources) for c in domain.contexts))
    text = f"""ipv4 table {namespace}path_{tag};
protocol pipe {namespace}originate_{tag} {{
 table {namespace}origins4; peer table {namespace}path_{tag}; import none;
 export filter {{ if proto ~ "{namespace}origin_*" then accept; reject; }};
}}
protocol bgp {bgp} {{
 local {session.local} as {domain.host.local_asn};
 neighbor {session.peer}%{session.interface} as {session.remote_asn};
 interface {quoted(session.interface)}; password {quoted(password)};
 direct; strict bind yes; check link yes; bfd yes;
 connect retry time 2; error wait time 1, 5;
 hold time {domain.hold_time}; keepalive time {domain.keepalive_time};
 graceful restart off;
 ipv4 {{ table {namespace}path_{tag}; extended next hop on;
  import filter {{ if net ~ [ {imports} ] then accept; reject; }};
  import limit {max(1, len(domain.imports))} action disable;
  export filter {{ if proto ~ "{namespace}origin_*" then {{
"""
    contexts = [c for c in domain.contexts if session in sessions_for_context(domain, c)]
    for context in contexts:
        sources = ", ".join(map(str, context.sources))
        preferred = (
            1 if context.numa_node is not None and session.numa_node == context.numa_node else 0
        )
        # Communities express preference to the fabric owner. They do not prove
        # TOR policy, inbound locality, or hardware offload on their own.
        text += f"""   if net ~ [ {sources} ] then {{
    bgp_large_community.add(({domain.host.local_asn}, {context.table}, {preferred})); accept;
   }}
"""
    text += f"""  }} reject; }};
  export limit {export_limit} action disable;
 }};
}}
"""
    # Retain each peer's routes separately before choosing per-context preference;
    # selecting a single global best path would discard the remote fallback.
    for context in contexts:
        context_tag = _tag(context.id)
        preference = (
            200
            if context.numa_node is not None and session.numa_node == context.numa_node
            else 100
        )
        text += f"""protocol pipe {namespace}path_{tag}_context_{context_tag} {{
 table {namespace}path_{tag}; peer table {namespace}context_{context_tag}; import none;
 export filter {{ if proto = "{bgp}" && source = RTS_BGP then {{
  preference = {preference}; accept;
 }} reject; }};
}}
"""
    return text
