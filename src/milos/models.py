"""Firestore document types and the API's wire types.

Three objects matter: Agent, Session and Event. AgentVersion belongs to an
Agent; Permission and Approval belong to a Session. Every document is strict:
unknown fields are rejected.

Collections:

    agents/{agent_id}
      versions/{version}
    sessions/{session_id}
      events/{event_id}          append-only; seq is the display order
      permissions/{tool_use_id}  create-only; existence means "permitted"
      approvals/{tool_use_id}    create-only
    requests/{key}               create-only; one per (actor, client_request_id)

Vocabulary: the runner sends a *permission request* and gets an `Outcome`; an
`allow` outcome creates a *permission*. A `require_approval` outcome parks the
session until a person records an *approval* with a `Verdict`.
"""

import fnmatch
import hashlib
import json
import secrets
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import Invalid


def utcnow() -> datetime:
    return datetime.now(UTC)


def sha256_json(value: Any) -> str:
    """Stable hash of a JSON-serialisable value (sorted keys, no whitespace)."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


class Outcome(StrEnum):
    """The API's answer to a permission request."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"
    STOP = "stop"


class Verdict(StrEnum):
    """What an approver (or the timeout) decided."""

    ALLOW = "allow"
    DENY = "deny"


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


def _problem(error: Any) -> str:
    """`field: message`, without pydantic's "Value error, " prefix on our own messages."""
    message = str(error["msg"]).removeprefix("Value error, ")
    location = ".".join(str(part) for part in error["loc"])
    return f"{location}: {message}" if location else message


def _email(value: str) -> str:
    if "@" not in value:
        raise ValueError("must be an email address")
    return value


class AgentVersion(Document):
    """agents/{agent_id}/versions/{version}. Published from Git by CI after validation.

    The rules a definition must meet are validators here, so one round of
    validation reports every problem in the file.
    """

    @classmethod
    def from_yaml(cls, path: "str | Path") -> "AgentVersion":
        """Parse and validate one definition file. Raises `Invalid` naming every problem found."""
        raw = Path(path).read_bytes()
        data = yaml.safe_load(raw) or {}
        if not isinstance(data, dict):
            raise Invalid(f"{path}: definition must be a mapping")
        fields = {**data, "version": 0, "definition_sha256": hashlib.sha256(raw).hexdigest(), "published_at": utcnow()}
        try:
            return cls.model_validate(fields)
        except ValidationError as error:
            raise Invalid("invalid definition: " + "; ".join(_problem(e) for e in error.errors())) from error

    agent_id: str
    version: int
    definition_sha256: str  # hash of the definition file; pinned onto every session
    purpose: str
    owner: str
    allowed_groups: list[str]  # Google groups allowed to start sessions
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
        if not self.allowed_groups:
            problems.append("allowed_groups must name at least one group")
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


class Lease(BaseModel):
    """Embedded in Session.lease; not a document of its own.

    The token is re-issued on every job start. Every runner write must present
    the current token, so a job that was replaced can no longer write.
    """

    model_config = ConfigDict(extra="forbid")

    runner_id: str
    token: str
    last_poll_at: datetime


class PendingCall(BaseModel):
    """A tool call parked for approval, embedded in Session.pending."""

    model_config = ConfigDict(extra="forbid")

    tool_use_id: str
    tool_name: str
    args_sha256: str
    event_id: str  # the agent.tool_use event that journaled the request


class SessionFields(Document):
    """Everything about a session but its lease: shared by the stored `Session` and the public `SessionView`."""

    session_id: str
    agent_id: str
    agent_version: int  # pinned at creation
    definition_sha256: str
    status: SessionStatus
    stop_reason: StopReason | None = None
    operator: str
    viewers: list[str] = Field(default_factory=list)
    approvers: list[str] = Field(default_factory=list)  # empty: anyone in the agent's groups but the operator
    snapshot: int = 0  # snapshot number used for restore
    consumed_seq: int = 0  # last user.* event the runner has handled
    last_message_seq: int = 0  # seq of the newest user.message; > consumed_seq means input is waiting
    pending: list[PendingCall] = Field(default_factory=list)
    approval_expires_at: datetime | None = None  # requires_action deadline
    last_event_seq: int = 0  # seq counter; advanced in the same transaction as the event
    created_at: datetime
    updated_at: datetime


class Session(SessionFields):
    """sessions/{session_id}."""

    lease: Lease | None = None


class LeaseView(BaseModel):
    """A lease as users see it: who holds it and when it last polled, never the token."""

    model_config = ConfigDict(extra="forbid")

    runner_id: str
    last_poll_at: datetime


