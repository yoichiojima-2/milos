"""The Agent SDK shape: a script sees the journal as SDK messages and decides parked tools from code."""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport

from milos import (
    AssistantMessage,
    MilosClient,
    MilosOptions,
    PermissionResultAllow,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from milos.api import create_app
from milos.auth import IAP_HEADER
from milos.client import Client
from milos.models import EventType, RunnerEvent, SessionStatus, StopReason

from .test_api import FakeIap

OPERATOR, APPROVER = "alice@example.com", "bob@example.com"


@pytest.fixture
def app(service, tokens, directory):
    return create_app(service, role="public", tokens=tokens, iap=FakeIap(), directory=directory)


def as_user(app, email: str) -> Client:
    client = Client("http://public", transport=ASGITransport(app=app))
    client._http.headers[IAP_HEADER] = email
    return client


def sdk_client(app, **options) -> MilosClient:
    """A MilosClient whose HTTP goes to the in-process API as the operator and approver."""
    client = MilosClient(MilosOptions("analyst", api_url="http://public", token="t", poll_interval=0.01, **options))
    client._operator = as_user(app, OPERATOR)
    client._approver = as_user(app, APPROVER) if options.get("approver_token") else None
    return client


async def finish_turn(service, session_id: str, text: str) -> None:
    """Play the runner: consume the input, one message, usage, end of turn."""
    session = await service.get_session(session_id)
    lease = session.lease.token
    poll = await service.poll(session_id, lease_token=lease)
    await service.ack(session_id, lease_token=lease, seq=max(e.seq for e in poll.events))
    await service.report(session_id, [RunnerEvent(type=EventType.AGENT_MESSAGE, payload={"text": text})], lease_token=lease)
    usage = {"num_turns": 1, "duration_ms": 5, "total_cost_usd": 0.01}
    await service.report(session_id, [RunnerEvent(type=EventType.SESSION_USAGE, payload=usage)], lease_token=lease)
    await service.finish(session_id, lease_token=lease, stop_reason=StopReason.END_TURN)


async def test_query_yields_sdk_messages(app, agent, service):
    async def runner():
        while not (sessions := await service.list_sessions(operator=OPERATOR)):
            await asyncio.sleep(0.01)
        await finish_turn(service, sessions[0].session_id, "hello back")

    task = asyncio.create_task(runner())
    messages = []
    # query() builds its own clients; point them at the in-process app.
    client = sdk_client(app)
    await client.query("hi")
    async for message in client.receive_response():
        messages.append(message)
    await task

    assert isinstance(messages[0], UserMessage) and messages[0].content == "hi"
    text = [m for m in messages if isinstance(m, AssistantMessage)]
    assert isinstance(text[0].content[0], TextBlock) and text[0].content[0].text == "hello back"
    result = messages[-1]
    assert isinstance(result, ResultMessage)
    assert result.subtype == "success" and result.result == "hello back" and result.num_turns == 1
    assert result.total_cost_usd == 0.01 and result.session_id == client.session_id


async def test_can_use_tool_decides_parked_calls_as_the_approver(app, agent, service):
    seen = []

    async def allow(tool_name, args, context):
        seen.append((tool_name, args, context.tool_use_id))
        return PermissionResultAllow()

    client = sdk_client(app, can_use_tool=allow, approver_token="approver")
    await client.query("delete x")
    sid = client.session_id
    session = await service.get_session(sid)
    lease = session.lease.token
    decision = await service.permit(sid, lease_token=lease, tool_use_id="t1", tool_name="Bash", args={"command": "rm x"})
    assert decision.outcome == "require_approval"
    await service.finish(sid, lease_token=lease, stop_reason=StopReason.REQUIRES_ACTION)

    async def resumed_runner():
        while True:
            current = await service.get_session(sid)
            if current.status == SessionStatus.RUNNING and current.lease and current.lease.token != lease:
                break
            await asyncio.sleep(0.01)
        result = RunnerEvent(type=EventType.TOOL_RESULT, payload={"outcome": "succeeded", "summary": "gone"}, tool_use_id="t1")
        await service.report(sid, [result], lease_token=current.lease.token)
        await finish_turn(service, sid, "deleted")

    task = asyncio.create_task(resumed_runner())
    messages = [m async for m in client.receive_response()]
    await task

    assert seen == [("Bash", {"command": "rm x"}, "t1")]
    tool_use = next(m for m in messages if isinstance(m, AssistantMessage) and isinstance(m.content[0], ToolUseBlock))
    assert tool_use.content[0].name == "Bash" and tool_use.content[0].input == {"command": "rm x"}
    assert any(isinstance(m, SystemMessage) and m.subtype == "user.approval" for m in messages)
    tool_result = next(m for m in messages if isinstance(m, UserMessage) and isinstance(m.content, list))
    assert isinstance(tool_result.content[0], ToolResultBlock) and tool_result.content[0].content == "gone"
    assert isinstance(messages[-1], ResultMessage) and messages[-1].subtype == "success"
    assert (await service.get_session(sid)).pending == []


async def test_without_can_use_tool_a_parked_session_ends_the_turn(app, agent, service):
    client = sdk_client(app)
    await client.query("delete x")
    sid = client.session_id
    lease = (await service.get_session(sid)).lease.token
    await service.permit(sid, lease_token=lease, tool_use_id="t1", tool_name="Bash", args={"command": "rm x"})
    await service.finish(sid, lease_token=lease, stop_reason=StopReason.REQUIRES_ACTION)
    messages = [m async for m in client.receive_response()]
    result = messages[-1]
    assert isinstance(result, ResultMessage)
    assert result.subtype == "requires_action" and result.stop_reason == "requires_action"
    assert (await client.terminate()).status == SessionStatus.TERMINATED


def test_can_use_tool_needs_an_approver_identity():
    with pytest.raises(ValueError, match="approver_token"):
        MilosClient(MilosOptions("analyst", api_url="http://x", token="t", can_use_tool=lambda *a: None))  # type: ignore[arg-type]


async def test_query_function_is_one_turn(monkeypatch):
    """`query` is sugar over MilosClient; check it wires prompt and options through."""
    calls = []

    class Fake:
        def __init__(self, options):
            calls.append(options)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            calls.append("closed")

        async def query(self, prompt):
            calls.append(prompt)

        async def receive_response(self):
            yield ResultMessage(
                subtype="success", duration_ms=0, duration_api_ms=0, is_error=False, num_turns=1, session_id="s"
            )

    monkeypatch.setattr("milos.sdk.MilosClient", Fake)
    options = MilosOptions("analyst", api_url="http://x", token="t")
    out = [m async for m in query("hi", options)]
    assert calls == [options, "hi", "closed"] and isinstance(out[0], ResultMessage)
