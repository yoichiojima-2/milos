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

import fnmatch
import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
        return {k: _plain(v) for k, v in self.model_dump().items()}


def _plain(value: Any) -> Any:
    match value:
        case StrEnum():
            return value.value
        case dict():
            return {k: _plain(v) for k, v in value.items()}
        case list():
            return [_plain(v) for v in value]
        case _:
            return value


type ToolDecision = Literal["allow", "deny"]


# --- agents -----------------------------------------------------------------


class Agent(Document):
    """agents/{agent_id}. `enabled=False` stops every version and every running session."""

    agent_id: str
    enabled: bool = True
    latest_version: int


# Tools the SDK ships that must never be granted directly: the web goes
# through a connector, where the URL is logged and the host is checked.
FORBIDDEN_TOOLS = ("WebFetch", "WebSearch")
# The SDK's own tools an agent may be granted; anything else must be an MCP
# tool exposed by a connector (`mcp__<connector>__<tool>`).
SDK_TOOLS = ("Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "NotebookEdit", "Task")


def _email(value: str) -> str:
    if "@" not in value:
        raise ValueError("must be an email address")
    return value


class AgentVersion(Document):
    """agents/{agent_id}/versions/{version}. Published from Git by CI after validation.

    The rules a definition must meet are validators here, so one round of
    validation reports every problem in the file.
    """

    agent_id: str
    version: int
    definition_sha256: str  # hash of the definition file; pinned onto every session
    purpose: str
    owner: str
    allowed_groups: list[str]  # Google groups allowed to start sessions
    allowed_users: list[str] = Field(default_factory=list)  # Explicit Google identities for projects without groups
    data_classes: list[str]
    allowed_tools: list[str]  # platform capabilities; never passed to the SDK as-is
    approval_required: list[str]  # subset of allowed_tools
    approval_ttl_sec: int = Field(gt=0)
    max_turns: int = Field(gt=0)  # mandatory; a definition without it is not published
    max_budget_usd: float = Field(gt=0)
    max_concurrent_sessions: int = Field(gt=0)
    model: str  # Vertex AI model id
    runner_sa: str  # the agent's dedicated runner service account
    system_prompt: str
    connectors: list[str] = Field(default_factory=list)  # MCP connector names mounted
    published_at: datetime

    @field_validator("purpose")
    @classmethod
    def _purpose_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("owner")
    @classmethod
    def _owner_email(cls, value: str) -> str:
        return _email(value)

    @field_validator("allowed_users")
    @classmethod
    def _users_are_lower_case_emails(cls, users: list[str]) -> list[str]:
        if any("@" not in u or u != u.strip().lower() for u in users):
            raise ValueError("must contain lower-case email addresses")
        return users

    @field_validator("runner_sa")
    @classmethod
    def _runner_sa_is_service_account(cls, value: str) -> str:
        if not value.endswith(".iam.gserviceaccount.com"):
            raise ValueError("must be a service account email")
        return value

    @field_validator("allowed_tools")
    @classmethod
    def _tools_are_known(cls, tools: list[str]) -> list[str]:
        problems = []
        for tool in tools:
            if tool in FORBIDDEN_TOOLS:
                problems.append(f"{tool} may not be granted directly; use a connector")
            elif not (tool in SDK_TOOLS or tool.startswith("mcp__")):
                problems.append(f"unknown tool {tool}")
        if problems:
            raise ValueError("; ".join(problems))
        return tools

    @model_validator(mode="after")
    def _fields_agree(self) -> "AgentVersion":
        problems = []
        if not self.allowed_groups and not self.allowed_users:
            problems.append("allowed_groups must name at least one group or allowed_users must name a user")
        for tool in self.approval_required:
            if not any(fnmatch.fnmatchcase(tool, p) or tool == p for p in self.allowed_tools):
                problems.append(f"approval_required entry {tool} is not in allowed_tools")
        for tool in self.allowed_tools:
            if tool.startswith("mcp__"):
                name, separator, _ = tool.removeprefix("mcp__").partition("__")
                connector = name if separator else ""
                if connector not in self.connectors:
                    problems.append(f"{tool} needs connector {connector!r} in connectors")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class Published(BaseModel):
    """An agent with its latest version, as the public API returns it."""

    model_config = ConfigDict(extra="forbid")

    agent: Agent
    version: AgentVersion


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
    decision: ToolDecision
    decided_by: str  # from the IAP identity; rejected when equal to the operator
    decided_at: datetime
    expires_at: datetime
    timed_out: bool = False
    tool_name: str
    args_sha256: str  # a re-issued call is matched by content, never by id
