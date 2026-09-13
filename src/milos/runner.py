"""Cloud Run Job entrypoint: `python -m milos.runner`.

One execution runs one session until it has nothing to do. It never writes to
Firestore; everything goes through the internal API with this execution's
lease token. The SDK's own tools (Bash, Read, Write, Edit, Glob, Grep) stay
enabled, and a `PreToolUse` hook sends every call to the API before it runs:
the hook decides, never the model and never the SDK's own permission mode.

    allow                → the tool runs
    deny                 → the model is told why
    require_confirmation → the turn is interrupted, a snapshot is written and
                           the job exits; the API restarts a job when a human
                           decides, and the re-issued call is matched by content
    stop                 → the agent was disabled or the session terminated

Web tools are disabled; the web is reached through a connector where the
request is checked and logged. `setting_sources=[]` keeps repository settings,
hooks and skills out: the definition's system prompt is the only instruction.
"""

import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResult,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    UserMessage,
)
from claude_agent_sdk.types import HookEvent, McpHttpServerConfig, McpServerConfig

from .connector import MCP_PATH
from .control import Control, GoogleIdentity, Identity, auth_headers
from .definitions import FORBIDDEN_TOOLS
from .models import AgentVersion, EventType, StopReason, sha256_text
from .service import RunnerEvent
from .settings import RunnerSettings
from .snapshots import Blobs, GcsBlobs, restore, save

DISALLOWED_TOOLS = [*FORBIDDEN_TOOLS]
RESULT_SUMMARY_CHARS = 2_000


def _hook_output(decision: str, reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }


class Gate:
    """The runner side of tool permission: ask the API, act on the answer."""

    def __init__(self, control: Control) -> None:
        self._control = control
        self.client: Any = None  # set once the SDK client is open
        self.parked: str | None = None  # tool_use_id awaiting a human
        self.stopped = False
        self.denied: list[str] = []

    def hooks(self) -> dict[HookEvent, list[HookMatcher]]:
        # The SDK types the hook input as a union of every event's TypedDict;
        # this hook only ever receives PreToolUse input.
        return {"PreToolUse": [HookMatcher(matcher=None, hooks=[cast(Any, self.pre_tool_use)])]}

    async def pre_tool_use(self, input_data: dict[str, Any], tool_use_id: str | None, _: Any) -> dict[str, Any]:
        tool_name = input_data.get("tool_name", "")
        args = input_data.get("tool_input") or {}
        tool_use_id = tool_use_id or sha256_text(f"{tool_name}:{args}")[:24]
        try:
            decision, reason = await self._control.permit(tool_use_id, tool_name, args)
        except Exception as error:  # the API is unreachable: fail closed
            self.denied.append(tool_use_id)
            return _hook_output("deny", f"permission service unavailable: {error}")
        match decision:
            case "allow":
                return _hook_output("allow", reason)
            case "require_confirmation":
                self.parked = tool_use_id
                await self.interrupt()
                return _hook_output("deny", f"{reason}; this session pauses until a human decides")
            case "stop":
                self.stopped = True
                await self.interrupt()
        self.denied.append(tool_use_id)
        return _hook_output("deny", reason)

    async def can_use_tool(self, tool_name: str, _: dict[str, Any], __: Any) -> PermissionResult:
        """Safety net: nothing the hook did not allow may run."""
        return PermissionResultDeny(message="tool use is decided by the platform hook")

    async def interrupt(self) -> None:
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.interrupt()


def mcp_url(connector_url: str) -> str:
    """The connector's MCP endpoint: the service URL plus the path its server mounts."""
    base = connector_url.rstrip("/")
    return base if base.endswith(MCP_PATH) else base + MCP_PATH


