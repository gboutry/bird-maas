"""GET-only MAAS client with OAuth1 authentication and verified HTTPS."""

from collections import Counter
from urllib.parse import urljoin, urlsplit

import requests
from requests_oauthlib import OAuth1

from maas_bird.models import Host, Inventory, Peer


class InventoryError(RuntimeError):
    pass


class MaasClient:
    def __init__(self, url: str, credential: str, ca: str | bool = True):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("MAAS URL must be HTTPS without credentials, query or fragment")
        parts = credential.split(":")
        if len(parts) != 3 or not all(parts):
            raise ValueError(
                "MAAS credential must contain consumer key, token key and token secret"
            )
        if ca is False:
            raise ValueError("TLS certificate verification cannot be disabled")
        self.url = url.rstrip("/") + "/"
        self.verify = ca
        self.auth = OAuth1(
            parts[0],
            resource_owner_key=parts[1],
            resource_owner_secret=parts[2],
            signature_method="PLAINTEXT",
        )

    def machines(self) -> list[dict]:
        try:
            # No redirects: do not forward OAuth credentials to an unexpected origin.
            response = requests.get(
                urljoin(self.url, "api/2.0/machines/"),
                auth=self.auth,
                verify=self.verify,
                timeout=(5, 30),
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise InventoryError(
                    f"MAAS inventory request returned HTTP {response.status_code}"
                )
            result = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise InventoryError("MAAS inventory read failed") from exc
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise InventoryError("Unexpected MAAS inventory response")
        return result


def normalize(machines: list[dict], racks: list[dict], identities: list[dict]) -> Inventory:
    """Join explicit allocations and rack policy with allowlisted MAAS fields."""

    allocated = {item["machine_id"]: item for item in identities}
    if len(allocated) != len(identities):
        raise ValueError("Duplicate machine identity allocations")
    rack_by_id = {item["id"]: item for item in racks}
    if len(rack_by_id) != len(racks):
        raise ValueError("Duplicate rack profiles")
    hosts: list[Host] = []
    seen = set()
    for machine in machines:
        mid = machine.get("system_id")
        if mid not in allocated:
            continue
        if mid in seen:
            raise ValueError("Duplicate MAAS machine ID")
        seen.add(mid)
        rack_ids = [
            tag.removeprefix("rack-")
            for tag in machine.get("tag_names", [])
            if tag.startswith("rack-")
        ]
        if len(rack_ids) != 1 or rack_ids[0] not in rack_by_id:
            raise ValueError("Allocated machine must have exactly one configured rack tag")
        allocation = allocated[mid]
        uid = machine.get("hardware_uuid")
        if not uid:
            raise ValueError("Allocated machine has no hardware UUID")
        peers = tuple(Peer.model_validate(item) for item in rack_by_id[rack_ids[0]]["peers"])
        if not peers or any(not p.id or not p.asn or not p.chassis_id for p in peers):
            raise ValueError("Rack peers are incomplete")
        hosts.append(
            Host(
                machine_id=mid,
                system_uuid=uid.lower(),
                rack=rack_ids[0],
                local_asn=allocation["local_asn"],
                router_id=allocation["router_id"],
                peers=peers,
            )
        )
    if seen != set(allocated):
        raise ValueError("An allocated machine is missing from MAAS inventory")
    for values in [
        [h.system_uuid for h in hosts],
        [h.router_id for h in hosts],
        [h.local_asn for h in hosts],
    ]:
        if any(count != 1 for count in Counter(values).values()):
            raise ValueError("Duplicate routing or hardware identity")
    hosts.sort(key=lambda host: host.machine_id or "")
    return Inventory(hosts=tuple(hosts))


def normalize_rack(machines, racks, identities, rack, known_racks):
    """A complete MAAS response can revoke one invalid/missing host, not its rack.

    Transport failures are handled outside this function and never become empty
    snapshots. Successful responses must prove each host's identity and rack tag.
    Missing/ambiguous host observations confer no routing authorization.
    """
    by_machine = {machine.get("system_id"): machine for machine in machines}
    if len(by_machine) != len(machines):
        raise ValueError("Duplicate MAAS machine ID")
    bindings = dict(known_racks)
    valid = []
    for allocation in identities:
        machine = by_machine.get(allocation["machine_id"])
        if machine is None:
            continue
        tags = [
            t.removeprefix("rack-") for t in machine.get("tag_names", []) if t.startswith("rack-")
        ]
        if tags != [rack] or not machine.get("hardware_uuid"):
            continue
        # Validate one record before including it in the complete rack snapshot.
        # Global routing-identity conflicts are rejected by the caller first.
        try:
            normalize([machine], racks, [allocation])
        except (ValueError, KeyError):
            continue
        valid.append(allocation)
        bindings[allocation["machine_id"]] = rack
    result = normalize(machines, racks, valid)
    return result.model_copy(update={"rack": rack}), bindings


def restrict_cached_inventory(previous, racks, identities):
    """While MAAS is unavailable, apply removals but never grant new identities."""
    allocations = {row["machine_id"]: row for row in identities}
    profile = next((row for row in racks if row["id"] == previous.rack), None)
    peers = tuple(Peer.model_validate(row) for row in profile["peers"]) if profile else ()
    retained = []
    for host in previous.hosts or ():
        allocation = allocations.get(host.machine_id)
        if (
            allocation is None
            or str(host.router_id) != allocation["router_id"]
            or host.local_asn != allocation["local_asn"]
        ):
            continue
        retained.append(
            host.model_copy(
                update={"peers": tuple(peer for peer in host.peers or () if peer in peers)}
            )
        )
    return previous.model_copy(
        update={"hosts": None if previous.hosts is None else tuple(retained)}
    )
