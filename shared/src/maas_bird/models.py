"""Versioned interface contracts; absence parses, completeness is checked separately."""

from ipaddress import IPv4Address, IPv4Network, IPv6Address
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from maas_bird import ovsdb

Asn = Annotated[int, Field(strict=True, ge=1, le=4294967294)]


class Record(BaseModel):
    """Ignore additive fields to support independently upgraded producers."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class Identity(Record):
    scheme: str | None = None
    value: str | None = None


class Peer(Record):
    id: str | None = None
    chassis_id: str | None = None
    asn: Asn | None = None


class Host(Record):
    machine_id: str | None = None
    system_uuid: str | None = None
    rack: str | None = None
    local_asn: Asn | None = None
    router_id: IPv4Address | None = None
    peers: tuple[Peer, ...] | None = None


class Inventory(Record):
    """Provider application data. Only the provider leader writes this."""

    rack: str | None = None
    hosts: tuple[Host, ...] | None = None


class RackHeader(Record):
    rack: str | None = None


class InventoryRequest(Record):
    """Consumer unit data; the provider verifies against actual MAAS inventory."""

    system_uuid: str | None = None


class RackRequest(Record):
    """Consumer application selects one immutable rack relation scope."""

    rack: str | None = None


class Function(Record):
    controller: Annotated[int, Field(strict=True, ge=0)] | None = None
    pf: Annotated[int, Field(strict=True, ge=0)] | None = None
    vf: Annotated[int, Field(strict=True, ge=0)] | None = None


class UplinkBinding(Record):
    """Native same-card kernel membership and single-interface OVS uplink identity."""

    physical: str | None = None
    bond: str | None = None
    members: tuple[str, ...] | None = None
    serial: str | None = None
    port_uuid: UUID | None = None
    interface_uuid: UUID | None = None

    @field_validator("physical", "bond")
    @classmethod
    def safe_names(cls, value):
        return ovsdb.interface_name(value) if value is not None else None

    @field_validator("members")
    @classmethod
    def canonical_members(cls, value):
        if value is None:
            return None
        if len(set(value)) != len(value):
            raise ValueError("Duplicate uplink members")
        return tuple(sorted(ovsdb.interface_name(member) for member in value))

    @property
    def complete(self):
        return all(
            value is not None for key, value in self.model_dump().items() if key != "bond"
        ) and bool(self.members)


class OvsPort(Record):
    """A logical attachment with native row identities, never PCI coordinates."""

    bridge: str | None = None
    port: str | None = None
    bridge_uuid: UUID | None = None
    port_uuid: UUID | None = None
    interface_uuid: UUID | None = None
    ifindex: Annotated[int, Field(strict=True, gt=0)] | None = None
    vlan: Annotated[int, Field(strict=True, ge=1, le=4094)] | None = None
    uplink: UplinkBinding | None = None

    @field_validator("bridge", "port")
    @classmethod
    def safe_names(cls, value):
        return ovsdb.interface_name(value) if value is not None else None

    @property
    def complete(self):
        return (
            all(value is not None for value in self.model_dump().values())
            and self.uplink is not None
            and self.uplink.complete
        )


class Attachment(Record):
    id: str | None = None
    function: Function | None = None
    observed_function: Function | None = None
    ovs_port: OvsPort | None = None
    domain: str | None = None
    vlan: Annotated[int, Field(strict=True, ge=1, le=4094)] | None = None
    chassis_id: str | None = None
    peer_link_local: IPv6Address | None = None
    ready: bool | None = Field(default=None, strict=True)

    @field_validator("peer_link_local")
    @classmethod
    def link_local_only(cls, value):
        if value is not None and (not value.is_link_local or value.scope_id):
            raise ValueError("peer must be an unscoped IPv6 link-local address")
        return value


class Card(Record):
    identity: Identity | None = None
    attachments: tuple[Attachment, ...] | None = None


class Attachments(Record):
    """Provider unit data, one document per DPU unit."""

    rack: str | None = None
    cards: tuple[Card, ...] | None = None


class Advertisement(Record):
    machine_id: str | None = None
    domain: str | None = None
    prefix: IPv4Network | None = None
    ready: bool | None = Field(default=None, strict=True)
    context: str | None = None


class Advertisements(Record):
    """None means unknown; an explicit empty tuple is an authoritative withdrawal."""

    rack: str | None = None
    machine_id: str | None = None
    domain: str | None = None
    advertisements: tuple[Advertisement, ...] | None = None


class Session(Record):
    id: str
    interface: str
    peer: IPv6Address
    local: IPv6Address
    remote_asn: Asn
    identity: Identity | None = None
    peer_id: str | None = None
    function: Function | None = None
    ovs_port: OvsPort | None = None
    vlan: int | None = None
    chassis_id: str | None = None
    numa_node: int | None = None


class ContextIntent(Record):
    """Operator-owned source bindings; workload readiness is supplied separately."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    machine_id: str | None = None
    numa_node: Annotated[int, Field(strict=True, ge=0)] | None
    attachment_ids: tuple[str, ...] | None = None
    table: Annotated[int, Field(strict=True, ge=1000, le=4294967295)]
    rule_priority: Annotated[int, Field(strict=True, ge=1000, le=32000)]
    sources: tuple[IPv4Network, ...]

    @field_validator("attachment_ids")
    @classmethod
    def canonical_attachments(cls, value):
        return tuple(sorted(value)) if value is not None else None


