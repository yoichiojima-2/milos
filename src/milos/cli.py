"""`milos` — the command line for operators and CI.

Session commands talk to the public API (`MILOS_API_URL`, IAP identity token
from `MILOS_ID_TOKEN` or gcloud). Agent commands talk to Firestore directly
and are meant for CI, which publishes validated definitions.
"""

import argparse
import asyncio
import inspect
import json
import os
import sys
from collections.abc import Sequence

from . import definitions
from .audit import StderrAuditLog
from .auth import SessionTokens
from .client import Client, id_token
from .errors import Invalid, MilosError
from .jobs import NoJobs
from .models import Event, EventType
from .service import Service
from .store import FirestoreStore


def _client() -> Client:
    url = os.environ.get("MILOS_API_URL")
    if not url:
        raise SystemExit("MILOS_API_URL is not set")
    return Client(url, token=id_token(os.environ.get("MILOS_IAP_CLIENT_ID")))


def _service() -> Service:
    project = os.environ.get("MILOS_PROJECT")
    if not project:
        raise SystemExit("MILOS_PROJECT is not set")
    return Service(FirestoreStore(project=project), StderrAuditLog(), NoJobs(), SessionTokens("cli"))


def _print_event(event: Event) -> None:
    payload = event.payload
    match event.type:
        case EventType.USER_MESSAGE | EventType.AGENT_MESSAGE:
            body = payload.get("text", "")
        case EventType.AGENT_TOOL_USE:
            body = f"{payload.get('tool_name')} → {payload.get('decision')} ({payload.get('reason')})"
        case EventType.TOOL_RESULT:
            body = f"{payload.get('outcome')}: {payload.get('summary', '')[:120]}"
        case EventType.SESSION_STATUS:
            body = f"{payload.get('status')} {payload.get('stop_reason') or ''}".strip()
        case EventType.USER_TOOL_CONFIRMATION:
            body = f"{payload.get('decision')} {payload.get('tool_name')}"
        case _:
            body = json.dumps(payload, default=str)[:200]
    print(f"{event.seq:>4}  {event.type.value:<24} {event.actor:<20} {body}")


async def _follow(client: Client, session_id: str, *, after: int = 0) -> None:
    async for event in client.follow(session_id, after=after):
        _print_event(event)


# --- commands ------------------------------------------------------------------


async def cmd_run(args: argparse.Namespace) -> int:
    async with _client() as client:
        session = await client.create_session(args.agent, args.message, approvers=args.approver)
        print(session.session_id)
        if args.follow:
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


async def cmd_sessions(_: argparse.Namespace) -> int:
    async with _client() as client:
        for s in await client.sessions():
            pending = f" pending={','.join(s.pending_tool_use_ids)}" if s.pending_tool_use_ids else ""
            print(f"{s.session_id}  {s.agent_id:<16} {s.status.value:<12} {s.stop_reason or ''}{pending}")
    return 0


async def cmd_confirm(args: argparse.Namespace) -> int:
    async with _client() as client:
        approval = await client.confirm(args.session, args.tool_use_id, args.decision)
    print(f"{approval['decision']} by {approval['decided_by']}")
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


def cmd_agents_validate(args: argparse.Namespace) -> int:
    failed = 0
    for path in args.files:
        try:
            version = definitions.load(path)
            print(f"ok    {path}  ({version.agent_id}, {version.definition_sha256[:12]})")
        except Invalid as error:
            failed += 1
            print(f"error {path}: {error}")
    return 1 if failed else 0


async def cmd_agents_publish(args: argparse.Namespace) -> int:
    service = _service()
    for path in args.files:
        published = await service.publish(definitions.load(path))
        print(f"published {published.agent_id} v{published.version}")
    return 0


async def cmd_agents_registry(_: argparse.Namespace) -> int:
    print(definitions.registry(await _service().list_agents()), end="")
    return 0


async def cmd_agents_enable(args: argparse.Namespace) -> int:
    agent = await _service().set_enabled(args.agent, args.enabled)
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


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="milos", description="Secure agent platform on Google Cloud")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start a session")
    run.add_argument("agent")
    run.add_argument("message")
    run.add_argument("--approver", action="append", default=[], help="who may approve tool calls")
    run.add_argument("--follow", "-f", action="store_true")
    run.set_defaults(fn=cmd_run)

    send = sub.add_parser("send", help="send a follow-up message")
    send.add_argument("session")
    send.add_argument("text")
    send.set_defaults(fn=cmd_send)

    events = sub.add_parser("events", help="print a session's events")
    events.add_argument("session")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--follow", "-f", action="store_true")
    events.set_defaults(fn=cmd_events)

    sub.add_parser("sessions", help="list your sessions").set_defaults(fn=cmd_sessions)

    for decision in ("allow", "deny"):
        c = sub.add_parser(decision, help=f"{decision} a pending tool call")
        c.add_argument("session")
        c.add_argument("tool_use_id")
        c.set_defaults(fn=cmd_confirm, decision=decision)

    interrupt = sub.add_parser("interrupt", help="stop the current turn")
    interrupt.add_argument("session")
    interrupt.set_defaults(fn=cmd_interrupt)

    terminate = sub.add_parser("terminate", help="end a session for good")
    terminate.add_argument("session")
    terminate.set_defaults(fn=cmd_terminate)

    agents = sub.add_parser("agents", help="definitions (CI)").add_subparsers(dest="agents_command", required=True)
    validate = agents.add_parser("validate")
    validate.add_argument("files", nargs="+")
    validate.set_defaults(fn=cmd_agents_validate)
    publish = agents.add_parser("publish")
    publish.add_argument("files", nargs="+")
    publish.set_defaults(fn=cmd_agents_publish)
    agents.add_parser("registry").set_defaults(fn=cmd_agents_registry)
    for name, enabled in (("enable", True), ("disable", False)):
        toggle = agents.add_parser(name)
        toggle.add_argument("agent")
        toggle.set_defaults(fn=cmd_agents_enable, enabled=enabled)

    serve = sub.add_parser("serve", help="run the API or a connector")
    serve.add_argument("what", choices=["api", "connector"])
    serve.add_argument("--name", default="egress", help="connector name")
    serve.add_argument("--port", default="8080")
    serve.set_defaults(fn=cmd_serve)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = args.fn(args)
        return int(asyncio.run(result) if inspect.iscoroutine(result) else result)
    except MilosError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
