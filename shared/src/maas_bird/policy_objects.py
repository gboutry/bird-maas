"""Exact IPv4 policy object identities; no host mutations or dependencies."""

from ipaddress import IPv4Network

PROTOCOL = 196
GUARD_METRIC = 42760


class PolicyError(RuntimeError):
    pass


def rule_network(rule: dict, field: str):
    value = rule.get(field, "all")
    if value == "all":
        return IPv4Network("0.0.0.0/0")
    # iproute2 versions may serialize a prefix as address plus a separate length.
    if "/" not in value and field + "len" in rule:
        value += "/" + str(rule[field + "len"])
    return IPv4Network(value)


def rule_key(rule: dict):
    if set(rule) - {
        "src",
        "srclen",
        "dst",
        "dstlen",
        "table",
        "priority",
        "protocol",
        "flags",
    } or rule.get("flags"):
        return None
    try:
        return (
            str(rule_network(rule, "src")),
            str(rule_network(rule, "dst")),
            int(rule["table"]),
            int(rule["priority"]),
            int(rule.get("protocol", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def guard_key(route: dict):
    """Recognize the whole catch-all identity, never a restricted foreign route."""
    if set(route) - {"type", "dst", "table", "protocol", "metric", "flags", "scope", "tos"}:
        return None
    if route.get("flags") or str(route.get("scope", "global")) not in {"global", "0"}:
        return None
    if (
        str(route.get("type")) not in {"unreachable", "7"}
        or route.get("dst", "default") != "default"
    ):
        return None
    try:
        tos = route.get("tos", 0)
        if (int(tos, 0) if isinstance(tos, str) else int(tos)) != 0:
            return None
        return int(route["table"]), int(route["protocol"]), int(route["metric"])
    except (KeyError, TypeError, ValueError):
        return None
