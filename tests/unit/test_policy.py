"""Normalize observed iproute2 JSON without relaxing object ownership."""

from tests.prototype.policy import guard_key, rule_key


def test_split_prefix_lengths_match_canonical_owned_rule():
    owned = {
        "src": "10.60.11.11/32",
        "dst": "203.0.113.0/24",
        "table": 3100,
        "priority": 10000,
        "protocol": 196,
    }
    observed = {
        "src": "10.60.11.11",
        "dst": "203.0.113.0",
        "dstlen": 24,
        "table": "3100",
        "priority": 10000,
        "protocol": "196",
    }
    assert rule_key(observed) == rule_key(owned)
    assert rule_key(observed | {"dstlen": 25}) != rule_key(owned)
    assert rule_key(observed | {"fwmark": "0x1"}) is None
    assert rule_key(observed | {"flags": ["not"]}) is None


def test_numeric_route_type_keeps_exact_guard_identity():
    observed = {"type": "7", "dst": "default", "table": "3100", "protocol": "196", "metric": 42760}
    assert guard_key(observed) == (3100, 196, 42760)
    assert guard_key(observed | {"type": "unreachable"}) == (3100, 196, 42760)
    assert guard_key(observed | {"type": "6"}) is None
    assert guard_key(observed | {"dst": "203.0.113.0/24"}) is None
