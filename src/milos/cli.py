"""`milos` — the command line for operators, approvers and administrators.

Everything goes through the public API (`MILOS_API_URL`, IAP identity token
from `MILOS_ID_TOKEN` or gcloud). Publishing and enabling definitions are
admin routes; CI calls them with the admin service account's token.
"""

import argparse
import asyncio
import inspect
import json
import os
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import httpx

from .client import ApiError, Client
from .errors import Invalid, MilosError
from .models import (
    AgentVersion,
    Event,
    EventType,
    Published,
    SessionStatus,
    SessionView,
    StopReason,
    Verdict,
    workspace_dataset,
)


def _load_dotenv(path: str = ".env") -> None:
    """`KEY=value` lines from `.env` in the working directory; the environment wins."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        key, separator, value = line.strip().partition("=")
        if separator and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _client() -> Client:
    try:
        return Client.from_env()
    except KeyError as error:
        raise SystemExit(f"{error.args[0]} is not set; copy .env.example to .env and fill it in") from None


# --- printing ---------------------------------------------------------------------


def _args(payload: dict[str, Any], limit: int = 100) -> str:
    text = json.dumps(payload.get("args", {}), default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _print_event(event: Event) -> None:
    payload = event.payload
    match event.type:
        case EventType.USER_MESSAGE | EventType.AGENT_MESSAGE:
            body = payload.get("text", "")
        case EventType.AGENT_TOOL_USE:
            body = f"{payload.get('tool_name')} {_args(payload)} → {payload.get('outcome')} ({payload.get('reason')})"
        case EventType.TOOL_RESULT:
            body = f"{payload.get('outcome')}: {payload.get('summary', '')[:120]}"
        case EventType.SESSION_STATUS:
            body = f"{payload.get('status')} {payload.get('stop_reason') or ''}".strip()
        case EventType.USER_APPROVAL:
            body = f"{payload.get('verdict')} {payload.get('tool_name')}"
        case _:
            body = json.dumps(payload, default=str)[:200]
    when = event.created_at.astimezone().strftime("%H:%M:%S")
    print(f"{event.seq:>4}  {when}  {event.type.value:<24} {event.actor:<20} {body}")


def _print_session(s: SessionView) -> None:
    pending = f" pending={','.join(c.tool_use_id for c in s.pending)}" if s.pending else ""
    when = s.created_at.strftime("%Y-%m-%d %H:%M")
    print(f"{s.session_id}  {when}  {s.agent_id:<16} {s.status.value:<12} {s.stop_reason or ''}{pending}")


def _print_call(session: SessionView, request: Event) -> None:
    print(f"{session.session_id}  {session.agent_id}  by {session.operator}")
    print(f"  {request.payload.get('tool_name')} {_args(request.payload, limit=400)}")
    print(f"  milos allow {session.session_id} {request.tool_use_id}  |  milos deny {session.session_id} {request.tool_use_id}")


def _print_next_step(session: SessionView) -> None:
    """After a session stops, say what moves it on."""
    if session.stop_reason == StopReason.END_TURN:
        print(f'idle: milos send {session.session_id} "..."')
    elif session.status == SessionStatus.TERMINATED:
        print("terminated")
    elif session.stop_reason:
        print(f"stopped: {session.stop_reason.value}")


COLUMNS: tuple[tuple[str, Callable[[Published], str]], ...] = (
    ("Agent", lambda p: p.agent.agent_id),
    ("Version", lambda p: str(p.version.version)),
    ("Enabled", lambda p: "yes" if p.agent.enabled else "no"),
    ("Purpose", lambda p: p.version.purpose),
    ("Owner", lambda p: p.version.owner),
    ("Data classes", lambda p: ", ".join(p.version.data_classes)),
    ("Tools", lambda p: ", ".join(p.version.allowed_tools)),
    ("Needs approval", lambda p: ", ".join(p.version.approval_required) or "—"),
    ("BigQuery", lambda p: ", ".join(_bigquery(p.version)) or "—"),
    ("Runner SA", lambda p: p.version.runner_sa),
)


def _bigquery(version: AgentVersion) -> list[str]:
    """What the agent reaches in BigQuery: the shared datasets and its workspace."""
    return [*version.datasets, *([workspace_dataset(version.agent_id) + " (workspace)"] if version.workspace else [])]


def registry(published: list[Published]) -> str:
    """The AI usage register as Markdown, generated from what is published rather than maintained by hand."""

    def line(cells: Iterable[str]) -> str:
        return "| " + " | ".join(cells) + " |"

    header = line(heading for heading, _ in COLUMNS)
    rule = line("---" for _ in COLUMNS)
    return "\n".join([header, rule, *(line(cell(p) for _, cell in COLUMNS) for p in published)]) + "\n"


# --- following a session --------------------------------------------------------


async def _pending_calls(client: Client, session: SessionView) -> list[tuple[SessionView, Event]]:
    """The tool requests a session is waiting on, with their arguments."""
    requests = {e.tool_use_id: e for e in await client.events(session.session_id) if e.type == EventType.AGENT_TOOL_USE}
    return [(session, requests[c.tool_use_id]) for c in session.pending if c.tool_use_id in requests]


async def _inbox(client: Client, session_id: str | None = None) -> list[tuple[SessionView, Event]]:
    """Every tool call waiting on the caller, or those of one session."""
    sessions = [await client.session(session_id)] if session_id else await client.sessions(role="approver")
    return [call for s in sessions if s.pending for call in await _pending_calls(client, s)]


async def _print_waiting(client: Client, session_id: str) -> None:
    session = await client.session(session_id)
    who = ", ".join(session.approvers) or "someone in the agent's allowed groups"
    for _, request in await _pending_calls(client, session):
        print(f"      waiting for {who} to decide: {request.payload.get('tool_name')} {_args(request.payload)}")
        print(f"      milos allow {session_id} {request.tool_use_id}  |  milos deny {session_id} {request.tool_use_id}")


async def _follow(client: Client, session_id: str, *, after: int = 0, interval: float = 2.0) -> None:
    """Print events as they happen, through approval waits, until the session is idle or terminated."""
    try:
        async for event in client.follow(session_id, after=after, interval=interval):
            _print_event(event)
            if event.type == EventType.SESSION_STATUS and event.payload.get("stop_reason") == StopReason.REQUIRES_ACTION:
                await _print_waiting(client, session_id)
    except asyncio.CancelledError:
        print(f"\ndetached; the session continues: milos events {session_id} --follow")
        raise
    _print_next_step(await client.session(session_id))


# --- commands ------------------------------------------------------------------


async def cmd_run(args: argparse.Namespace) -> int:
    async with _client() as client:
        session = await client.create_session(args.agent, args.message, approvers=args.approver)
        print(session.session_id)
        if args.detach:
            print(f"follow with: milos events {session.session_id} --follow")
        else:
            await _follow(client, session.session_id)
    return 0


async def cmd_send(args: argparse.Namespace) -> int:
    async with _client() as client:
        event = await client.send(args.session, args.text)
    print(f"queued as seq {event.seq}")
    return 0


async def cmd_events(args: argparse.Namespace) -> int:
    async with _client() as client:
        if args.follow:
            await _follow(client, args.session, after=args.after)
        else:
            for event in await client.events(args.session, after=args.after):
                _print_event(event)
    return 0


async def cmd_sessions(args: argparse.Namespace) -> int:
    async with _client() as client:
        for s in await client.sessions(role="approver" if args.approving else "operator"):
            _print_session(s)
    return 0


async def cmd_pending(_: argparse.Namespace) -> int:
    async with _client() as client:
        for session, request in await _inbox(client):
            _print_call(session, request)
    return 0


async def cmd_decide(args: argparse.Namespace) -> int:
    """`milos allow [session] [tool_use_id]`: what is left out is resolved from the inbox when unambiguous."""
    async with _client() as client:
        session_id, tool_use_id = args.session, args.tool_use_id
        if not tool_use_id:
            calls = await _inbox(client, session_id)
            if not calls:
                print("nothing is waiting for your decision", file=sys.stderr)
                return 1
            if len(calls) > 1:
                print(f"{len(calls)} calls are waiting; name the session and tool use id:", file=sys.stderr)
                for session, request in calls:
                    _print_call(session, request)
                return 1
            ((session, request),) = calls
            session_id, tool_use_id = session.session_id, request.tool_use_id or ""
            print(f"{args.verdict}: {request.payload.get('tool_name')} {_args(request.payload)} in {session_id}")
        approval = await client.decide(session_id, tool_use_id, args.verdict)
    print(f"{approval.verdict} by {approval.decided_by}")
    return 0


async def cmd_interrupt(args: argparse.Namespace) -> int:
    async with _client() as client:
        await client.interrupt(args.session)
    return 0


async def cmd_terminate(args: argparse.Namespace) -> int:
    async with _client() as client:
        session = await client.terminate(args.session)
    print(session.status.value)
    return 0


async def cmd_agents_list(_: argparse.Namespace) -> int:
    async with _client() as client:
        for p in await client.agents():
            v = p.version
            state = "enabled" if p.agent.enabled else "disabled"
            print(f"{v.agent_id:<16} v{v.version:<3} {state:<9} {v.purpose}")
            print(f"{'':16} tools: {', '.join(v.allowed_tools)}")
            if v.approval_required:
                print(f"{'':16} needs approval: {', '.join(v.approval_required)}")
    return 0


def cmd_agents_validate(args: argparse.Namespace) -> int:
    failed = 0
    for path in args.files:
        try:
            version = AgentVersion.from_yaml(path)
            print(f"ok    {path}  ({version.agent_id}, {version.definition_sha256[:12]})")
        except Invalid as error:
            failed += 1
            print(f"error {path}: {error}")
    return 1 if failed else 0


async def cmd_agents_publish(args: argparse.Namespace) -> int:
    versions = [AgentVersion.from_yaml(path) for path in args.files]  # every file valid before any is published
    async with _client() as client:
        for version in versions:
            published = await client.publish(version)
            print(f"published {published.agent_id} v{published.version}")
    return 0


async def cmd_agents_registry(_: argparse.Namespace) -> int:
    async with _client() as client:
        print(registry(await client.agents()), end="")
    return 0


async def cmd_agents_enable(args: argparse.Namespace) -> int:
    async with _client() as client:
        agent = await client.set_enabled(args.agent, args.enabled)
    print(f"{agent.agent_id} enabled={agent.enabled}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    # Deferred: the server stack is only needed by this command.
    import uvicorn

    if args.what == "api":
        from .api import build_from_env

        app = build_from_env()
    else:
        from .connector import build_from_env as build_connector

        app = build_connector(args.name)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", args.port)))
    return 0


# --- parser ----------------------------------------------------------------------

type Arg = tuple[Sequence[str], dict[str, Any]]
SESSION: Arg = (("session",), {})
APPROVER_HELP = "who may approve tool calls (repeatable)"

COMMANDS: list[tuple[str, str, list[Arg], Callable[..., Any], dict[str, Any]]] = [
    (
        "run",
        "start a session and follow it",
        [
            (("agent",), {}),
            (("message",), {}),
            (("--approver",), {"action": "append", "default": [], "help": APPROVER_HELP}),
            (("--detach", "-d"), {"action": "store_true", "help": "print the session id and return"}),
        ],
        cmd_run,
        {},
    ),
    ("send", "send a follow-up message", [SESSION, (("text",), {})], cmd_send, {}),
    (
        "events",
        "print a session's events",
        [
            SESSION,
            (("--after",), {"type": int, "default": 0}),
            (("--follow", "-f"), {"action": "store_true"}),
        ],
        cmd_events,
        {},
    ),
    (
        "sessions",
        "list your sessions",
        [
            (("--approving",), {"action": "store_true", "help": "sessions that name you as an approver"}),
        ],
        cmd_sessions,
        {},
    ),
    ("pending", "tool calls waiting for your approval", [], cmd_pending, {}),
    (
        "allow",
        "allow a pending tool call; ids may be left out when only one call is waiting",
        [
            (("session",), {"nargs": "?"}),
            (("tool_use_id",), {"nargs": "?"}),
        ],
        cmd_decide,
        {"verdict": Verdict.ALLOW},
    ),
    (
        "deny",
        "deny a pending tool call; ids may be left out when only one call is waiting",
        [
            (("session",), {"nargs": "?"}),
            (("tool_use_id",), {"nargs": "?"}),
        ],
        cmd_decide,
        {"verdict": Verdict.DENY},
    ),
    ("interrupt", "stop the current turn", [SESSION], cmd_interrupt, {}),
    ("terminate", "end a session for good", [SESSION], cmd_terminate, {}),
]

AGENT_COMMANDS: list[tuple[str, str, list[Arg], Callable[..., Any], dict[str, Any]]] = [
    ("list", "agents you can see, with their tools", [], cmd_agents_list, {}),
    ("validate", "check definition files locally (CI)", [(("files",), {"nargs": "+"})], cmd_agents_validate, {}),
    ("publish", "validate and publish definitions (admin group)", [(("files",), {"nargs": "+"})], cmd_agents_publish, {}),
    ("registry", "print the generated register", [], cmd_agents_registry, {}),
    ("enable", "enable an agent (admin group)", [(("agent",), {})], cmd_agents_enable, {"enabled": True}),
    (
        "disable",
        "disable an agent: every session stops at its next tool request (admin group)",
        [(("agent",), {})],
        cmd_agents_enable,
        {"enabled": False},
    ),
]


def _add(sub: Any, commands: list[tuple[str, str, list[Arg], Callable[..., Any], dict[str, Any]]]) -> None:
    for name, help_, arguments, fn, defaults in commands:
        command = sub.add_parser(name, help=help_)
        for flags, options in arguments:
            command.add_argument(*flags, **options)
        command.set_defaults(fn=fn, **defaults)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="milos", description="Secure agent platform on Google Cloud")
    sub = p.add_subparsers(dest="command", required=True)
    _add(sub, COMMANDS)
    agents = sub.add_parser("agents", help="published definitions").add_subparsers(dest="agents_command", required=True)
    _add(agents, AGENT_COMMANDS)
    serve = sub.add_parser("serve", help="run the API or a connector")
    serve.add_argument("what", choices=["api", "connector"])
    serve.add_argument("--name", default="egress", help="connector name")
    serve.add_argument("--port", default="8080")
    serve.set_defaults(fn=cmd_serve)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    _load_dotenv()
    args = parser().parse_args(argv)
    try:
        result = args.fn(args)
        return int(asyncio.run(result) if inspect.iscoroutine(result) else result)
    except (MilosError, ApiError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except httpx.HTTPError as error:
        print(f"error: cannot reach the API ({error}); check MILOS_API_URL", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