class RoutingContext(ContextIntent):
    """Validated internal plan; not a relation payload."""

    model_config = Record.model_config

    exports: tuple[IPv4Network, ...] = ()


class Plan(Record):
    host: Host
    sessions: tuple[Session, ...]
    prefixes: tuple[IPv4Network, ...]
    imports: tuple[IPv4Network, ...]
    kernel_table: int
    bfd_tx: int
    bfd_rx: int
    bfd_multiplier: int
    hold_time: int
    keepalive_time: int
    contexts: tuple[RoutingContext, ...] = ()
    domain: str = ""


class RoutingDomain(Record):
    """Operator configuration, distinct from relation snapshots."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    import_prefixes: tuple[IPv4Network, ...]
    export_allowlist: tuple[IPv4Network, ...]
    contexts: tuple[ContextIntent, ...]
    bfd_min_tx_ms: Annotated[int, Field(strict=True, ge=50, le=60000)] | None = None
    bfd_min_rx_ms: Annotated[int, Field(strict=True, ge=50, le=60000)] | None = None
    bfd_multiplier: Annotated[int, Field(strict=True, ge=1, le=255)] | None = None
    bgp_hold_time_seconds: Annotated[int, Field(strict=True, ge=3, le=65535)] | None = None
    bgp_keepalive_time_seconds: Annotated[int, Field(strict=True, ge=1, le=21845)] | None = None


class MultiDomainPlan(Record):
    advertisement_mode: Literal["workload", "node-addresses"] = "workload"
    host: Host
    domains: tuple[Plan, ...]

    @property
    def sessions(self):
        return tuple(session for domain in self.domains for session in domain.sessions)

    @property
    def contexts(self):
        return tuple(context for domain in self.domains for context in domain.contexts)

    @property
    def prefixes(self):
        return tuple(prefix for domain in self.domains for prefix in domain.prefixes)


def routing_plans(plan: Plan | MultiDomainPlan) -> tuple[Plan, ...]:
    return plan.domains if isinstance(plan, MultiDomainPlan) else (plan,)


def sessions_for_context(plan: Plan, context: RoutingContext) -> tuple[Session, ...]:
    """Select dedicated paths, or retain the existing shared-domain behavior.

    None is the backwards-compatible shared pair. Explicit IDs
    are operator authority: only those sessions may import into or export from
    this context, even when another context uses the same TORs.
    """
    return tuple(
        session
        for session in plan.sessions
        if context.attachment_ids is None or session.id in context.attachment_ids
    )


def advertisement_mode(config):
    """Only an explicit operator choice may replace workload origin authority."""
    value = config.get("advertisement-mode", "workload")
    if value == "workload":
        return "workload"
    if value == "node-addresses":
        return "node-addresses"
    raise ValueError("advertisement-mode must be workload or node-addresses")
