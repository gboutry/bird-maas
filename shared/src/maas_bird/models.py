"""Versioned interface contracts; absence parses, completeness is checked separately."""

from ipaddress import IPv4Address, IPv4Network, IPv6Address
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

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

    hosts: tuple[Host, ...] | None = None


class InventoryRequest(Record):
    """Consumer unit data; the provider verifies against actual MAAS inventory."""

    system_uuid: str | None = None


class Function(Record):
    controller: Annotated[int, Field(strict=True, ge=0)] | None = None
    pf: Annotated[int, Field(strict=True, ge=0)] | None = None
    vf: Annotated[int, Field(strict=True, ge=0)] | None = None


class Attachment(Record):
    id: str | None = None
    function: Function | None = None
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

    cards: tuple[Card, ...] | None = None


class Advertisement(Record):
    machine_id: str | None = None
    domain: str | None = None
    prefix: IPv4Network | None = None
    ready: bool | None = Field(default=None, strict=True)
    context: str | None = None


class Advertisements(Record):
    """None means unknown; an explicit empty tuple is an authoritative withdrawal."""

    advertisements: tuple[Advertisement, ...] | None = None


class Session(Record):
    id: str
    interface: str
    peer: IPv6Address
    local: IPv6Address
    remote_asn: Asn
    numa_node: int | None = None


class RoutingContext(Record):
    """Validated internal plan; not a relation payload."""

    id: str
    machine_id: str | None = None
    numa_node: Annotated[int, Field(strict=True, ge=0)]
    table: Annotated[int, Field(strict=True, ge=1000, le=4294967295)]
    rule_priority: Annotated[int, Field(strict=True, ge=1000, le=32000)]
    sources: tuple[IPv4Network, ...]
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
