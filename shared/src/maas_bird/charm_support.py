"""Small shared helpers; workload orchestration remains in each charm."""

import json
from collections.abc import Mapping
from typing import Any

import ops


def observe(charm, handler, endpoints):
    for event in [
        charm.on.install,
        charm.on.start,
        charm.on.config_changed,
        charm.on.update_status,
        charm.on.upgrade_charm,
        charm.on.secret_changed,
    ]:
        charm.framework.observe(event, handler)
    for endpoint in endpoints:
        for suffix in [
            "relation_joined",
            "relation_changed",
            "relation_departed",
            "relation_broken",
        ]:
            charm.framework.observe(getattr(charm.on[endpoint], suffix), handler)


def relations(charm, endpoint, event):
    """A broken/departing producer must not remain in the desired state."""
    for relation in charm.model.relations[endpoint]:
        if isinstance(event, ops.RelationBrokenEvent) and event.relation.id == relation.id:
            continue
        yield relation


def config_string(config: Mapping[str, object], option: str) -> str:
    value = config[option]
    if not isinstance(value, str):
        raise ValueError(f"{option} must be a string")
    return value


def secret_content(charm, option):
    sid = charm.config[option]
    if not sid:
        raise ValueError(f"Set {option} to a Juju secret ID")
    return charm.model.get_secret(id=sid).get_content(refresh=True)


def json_list(value: str, option: str) -> list[dict[str, Any]]:
    result = json.loads(value)
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise ValueError(f"{option} must be a JSON array of objects")
    return result