class SessionView(SessionFields):
    """What the public API returns for a session: the lease token is a runner credential and stays inside."""

    lease: LeaseView | None = None

    @classmethod
    def of(cls, session: Session) -> "SessionView":
        lease = LeaseView(runner_id=session.lease.runner_id, last_poll_at=session.lease.last_poll_at) if session.lease else None
        return cls(**session.model_dump(exclude={"lease"}), lease=lease)


class EventType(StrEnum):
    USER_MESSAGE = "user.message"
    USER_INTERRUPT = "user.interrupt"
    USER_APPROVAL = "user.approval"
    AGENT_MESSAGE = "agent.message"
    AGENT_TOOL_USE = "agent.tool_use"
    TOOL_PERMITTED = "tool.permitted"
    TOOL_RESULT = "tool.result"  # payload.outcome: succeeded / failed / unknown
    SESSION_USAGE = "session.usage"
    SESSION_STATUS = "session.status"


USER_EVENTS = frozenset({EventType.USER_MESSAGE, EventType.USER_INTERRUPT, EventType.USER_APPROVAL})
RUNNER_EVENTS = frozenset({EventType.AGENT_MESSAGE, EventType.TOOL_RESULT, EventType.SESSION_USAGE})


class Event(Document):
    """sessions/{session_id}/events/{event_id}. Append-only."""

    event_id: str
    seq: int  # display order, allocated by the API
    type: EventType
    actor: str  # verified caller, or "runner", "api", "scheduler", "system:inspection"
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
    verdict: Verdict
    decided_by: str  # from the IAP identity; rejected when equal to the operator
    decided_at: datetime
    expires_at: datetime
    timed_out: bool = False
    tool_name: str
    args_sha256: str  # a re-issued call is matched by content, never by id


class Request(Document):
    """requests/{key}. Create-only: the record of one user request, so a retry returns the same result.

    `key` is `sha256_json([actor, client_request_id])[:32]`; a retry with a different
    body (`content_sha256`) is a conflict.
    """

    actor: str
    client_request_id: str
    content_sha256: str
    session_id: str
    event_id: str | None = None  # the event the request produced; None for session creation
    created_at: datetime

    @staticmethod
    def key(actor: str, client_request_id: str) -> str:
        return sha256_json([actor, client_request_id])[:32]


# --- wire types: what the API accepts and returns --------------------------------
#
# Shared by `api.py`, `client.py` and `control.py`, so the runner and the CLI never
# import the service. Bodies that create something carry a `client_request_id`;
# a caller that wants a retry to be safe passes its own.


def _request_id() -> str:
    return secrets.token_hex(8)


class Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewSession(Wire):
    agent_id: str
    message: str
    client_request_id: str = Field(default_factory=_request_id)
    viewers: list[str] = Field(default_factory=list)
    approvers: list[str] = Field(default_factory=list)


class NewMessage(Wire):
    text: str
    client_request_id: str = Field(default_factory=_request_id)


class NewInterrupt(Wire):
    client_request_id: str = Field(default_factory=_request_id)


class NewApproval(Wire):
    tool_use_id: str
    verdict: Verdict


class AgentPatch(Wire):
    enabled: bool


class Me(Wire):
    """Who the API sees, for a client that only has a cookie: the console."""

    email: str
    admin: bool  # a member of `MILOS_ADMIN_GROUP`
    now: datetime  # the API's clock, so deadlines render against it rather than the browser's


class PermissionRequest(Wire):
    """The runner's `PreToolUse` question."""

    tool_use_id: str
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)


class PermissionAnswer(Wire):
    tool_use_id: str
    outcome: Outcome
    reason: str
    approval_tool_use_id: str | None = None  # the approval an allow consumed


class PermissionLookup(Wire):
    """A connector's check: is this exact call permitted under the current lease?"""

    permitted: bool
    tool_use_id: str | None = None


class RunnerEvent(Wire):
    """What a runner may append: agent.message, tool.result, session.usage."""

    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    tool_use_id: str | None = None


class Ack(Wire):
    seq: int


class SnapshotPointer(Wire):
    number: int


class Finish(Wire):
    stop_reason: StopReason


class RunnerContext(Wire):
    session: Session
    version: AgentVersion


class Polled(Wire):
    stop: bool
    events: list[Event]


class Inspection(Wire):
    expired: list[str] = Field(default_factory=list)
    restarted: list[str] = Field(default_factory=list)
    attention: list[str] = Field(default_factory=list)
