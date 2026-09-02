from __future__ import annotations
import re
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator
from .peers import ALL_PEERS, DEFAULT_SENDER


class Priority(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"
    urgent = "urgent"


class DispatchMode(str, Enum):
    draft = "draft"
    armed = "armed"


class TicketStatus(str, Enum):
    draft = "draft"
    armed = "armed"
    blocked = "blocked"
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


# ── Requests ──────────────────────────────────────────────────────────────────

_PEER_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")  # 1..32 chars total


def _validate_peer_name(v: str) -> str:
    """Format-only validation (regex). Used for 'to' fields — route enforces membership."""
    if not _PEER_RE.match(v):
        raise ValueError(f"Invalid peer name: {v!r} — must match ^[a-z][a-z0-9-]{{0,31}}$")
    return v


def _validate_known_peer(v: str) -> str:
    """Format + membership validation. Used for 'from_' fields to prevent spoofing."""
    _validate_peer_name(v)
    if v not in ALL_PEERS:
        known = ", ".join(sorted(ALL_PEERS)) or "none — this mesh has no roster"
        raise ValueError(f"Unknown peer: {v!r} (known: {known})")
    return v


def _no_null(v):
    """Reject null bytes in free-text fields. Tolerant to None (Optional fields)."""
    if isinstance(v, str) and "\x00" in v:
        raise ValueError("must not contain null bytes")
    return v


class SendRequest(BaseModel):
    to: str
    body: str = Field(..., max_length=65536)
    priority: Priority = Priority.normal
    from_: str = Field(DEFAULT_SENDER, alias="from", validate_default=True)
    reply_to: Optional[str] = None

    model_config = {"populate_by_name": True}

    @field_validator("from_", mode="before")
    @classmethod
    def validate_from(cls, v: str) -> str:
        return _validate_known_peer(v)

    @field_validator("body", mode="before")
    @classmethod
    def no_null_bytes(cls, v):
        return _no_null(v)

    @field_validator("to", mode="before")
    @classmethod
    def validate_to(cls, v: str) -> str:
        return _validate_peer_name(v)


class AckRequest(BaseModel):
    agent: str
    id: str


class TicketCreateRequest(BaseModel):
    to: str
    prompt: str = Field(..., max_length=65536)
    priority: Priority = Priority.normal
    dispatch_mode: DispatchMode = DispatchMode.draft
    depends_on: list[str] = Field(default_factory=list)
    parent_ticket_id: Optional[str] = None
    from_: str = Field(DEFAULT_SENDER, alias="from", validate_default=True)

    model_config = {"populate_by_name": True}

    @field_validator("from_", mode="before")
    @classmethod
    def validate_from(cls, v: str) -> str:
        return _validate_known_peer(v)

    @field_validator("prompt", mode="before")
    @classmethod
    def no_null_bytes(cls, v):
        return _no_null(v)


class BulkTicketItem(BaseModel):
    prompt: str = Field(..., max_length=65536)
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("prompt", mode="before")
    @classmethod
    def no_null_bytes(cls, v):
        return _no_null(v)


class BulkTicketCreateRequest(BaseModel):
    to: str
    tickets: list[BulkTicketItem]
    priority: Priority = Priority.normal
    dispatch_mode: DispatchMode = DispatchMode.draft
    from_: str = Field(DEFAULT_SENDER, alias="from", validate_default=True)

    model_config = {"populate_by_name": True}

    @field_validator("from_", mode="before")
    @classmethod
    def validate_from(cls, v: str) -> str:
        return _validate_known_peer(v)


class TicketPatchRequest(BaseModel):
    position: Optional[int] = None
    prompt: Optional[str] = Field(None, max_length=65536)
    dispatch_mode: Optional[DispatchMode] = None

    @field_validator("prompt", mode="before")
    @classmethod
    def no_null_bytes(cls, v):
        return _no_null(v)


# ── Responses ─────────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"


class AgentStatus(BaseModel):
    alive: bool
    idle: bool
    standby: bool = False  # on-demand agent asleep (wakes on a message), not the same as offline
    last_activity: Optional[str] = None
    last_heartbeat: Optional[str] = None
    tickets_pending: int = 0
    tickets_running: int = 0


class ServiceMap(BaseModel):
    model_config = {"extra": "allow"}


class HostStatus(BaseModel):
    cpu_temp_c: Optional[float] = None
    disk_used_pct: Optional[int] = None
    uptime_h: Optional[float] = None
    load_1m: Optional[float] = None


class HostHealth(BaseModel):
    """Health of one physical machine of the mesh.

    Most fields are optional because what a host can report depends on what it
    is: a small board reports CPU, disk and load; a phone reports its battery; a
    desktop that is only pinged reports nothing but on/off."""
    state: str  # online | offline | offline_intentional | unknown
    label: Optional[str] = None
    last_seen: Optional[str] = None
    note: Optional[str] = None
    # Pi
    cpu_temp_c: Optional[float] = None
    disk_used_pct: Optional[int] = None
    uptime_h: Optional[float] = None
    load_1m: Optional[float] = None
    # Pixel 6a
    battery_pct: Optional[int] = None
    battery_temp_c: Optional[float] = None
    plugged: Optional[str] = None
    battery_status: Optional[str] = None

    model_config = {"extra": "allow"}


class StatusResponse(BaseModel):
    agents: dict[str, AgentStatus]
    services: dict[str, str]
    host: HostStatus
    hosts: dict[str, HostHealth] = Field(default_factory=dict)
    generated_at: str


class MessageOut(BaseModel):
    id: str
    from_: str = Field(alias="from")
    to: str
    priority: str
    body: str
    ts: str
    reply_to: Optional[str] = None

    model_config = {"populate_by_name": True}


class InboxResponse(BaseModel):
    agent: str
    messages: list[MessageOut]
    total: int


class SendResponse(BaseModel):
    id: str
    queued_at: str
    delivered_live: bool = False


class AckResponse(BaseModel):
    acked: bool
    count: int


class TicketOut(BaseModel):
    id: str
    to: str
    from_: str = Field(alias="from")
    status: str
    dispatch_mode: str
    priority: str
    prompt: str
    prompt_preview: str
    depends_on: list[str] = Field(default_factory=list)
    parent_ticket_id: Optional[str] = None
    queued_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    tldr: Optional[str] = None
    position: Optional[int] = None
    full_response_url: Optional[str] = None

    model_config = {"populate_by_name": True}


class TicketsListResponse(BaseModel):
    agent: str
    tickets: list[TicketOut]
    total: int


class TicketCreateResponse(BaseModel):
    id: str
    to: str
    status: str
    queued_at: str
    position: Optional[int] = None


class BulkCreateResponse(BaseModel):
    ids: list[str]
    created_count: int
    first_position: Optional[int] = None


# ── Contexts ──────────────────────────────────────────────────────────────────

class ContextEntry(BaseModel):
    agent: str
    tokens: Optional[int] = None
    max_tokens: Optional[int] = None
    pct: Optional[float] = None
    model: Optional[str] = None
    status: Optional[str] = None
    source: str = "unknown"  # live | cached | stale | unknown
    updated_at: Optional[str] = None


class ContextsResponse(BaseModel):
    contexts: list[ContextEntry]


# ── Graph ─────────────────────────────────────────────────────────────────────

class GraphNode(BaseModel):
    id: str
    type: str  # "agent" | "operator"
    label: str
    color: str


class GraphEdge(BaseModel):
    from_: str = Field(alias="from")
    to: str
    weight: int
    kind: str  # "message" | "ticket"
    last_ts: Optional[str] = None

    model_config = {"populate_by_name": True}


class GraphResponse(BaseModel):
    window: str
    generated_at: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]
