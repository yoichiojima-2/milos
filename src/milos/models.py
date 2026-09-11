"""Firestore document types.

Three objects matter: Agent, Session and Event. AgentVersion belongs to an
Agent; Lease, Permission and Approval belong to a Session. Every document is
strict (unknown fields are rejected) and carries a schema version so future
migrations can tell documents apart.

Collections:

    agents/{agent_id}
      versions/{version}
    sessions/{session_id}
      events/{event_id}          append-only; seq is the display order
      permissions/{tool_use_id}  create-only; existence means "permitted"
      approvals/{tool_use_id}    create-only
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def sha256_json(value: Any) -> str:
    """Stable hash of a JSON-serialisable value (sorted keys, no whitespace)."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1

    def doc(self) -> dict[str, Any]:
        """The Firestore representation: enums as strings, datetimes as-is."""
        plain: dict[str, Any] = _plain(self.model_dump())
        return plain


def _plain(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


# --- agents -----------------------------------------------------------------


class Agent(Document):
    """agents/{agent_id}. `enabled=False` stops every version and every running session."""

    agent_id: str
    enabled: bool = True
    latest_version: int


class AgentVersion(Document):
    """agents/{agent_id}/versions/{version}. Published from Git by CI after validation."""

    agent_id: str
    version: int
    definition_sha256: str  # hash of the definition file; pinned onto every session
    purpose: str
    owner: str
    allowed_groups: list[str]  # Google groups allowed to start sessions
    data_classes: list[str]
    allowed_tools: list[str]  # platform capabilities; never passed to the SDK as-is
    approval_required: list[str]  # subset of allowed_tools
    approval_ttl_sec: int
    max_turns: int  # mandatory; a definition without it is not published
    max_budget_usd: float
    max_concurrent_sessions: int
    model: str  # Vertex AI model id
    runner_sa: str  # the agent's dedicated runner service account
    system_prompt: str
    connectors: list[str] = Field(default_factory=list)  # MCP connector names mounted
    published_at: datetime


# --- sessions ---------------------------------------------------------------


class SessionStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    RESCHEDULING = "rescheduling"
    TERMINATED = "terminated"


class StopReason(StrEnum):
    END_TURN = "end_turn"
    REQUIRES_ACTION = "requires_action"
    BUDGET_REACHED = "budget_reached"
    STOPPED = "stopped"
    NEEDS_ATTENTION = "needs_attention"


class Lease(Document):
    """Embedded in Session.lease; not a document of its own.

    The token is re-issued on every job start. Every runner write must present
    the current token, so a job that was replaced can no longer write.
    """

    runner_id: str
    token: str
    last_poll_at: datetime


class Session(Document):
    """sessions/{session_id}."""

    session_id: str
    agent_id: str
    agent_version: int  # pinned at creation
    definition_sha256: str
    status: SessionStatus
    stop_reason: StopReason | None = None
    classification: str  # inherited from the definition's data classes
    operator: str
    client_request_id: str | None = None  # dedupe key for retried/scheduled creation
    content_sha256: str | None = None
    viewers: list[str] = Field(default_factory=list)
    approvers: list[str] = Field(default_factory=list)
    lease: Lease | None = None
    snapshot: int = 0  # snapshot number used for restore
    consumed_seq: int = 0  # last user.* event the runner has handled
    pending_tool_use_ids: list[str] = Field(default_factory=list)
    approval_expires_at: datetime | None = None  # requires_action deadline
    last_event_seq: int = 0  # seq counter; advanced in the same transaction as the event
    created_at: datetime
    updated_at: datetime

    @property
    def active(self) -> bool:
        return self.status in (SessionStatus.RUNNING, SessionStatus.RESCHEDULING)


class EventType(StrEnum):
    USER_MESSAGE = "user.message"
    USER_INTERRUPT = "user.interrupt"
    USER_TOOL_CONFIRMATION = "user.tool_confirmation"
    AGENT_MESSAGE = "agent.message"
    AGENT_TOOL_USE = "agent.tool_use"
    TOOL_PERMITTED = "tool.permitted"
    TOOL_RESULT = "tool.result"  # payload.outcome: succeeded / failed / unknown
    SESSION_USAGE = "session.usage"
    SESSION_STATUS = "session.status"


USER_EVENTS = frozenset({EventType.USER_MESSAGE, EventType.USER_INTERRUPT, EventType.USER_TOOL_CONFIRMATION})
RUNNER_EVENTS = frozenset({EventType.AGENT_MESSAGE, EventType.TOOL_RESULT, EventType.SESSION_USAGE})


class Event(Document):
    """sessions/{session_id}/events/{event_id}. Append-only."""

    event_id: str
    seq: int  # display order, allocated by the API
    type: EventType
    actor: str  # verified caller; runners are identified by their lease token
    client_request_id: str | None = None  # dedupe key for user.* retries
    content_sha256: str | None = None  # same key with a different body is a conflict
    tool_use_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class Permission(Document):
    """sessions/{session_id}/permissions/{tool_use_id}. Create-only; existence means permitted."""

    tool_use_id: str
    lease_token: str
    tool_name: str
    args_sha256: str
    approval_tool_use_id: str | None = None  # the approval this permission consumed
    created_at: datetime


class Approval(Document):
    """sessions/{session_id}/approvals/{tool_use_id}. Create-only."""

    tool_use_id: str
    decision: Literal["allow", "deny"]
    decided_by: str  # from the IAP identity; rejected when equal to the operator
    decided_at: datetime
    expires_at: datetime
    timed_out: bool = False
    tool_name: str
    args_sha256: str  # a re-issued call is matched by content, never by id
