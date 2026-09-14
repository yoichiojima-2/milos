"""The runner against the real service behind an in-process internal API, with the SDK faked."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport

from milos.api import create_app
from milos.control import Control
from milos.models import EventType, SessionStatus, StopReason
from milos.runner import Run, continuation
from milos.settings import RunnerSettings

from .fakes import FakeBlobs

# --- a scripted stand-in for ClaudeSDKClient -------------------------------------


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str


@dataclass
class Script:
    """What the fake model does on each turn: a list of tool calls, then a reply."""

    turns: list[list[ToolCall]] = field(default_factory=list)
    reply: str = "done"
    subtype: str = "success"


class FakeSDKClient:
    """Drives the gate exactly as the SDK would: PreToolUse hook, then tool or denial."""

    instances: list[FakeSDKClient] = []

    def __init__(self, options: Any) -> None:
        self.options = options
        self.prompts: list[str] = []
        self.interrupted = False
        self.executed: list[str] = []
        self.script: Script = FakeSDKClient.script
        FakeSDKClient.instances.append(self)

    async def __aenter__(self) -> FakeSDKClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def interrupt(self) -> None:
        self.interrupted = True

    async def receive_response(self):
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ToolResultBlock,
            UserMessage,
        )

        hook = self.options.hooks["PreToolUse"][0].hooks[0]
        turn = len(self.prompts) - 1
        calls = self.script.turns[turn] if turn < len(self.script.turns) else []
        for call in calls:
            output = await hook({"tool_name": call.name, "tool_input": call.args}, call.id, None)
            decision = output["hookSpecificOutput"]["permissionDecision"]
            if decision == "allow":
                self.executed.append(call.id)
                yield UserMessage(content=[ToolResultBlock(tool_use_id=call.id, content="ok")])
            else:
                yield UserMessage(content=[ToolResultBlock(tool_use_id=call.id, content="denied", is_error=True)])
            if self.interrupted:
                break
        if not self.interrupted:
            yield AssistantMessage(content=[TextBlock(text=self.script.reply)], model="fake")
        yield ResultMessage(
            subtype=self.script.subtype if not self.interrupted else "success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sdk-session-1",
            total_cost_usd=0.01,
            terminal_reason="aborted_tools" if self.interrupted else "completed",
        )


@pytest.fixture
def sdk(monkeypatch):
    FakeSDKClient.instances = []
    FakeSDKClient.script = Script()
    return FakeSDKClient


@pytest.fixture
def blobs() -> FakeBlobs:
    return FakeBlobs()


def make_run(service, tokens, session, sdk, blobs, tmp_path: Path, monkeypatch) -> Run:
    app = create_app(service, role="internal", tokens=tokens)
    control = Control(
        "http://internal",
        session_id=session.session_id,
        session_token=tokens.issue(session.session_id),
        lease_token=session.lease.token,
        transport=ASGITransport(app=app),
    )
    settings = RunnerSettings(
        session_id=session.session_id,
        session_token=tokens.issue(session.session_id),
        lease_token=session.lease.token,
        runner_id="r1",
        api_url="http://internal",
        project="p",
        snapshot_bucket="b",
        connector_urls={"egress": "http://egress"},
        vertex_region="us-east5",
        work_dir=str(tmp_path / "work"),
        idle_seconds=0,
        poll_seconds=0,
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return Run(settings, control, blobs=blobs, client_factory=sdk)


async def test_plain_turn_reports_messages_and_finishes(service, tokens, session, sdk, blobs, tmp_path, monkeypatch):
    sdk.script = Script(turns=[[ToolCall("Read", {"path": "a.csv"}, "t1")]], reply="the report")
    run = make_run(service, tokens, session, sdk, blobs, tmp_path, monkeypatch)
    assert await run() == StopReason.END_TURN
    client = sdk.instances[0]
    assert client.prompts == ["hello"] and client.executed == ["t1"]
    types = [e.type for e in await service.events(session.session_id)]
    assert EventType.AGENT_TOOL_USE in types and EventType.TOOL_PERMITTED in types
    assert EventType.TOOL_RESULT in types and EventType.AGENT_MESSAGE in types
    assert types[-1] == EventType.SESSION_STATUS
    after = await service.get_session(session.session_id)
    assert after.status == SessionStatus.IDLE and after.stop_reason == StopReason.END_TURN
    assert after.snapshot == 1 and after.consumed_seq == 1
    assert any(p.endswith("manifest.json") for p in blobs.objects)
    options = client.options
    assert options.setting_sources == [] and "WebFetch" in options.disallowed_tools
    assert options.max_turns == 20 and options.max_budget_usd == 5.0
    assert options.env["CLAUDE_CODE_USE_VERTEX"] == "1"
    assert list(options.mcp_servers) == ["egress"]  # the definition's connectors, from connector_urls


async def test_denied_tool_never_runs(service, tokens, session, sdk, blobs, tmp_path, monkeypatch):
    sdk.script = Script(turns=[[ToolCall("WebFetch", {"url": "x"}, "t1")]])
    run = make_run(service, tokens, session, sdk, blobs, tmp_path, monkeypatch)
    await run()
    assert sdk.instances[0].executed == []


async def test_approval_parks_then_resumes_and_executes(service, tokens, session, sdk, blobs, tmp_path, monkeypatch, jobs):
    sid = session.session_id
    sdk.script = Script(turns=[[ToolCall("Bash", {"command": "make"}, "t1")]])
    run = make_run(service, tokens, session, sdk, blobs, tmp_path, monkeypatch)
    assert await run() == StopReason.REQUIRES_ACTION
    first = sdk.instances[0]
    assert first.interrupted and first.executed == []
    parked = await service.get_session(sid)
    assert parked.pending_tool_use_ids == ["t1"] and parked.lease is None and parked.snapshot == 1

    await service.confirm(sid, "t1", "allow", actor="bob@example.com")
    resumed = await service.get_session(sid)
    assert resumed.status == SessionStatus.RUNNING and len(jobs.launched) == 2

    # the restarted job: new lease, same tool call re-issued with a new id
    sdk.script = Script(turns=[[ToolCall("Bash", {"command": "make"}, "t2")]], reply="built")
    run2 = make_run(service, tokens, resumed, sdk, blobs, tmp_path, monkeypatch)
    assert await run2() == StopReason.END_TURN
    second = sdk.instances[1]
    assert second.options.resume == "sdk-session-1"
    assert second.prompts == [continuation({"decision": "allow", "tool_name": "Bash"})]
    assert second.executed == ["t2"]
    assert (await service.get_session(sid)).snapshot == 2


async def test_budget_reached_is_reported(service, tokens, session, sdk, blobs, tmp_path, monkeypatch):
    sdk.script = Script(subtype="error_max_turns")
    run = make_run(service, tokens, session, sdk, blobs, tmp_path, monkeypatch)
    assert await run() == StopReason.BUDGET_REACHED
    assert (await service.get_session(session.session_id)).stop_reason == StopReason.BUDGET_REACHED


async def test_terminated_session_stops_the_runner(service, tokens, session, sdk, blobs, tmp_path, monkeypatch):
    await service.terminate(session.session_id, actor="alice@example.com")
    run = make_run(service, tokens, session, sdk, blobs, tmp_path, monkeypatch)
    assert await run() == StopReason.STOPPED
    assert sdk.instances[0].prompts == []


async def test_unreachable_api_denies_fail_closed(tokens, session, sdk, tmp_path, monkeypatch):
    from milos.runner import Gate

    class Down:
        async def permit(self, *_: Any) -> Any:
            raise ConnectionError("down")

    gate = Gate(Down())  # type: ignore[arg-type]
    out = await gate.pre_tool_use({"tool_name": "Read", "tool_input": {}}, "t1", None)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_interrupt_during_turn_reaches_the_client(service, tokens, session, sdk, blobs, tmp_path, monkeypatch):
    """An interrupt queued before the turn starts is seen by the watcher on its first poll."""
    sid = session.session_id
    await service.interrupt(sid, actor="alice@example.com", client_request_id="i1")

    class SlowSDK(sdk):
        async def receive_response(self):
            await asyncio.sleep(0.05)
            async for m in super().receive_response():
                yield m

    SlowSDK.script = Script(reply="slow")
    run = make_run(service, tokens, session, SlowSDK, blobs, tmp_path, monkeypatch)
    await run()
    assert sdk.instances[0].interrupted