def build_options(
    version: AgentVersion,
    settings: RunnerSettings,
    gate: Gate,
    *,
    resume: str | None,
    connector_headers: dict[str, dict[str, str]],
) -> ClaudeAgentOptions:
    mcp_servers: dict[str, McpServerConfig] = {
        name: McpHttpServerConfig(type="http", url=mcp_url(settings.connector_urls[name]), headers=headers)
        for name, headers in connector_headers.items()
    }
    return ClaudeAgentOptions(
        model=version.model,
        system_prompt=version.system_prompt,
        max_turns=version.max_turns,
        max_budget_usd=version.max_budget_usd,
        disallowed_tools=DISALLOWED_TOOLS,
        setting_sources=[],
        permission_mode="default",
        hooks=gate.hooks(),
        can_use_tool=gate.can_use_tool,
        cwd=settings.work_dir,
        resume=resume,
        mcp_servers=mcp_servers,
        strict_mcp_config=True,
        env=model_env(settings),
    )


def model_env(settings: RunnerSettings) -> dict[str, str]:
    """Where the SDK sends model calls: Vertex AI, or the Anthropic API in development."""
    if settings.anthropic_api_key:
        return {"ANTHROPIC_API_KEY": settings.anthropic_api_key}
    return {
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLOUD_ML_REGION": settings.vertex_region,
        "ANTHROPIC_VERTEX_PROJECT_ID": settings.project,
    }


def continuation(payload: dict[str, Any]) -> str:
    """What the model is told when a run resumes after a human decision."""
    tool = payload.get("tool_name", "the tool")
    if payload.get("timed_out"):
        return f"The approval for {tool} timed out and was denied. Continue without that call."
    if payload.get("decision") == "allow":
        return f"The operator approved the {tool} call. Re-issue it exactly as before and continue."
    return f"The operator denied the {tool} call. Continue without it."


