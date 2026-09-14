"""The Agent SDK shape over the milos API.

A script drives a milos session the way it would drive `claude_agent_sdk`:

    async for message in query(prompt="Summarise last week.", options=MilosOptions(agent="analyst")):
        ...

    async with MilosClient(MilosOptions(agent="analyst")) as client:
        await client.query("Summarise last week.")
        async for message in client.receive_response():
            ...

Messages are `claude_agent_sdk`'s own types (`AssistantMessage`, `UserMessage`,
`SystemMessage`, `ResultMessage` and their content blocks), rebuilt from the
session journal. The platform keeps the security model: the model runs in its
own runner, tools are gated by the agent's definition, and a tool the definition
does not pre-approve parks the session until someone other than the operator
decides it. `MilosOptions.can_use_tool` plays that role from code when an
approver identity is given.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from .client import Client, id_token
from .models import Event, EventType, Session, SessionStatus, StopReason, ToolDecision

PermissionResult = PermissionResultAllow | PermissionResultDeny
CanUseTool = Callable[[str, dict[str, Any], ToolPermissionContext], Awaitable[PermissionResult]]
Message = AssistantMessage | UserMessage | SystemMessage | ResultMessage

_SUBTYPES = {
    StopReason.END_TURN: "success",
    StopReason.BUDGET_REACHED: "error_max_budget",
    StopReason.REQUIRES_ACTION: "requires_action",
    StopReason.STOPPED: "stopped",
    StopReason.NEEDS_ATTENTION: "error_during_execution",
}


@dataclass
class MilosOptions:
    """What `ClaudeAgentOptions` is to the Agent SDK. Everything about the model and its tools lives in the agent."""

    agent: str
    api_url: str | None = None  # default: MILOS_API_URL
    token: str | None = None  # default: MILOS_ID_TOKEN or gcloud, audience MILOS_IAP_CLIENT_ID
    approvers: list[str] = field(default_factory=list)
    viewers: list[str] = field(default_factory=list)
    client_request_id: str | None = None  # makes the first `query` retry-safe
    can_use_tool: CanUseTool | None = None  # decides parked tool calls; needs `approver_token`
    approver_token: str | None = None  # identity token of someone who is not the operator
    poll_interval: float = 2.0


class MilosClient:
    """`ClaudeSDKClient` over a milos session.

    `connect` starts the session (or, with no prompt, waits for the first `query`);
    `receive_response` yields the messages of one turn; `disconnect` closes the
    connection and leaves the session on the platform where it can be resumed.
    """

    def __init__(self, options: MilosOptions) -> None:
        if options.can_use_tool and not options.approver_token:
            raise ValueError("can_use_tool needs approver_token: the operator cannot approve their own tool calls")
        self.options = options
        url = options.api_url or os.environ.get("MILOS_API_URL")
        if not url:
            raise KeyError("MILOS_API_URL")
        self._operator = Client(url, token=options.token or id_token(os.environ.get("MILOS_IAP_CLIENT_ID")))
        self._approver = Client(url, token=options.approver_token) if options.approver_token else None
        self._session: Session | None = None
        self._after = 0

    @property
    def session_id(self) -> str | None:
        return self._session.session_id if self._session else None

    async def connect(self, prompt: str | None = None) -> None:
        if prompt is not None:
            await self.query(prompt)

    async def query(self, prompt: str) -> None:
        """Send one user message: the first starts the session, the rest continue it."""
        if self._session is None:
            self._session = await self._operator.create_session(
                self.options.agent,
                prompt,
                client_request_id=self.options.client_request_id,
                approvers=self.options.approvers,
                viewers=self.options.viewers,
            )
        else:
            await self._operator.send(self._session.session_id, prompt)

    async def receive_response(self) -> AsyncIterator[Message]:
        """Messages until the turn ends; the last one is a `ResultMessage`."""
        async for message in self._receive(until_idle=True):
            yield message

    async def receive_messages(self) -> AsyncIterator[Message]:
        """Messages for as long as the session exists, across turns."""
        async for message in self._receive(until_idle=False):
            yield message

    async def interrupt(self) -> None:
        await self._operator.interrupt(self._require_session().session_id)

    async def terminate(self) -> Session:
        """End the session. Not in the Agent SDK, where a session dies with its process."""
        self._session = await self._operator.terminate(self._require_session().session_id)
        return self._session

    async def disconnect(self) -> None:
        await self._operator.close()
        if self._approver:
            await self._approver.close()

    async def __aenter__(self) -> MilosClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.disconnect()

    def _require_session(self) -> Session:
        if self._session is None:
            raise RuntimeError("no session yet: call query() first")
        return self._session

    async def _receive(self, *, until_idle: bool) -> AsyncIterator[Message]:
        session = self._require_session()
        usage: dict[str, Any] = {}
        last_text: str | None = None
        while True:
            async for event in self._operator.follow(
                session.session_id, after=self._after, interval=self.options.poll_interval
            ):
                self._after = event.seq
                if event.type == EventType.SESSION_USAGE:
                    usage = event.payload
                    continue
                message = _message(event, session.agent_id)
                if isinstance(message, AssistantMessage) and isinstance(message.content[0], TextBlock):
                    last_text = message.content[0].text
                yield message
            self._session = session = await self._operator.session(session.session_id)
            if session.stop_reason == StopReason.REQUIRES_ACTION and self.options.can_use_tool:
                await self._decide(session)
                continue
            if until_idle or session.status == SessionStatus.TERMINATED:
                yield _result(session, usage, last_text)
                return

    async def _decide(self, session: Session) -> None:
        assert self.options.can_use_tool and self._approver
        events = await self._operator.events(session.session_id)
        calls = {e.tool_use_id: e.payload for e in events if e.type == EventType.AGENT_TOOL_USE}
        for tool_use_id in session.pending_tool_use_ids:
            call = calls[tool_use_id]
            context = ToolPermissionContext(tool_use_id=tool_use_id)
            result = await self.options.can_use_tool(call["tool_name"], call["args"], context)
            decision: ToolDecision = "allow" if isinstance(result, PermissionResultAllow) else "deny"
            await self._approver.confirm(session.session_id, tool_use_id, decision)


async def query(prompt: str, options: MilosOptions) -> AsyncIterator[Message]:
    """One turn of a new session, as `claude_agent_sdk.query`."""
    async with MilosClient(options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            yield message


def _message(event: Event, model: str) -> Message:
    p = event.payload
    match event.type:
        case EventType.USER_MESSAGE:
            return UserMessage(content=p.get("text", ""), uuid=event.event_id)
        case EventType.AGENT_MESSAGE:
            return AssistantMessage(content=[TextBlock(text=p.get("text", ""))], model=model, uuid=event.event_id)
        case EventType.AGENT_TOOL_USE:
            use = ToolUseBlock(id=event.tool_use_id or "", name=p.get("tool_name", ""), input=p.get("args", {}))
            return AssistantMessage(content=[use], model=model, uuid=event.event_id)
        case EventType.TOOL_RESULT:
            result = ToolResultBlock(
                tool_use_id=event.tool_use_id or "",
                content=p.get("summary"),
                is_error=p.get("outcome") == "failed",
            )
            return UserMessage(content=[result], uuid=event.event_id)
        case _:
            return SystemMessage(subtype=event.type.value, data=p)


def _result(session: Session, usage: dict[str, Any], last_text: str | None) -> ResultMessage:
    stop = session.stop_reason
    if session.status == SessionStatus.TERMINATED:
        subtype = "terminated"
    else:
        subtype = _SUBTYPES.get(stop, "unknown") if stop else "unknown"
    return ResultMessage(
        subtype=subtype,
        duration_ms=int(usage.get("duration_ms") or 0),
        duration_api_ms=0,
        is_error=stop == StopReason.NEEDS_ATTENTION,
        num_turns=int(usage.get("num_turns") or 0),
        session_id=session.session_id,
        stop_reason=stop.value if stop else None,
        total_cost_usd=usage.get("total_cost_usd"),
        usage=usage or None,
        result=last_text,
    )