class Run:
    def __init__(
        self,
        settings: RunnerSettings,
        control: Control,
        *,
        blobs: Blobs | None,
        client_factory: Any,
        identity: Identity | None = None,
    ) -> None:
        self.settings = settings
        self.control = control
        self.blobs = blobs
        self.client_factory = client_factory
        self.identity = identity
        self.gate = Gate(control)
        self.sdk_session_id: str | None = None
        self.budget_reached = False
        self.work_dir = Path(settings.work_dir)
        self.transcripts = Path(os.environ.get("HOME", "/home/sandbox")) / ".claude" / "projects"

    async def __call__(self) -> StopReason:
        context = await self.control.context()
        session, version = context.session, context.version
        stop_reason = StopReason.NEEDS_ATTENTION
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            manifest = await self._restore(session.session_id, session.snapshot) or {}
            options = build_options(
                version,
                self.settings,
                self.gate,
                resume=manifest.get("sdk_session_id"),
                connector_headers=await self._connector_headers(version.connectors),
            )
            async with self.client_factory(options) as client:
                self.gate.client = client
                stop_reason = await self._loop(client)
        except Exception as error:
            print(f"run failed: {error}", file=sys.stderr)
        finally:
            # Every exit path releases the lease, crashes included. A snapshot
            # is attempted first; if it fails the previous one stays current.
            with contextlib.suppress(Exception):
                await self._snapshot(session.session_id, session.snapshot + 1)
            await self.control.finish(stop_reason)
        return stop_reason

    def _stopping(self) -> StopReason | None:
        if self.gate.parked:
            return StopReason.REQUIRES_ACTION
        if self.gate.stopped:
            return StopReason.STOPPED
        if self.budget_reached:
            return StopReason.BUDGET_REACHED
        return None

    async def _loop(self, client: Any) -> StopReason:
        idle = 0.0
        while True:
            if reason := self._stopping():
                return reason
            polled = await self.control.poll()
            if polled.stop:
                return StopReason.STOPPED
            if not polled.events:
                if idle >= self.settings.idle_seconds:
                    return StopReason.END_TURN
                await asyncio.sleep(self.settings.poll_seconds)
                idle += self.settings.poll_seconds
                continue
            idle = 0.0
            for event in polled.events:
                if event.type == EventType.USER_MESSAGE:
                    await self._turn(client, event.payload["text"])
                elif event.type == EventType.USER_TOOL_CONFIRMATION:
                    await self._turn(client, continuation(event.payload))
                # user.interrupt between turns has nothing to stop
                await self.control.ack(event.seq)
                if self._stopping():
                    break

    async def _turn(self, client: Any, prompt: str) -> None:
        async with self._watching(client):
            await client.query(prompt)
            async for message in client.receive_response():
                await self._record(message)

    async def _record(self, message: Any) -> None:
        """Journal what the model said, what its tools returned and what the turn cost."""
        match message:
            case AssistantMessage(content=blocks):
                if text := "\n".join(b.text for b in blocks if isinstance(b, TextBlock) and b.text):
                    await self.control.report([RunnerEvent(EventType.AGENT_MESSAGE, {"text": text})])
            case UserMessage(content=list() as blocks):
                results = [
                    RunnerEvent(
                        EventType.TOOL_RESULT,
                        {"outcome": "failed" if b.is_error else "succeeded", "summary": _summary(b.content)},
                        tool_use_id=b.tool_use_id,
                    )
                    for b in blocks
                    if isinstance(b, ToolResultBlock)
                ]
                await self.control.report(results)
            case ResultMessage():
                self.sdk_session_id = message.session_id
                if "max_turns" in message.subtype or "budget" in message.subtype:
                    self.budget_reached = True
                usage = {
                    "subtype": message.subtype,
                    "num_turns": message.num_turns,
                    "duration_ms": message.duration_ms,
                    "total_cost_usd": message.total_cost_usd,
                    "terminal_reason": message.terminal_reason,
                }
                await self.control.report([RunnerEvent(EventType.SESSION_USAGE, usage)])

    @contextlib.asynccontextmanager
    async def _watching(self, client: Any) -> AsyncIterator[None]:
        watcher = asyncio.create_task(self._watch(client))
        try:
            yield
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    async def _watch(self, client: Any) -> None:
        """While a turn runs: forward interrupts and the stop signal, keep the lease warm."""
        while True:
            await asyncio.sleep(self.settings.poll_seconds)
            try:
                polled = await self.control.poll()
            except Exception:
                continue
            if polled.stop:
                self.gate.stopped = True
                await self.gate.interrupt()
                return
            if any(e.type == EventType.USER_INTERRUPT for e in polled.events):
                await self.gate.interrupt()

    async def _restore(self, session_id: str, number: int) -> dict[str, Any] | None:
        if not (self.blobs and number):
            return None
        return await restore(self.blobs, session_id, number, work_dir=self.work_dir, transcripts=self.transcripts)

    async def _snapshot(self, session_id: str, number: int) -> None:
        if not self.blobs:
            return
        manifest = {"sdk_session_id": self.sdk_session_id, "runner_id": self.settings.runner_id}
        await save(self.blobs, session_id, number, work_dir=self.work_dir, transcripts=self.transcripts, manifest=manifest)
        await self.control.advance_snapshot(number)

    async def _connector_headers(self, names: list[str]) -> dict[str, dict[str, str]]:
        session = {"X-Milos-Session": self.settings.session_token}
        return {
            name: await auth_headers(self.identity, url, session)
            for name in names
            if (url := self.settings.connector_urls.get(name))
        }


def _summary(content: Any) -> str:
    match content:
        case str():
            text = content
        case list():
            text = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        case _:
            text = ""
    return text[:RESULT_SUMMARY_CHARS]


async def main() -> int:
    settings = RunnerSettings.from_env()
    identity = GoogleIdentity()
    blobs = GcsBlobs(settings.snapshot_bucket, project=settings.project) if settings.snapshot_bucket else None
    async with Control(
        settings.api_url,
        session_id=settings.session_id,
        session_token=settings.session_token,
        lease_token=settings.lease_token,
        identity=identity,
    ) as control:
        reason = await Run(settings, control, blobs=blobs, client_factory=ClaudeSDKClient, identity=identity)()
    return 0 if reason != StopReason.NEEDS_ATTENTION else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
