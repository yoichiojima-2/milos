"""Minimal ops + compliance CLI — the UI milos deliberately doesn't have.

milos run "prompt" [--agent NAME] [--resume sess_...] [--model ...]
                                        one-shot query; streams the response
milos sessions                          list recent sessions
milos sessions show <session_id>        one session's document, as JSON
milos sessions delete <session_id>      remove a session and its journal
milos sessions purge --older-than 30    delete terminated/idle sessions older
                                        than N days (retention; --dry-run)
milos tail <session_id>                 follow a session's journal (messages + audit)
milos rewind <session_id> <event_uuid>  branch the transcript from a past event
milos approvals                         pending approvals across every session
milos approvals <session_id>            pending approvals for one session
milos approvals <session_id> allow <call_hash>
milos approvals <session_id> deny <call_hash> [-m reason]
milos kill <session_id>                 flip the kill switch
milos agents                            list stored agents (the AI risk register)
milos agents create <name> --system-prompt "..." --allow Read \\
    --risk-purpose "..." --risk-impact low --risk-owner me@x --risk-review-by 2027-01-01
milos agents show|update|delete <name>
milos agents revisions <name>           the agent's version history
milos workspaces                        list workspaces, members, and leases
milos workspaces create <name> [--model ...] [--description ...]
milos workspaces show|update|delete <name>
milos workspaces claude-md <name>       print the workspace's CLAUDE.md
milos workspaces claude-md <name> --file p  replace it from a local file
milos skills                            list skills in the bucket
                                        (--workspace lists a workspace's skills)
milos skills push <dir...> [--name X] [--replace]
                                        upload local skill directories (SKILL.md plus resources)
milos skills files <name>               list one skill's files
milos skills cat <name> <file>          print one skill file's content
milos skills sync                       seed skills/ from the official anthropics/skills repo
milos policies                          list policy versions (active one marked)
milos policies apply <policy.yaml>      validate + store the next version, activate it
milos policies show [vNNNNNN]           print one version (default: active)
milos policies diff <v1> <v2>           what changed between two versions
milos evidence export --from 2026-08-01 --to 2026-09-01 [--session sess_...]
                                        write a hashed audit bundle to the evidence bucket
milos evidence verify <export_id>       re-hash a bundle against its manifest
milos incidents                         list AI incident records
milos incidents open <session_id> --reason "..." [--severity high]
milos incidents close <inc_id> --resolution "..."
milos settings                          show global option defaults + active policy
milos settings update [--model ...]     replace global option defaults
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import getpass
import json
import time

from . import env
from .errors import MilosError
from .options import AgentOptions
from .store import Store
from .types import doc_to_message


def _project(args: argparse.Namespace) -> str:
    project = env.find_project(args.project)
    if not project:
        raise SystemExit("set --project or $MILOS_PROJECT")
    return project


def _store(args: argparse.Namespace) -> Store:
    return Store(_project(args))


def _options(args: argparse.Namespace) -> AgentOptions:
    """The installation coordinates a command needs to trigger a run."""
    return AgentOptions(
        project=_project(args),
        region=getattr(args, "region", None),
        job=getattr(args, "job", None),
    )


def _risk(args) -> dict | None:
    values = {
        "purpose": args.risk_purpose,
        "impact": args.risk_impact,
        "owner": args.risk_owner,
        "review_by": args.risk_review_by,
    }
    return {k: v for k, v in values.items() if v is not None} or None


def _run_options(args) -> AgentOptions:
    """The AgentOptions subset worth having on the command line."""
    return AgentOptions(
        model=args.model,
        system_prompt=args.system_prompt,
        allowed_tools=list(args.allow or []),
        permission_mode=args.permission_mode,
        workspace=getattr(args, "workspace", None),
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget_usd,
    )


async def _run(args) -> None:
    from .client import query

    options = _run_options(args)
    options.project = _project(args)
    options.region = args.region
    options.job = args.job
    options.agent = args.agent
    options.resume = args.resume
    options.from_event = args.from_event
    async for message in query(prompt=args.prompt, options=options):
        print(message)


async def _sessions(args) -> None:
    from .store import runtime

    store = _store(args)

    if args.action == "list":
        for session in await store.list_sessions():
            state = runtime(session)
            print(
                f"{session['id']}  {state.get('status'):<10}"
                f"  ${float(session.get('cost_usd') or 0):.4f}"
                f"  {state.get('stop_reason') or '':<14}"
                f"  {session.get('policy_version') or '':<9}"
                f"  {session.get('agent') or ''}"
            )
        return

    if args.action == "show":
        if not args.session_id:
            raise SystemExit("sessions show requires a session id")
        session = await store.get_session(args.session_id)
        if session is None:
            raise SystemExit(f"no such session: {args.session_id}")
        print(json.dumps(session, indent=2, default=str))
        return

    if args.action == "delete":
        if not args.session_id:
            raise SystemExit("sessions delete requires a session id")
        await store.delete_session(args.session_id)
        print(f"deleted {args.session_id}")
        return

    # purge — the code half of the retention schedule (the GCS half is the
    # bucket lifecycle rule in infra/): delete finished sessions past N days.
    if args.older_than is None:
        raise SystemExit("purge requires --older-than DAYS")
    from .evidence import _epoch
    from .store import lease_active

    cutoff = time.time() - args.older_than * 86400
    purged = 0
    for session in await store.list_sessions(limit=None):
        state = runtime(session)
        if lease_active(session) or state.get("status") in ("running", "starting"):
            continue
        if _epoch(session.get("created_at")) >= cutoff:
            continue
        purged += 1
        if args.dry_run:
            print(f"would delete {session['id']}")
        else:
            await store.delete_session(session["id"])
            print(f"deleted {session['id']}")
    print(f"{'would delete' if args.dry_run else 'deleted'} {purged} session(s)")


async def _tail(args) -> None:
    from .journal import active_branch, event_message

    store = _store(args)
    session = await store.get_session(args.session_id)
    if session is None:
        raise SystemExit(f"no such session: {args.session_id}")
    branch = active_branch(session)
    cursor = 0
    while True:
        events = await store.list_events(args.session_id, branch, after=cursor)
        for event in events:
            cursor = int(event["seq"])
            doc = event_message(event)
            if doc is not None:
                print(f"[{cursor}] {doc_to_message(doc)}")
            else:
                # Journal-only records (tool_call, approval, lifecycle): one
                # compact line each — the audit trail inline with the chat.
                payload = json.dumps(event.get("payload") or {}, default=str)
                print(f"[{cursor}] <{event.get('type')}> {payload[:200]}")
        if not events:
            await asyncio.sleep(1.0)


async def _rewind(args) -> None:
    from . import remote

    store = _store(args)
    session = await store.get_session(args.session_id)
    if session is None:
        raise SystemExit(f"no such session: {args.session_id}")
    branch, cursor = await remote.branch_from_event(
        store, args.session_id, session, args.event_uuid
    )
    print(f"rewound {args.session_id} to {args.event_uuid}")
    print(f"    new branch {branch} (cursor {cursor}) — next prompt continues from there")


async def _approvals(args) -> None:
    store = _store(args)
    if args.action == "list":
        if args.session_id:
            pending = await store.list_pending_approvals(args.session_id)
        else:
            pending = await store.list_all_pending_approvals()
        for approval in pending:
            owner = f"{approval['session_id']}  " if "session_id" in approval else ""
            reason = f"  ({approval['reason']})" if approval.get("reason") else ""
            print(f"{owner}{approval['call_hash']}  {approval['tool_name']}{reason}")
            print(f"    {json.dumps(approval.get('input') or {})[:200]}")
        return
    if not args.session_id:
        raise SystemExit("allow/deny require a session id")
    await store.decide_approval(
        args.session_id,
        args.call_hash,
        allow=args.action == "allow",
        decided_by=getpass.getuser(),
        deny_message=args.message if args.action == "deny" else None,
    )
    print(f"{args.action}: {args.call_hash}")


async def _kill(args) -> None:
    store = _store(args)
    await store.update_session(args.session_id, disabled=True)
    print(f"disabled: {args.session_id}")


async def _agents(args) -> None:
    from . import agents

    options = _options(args)
    store = _store(args)

    if args.action == "list":
        today = datetime.date.today().isoformat()
        for agent in await agents.list_all(store=store):
            opts = agent.get("options") or {}
            risk = agent.get("risk") or {}
            review = risk.get("review_by") or ""
            overdue = "  REVIEW OVERDUE" if review and review < today else ""
            print(
                f"{agent['name']:<24}  {opts.get('model') or '-':<24}"
                f"  {risk.get('impact') or '-':<7}  {review or '-':<11}{overdue}"
                f"  {agent.get('description') or ''}"
            )
        return

    if not args.name:
        raise SystemExit(f"agents {args.action} requires a name")

    if args.action == "create":
        await agents.create(
            args.name,
            _run_options(args),
            options=options,
            risk=_risk(args),
            description=args.description,
            created_by=getpass.getuser(),
            store=store,
        )
        print(f"created {args.name}")
        return

    if args.action == "update":
        await agents.update(
            args.name,
            _run_options(args),
            options=options,
            risk=_risk(args),
            description=args.description,
            store=store,
        )
        print(f"updated {args.name}")
        return

    if args.action == "delete":
        await agents.delete(args.name, store=store)
        print(f"deleted {args.name}  (sessions that ran as it keep their options)")
        return

    if args.action == "revisions":
        for revision in await agents.revisions(args.name, store=store):
            opts = revision.get("options") or {}
            print(f"{revision['revision']:>3}  {opts.get('model') or '-':<24}")
        return

    # show
    agent = await agents.get(args.name, store=store)
    if agent is None:
        raise SystemExit(f"no such agent: {args.name}")
    print(
        json.dumps(
            {k: v for k, v in agent.items() if k not in ("created_at", "updated_at")},
            indent=2,
            default=str,
        )
    )


async def _workspaces(args) -> None:
    from . import workspaces
    from .store import lease_active

    store = _store(args)

    if args.action == "list":
        agent_docs = await store.list_agents()
        for doc in sorted(await store.list_workspaces(), key=lambda d: d["name"]):
            busy = lease_active(doc)
            holder = doc.get("lease_session_id") if busy else ""
            opts = doc.get("options") or {}
            member_names = ",".join(workspaces.members(doc["name"], agent_docs))
            print(
                f"{doc['name']:<24}  {'busy' if busy else 'free':<6}"
                f"  {opts.get('model') or '-':<24}  {member_names or '-':<24}"
                f"  {holder or doc.get('description') or ''}"
            )
        return

    if not args.name:
        raise SystemExit(f"workspaces {args.action} requires a name")

    if args.action == "claude-md":
        project = _project(args)
        bucket = env.default_bucket(None, project)
        if args.file:
            from pathlib import Path

            text = Path(args.file).read_text()
            await asyncio.to_thread(workspaces.write_claude_md, project, bucket, args.name, text)
            print(f"wrote CLAUDE.md for workspace {args.name}")
            return
        text = await asyncio.to_thread(workspaces.read_claude_md, project, bucket, args.name)
        if text is None:
            raise SystemExit(f"workspace {args.name} has no CLAUDE.md")
        print(text, end="")
        return

    if args.action == "create":
        workspace = await workspaces.create(
            args.name,
            _run_options(args),
            options=_options(args),
            description=args.description,
            created_by=getpass.getuser(),
            store=store,
        )
        print(f"created workspace {workspace['name']}")
        return
    if args.action == "update":
        await workspaces.update(
            args.name,
            _run_options(args),
            options=_options(args),
            description=args.description,
            store=store,
        )
        print(f"updated workspace {args.name}")
        return
    if args.action == "delete":
        await workspaces.delete(args.name, store=store)
        print(f"deleted workspace {args.name}")
        return

    # show
    workspace = await workspaces.get(args.name, store=store)
    if workspace is None:
        raise SystemExit(f"no such workspace: {args.name}")
    workspace["members"] = workspaces.members(args.name, await store.list_agents())
    print(json.dumps(workspace, indent=2, default=str))


async def _skills(args) -> None:
    from . import skills

    project = _project(args)
    bucket = env.default_bucket(None, project)

    if args.action == "sync":
        if args.workspace:
            # official skills are global by design, and a global skill already
            # mounts into every session — a per-workspace copy would be dead weight
            raise SystemExit(
                "skills sync seeds the global skills/ prefix; --workspace is not supported"
            )
        summary = await asyncio.to_thread(
            skills.sync_official, project, bucket, max_bytes=skills.MAX_SKILL_FILE_BYTES
        )
        print(f"synced {summary['files']} file(s) across {len(summary['skills'])} skill(s)")
        for skipped in summary["skipped"]:
            print(f"    skipped {skipped['skill']}/{skipped['file']} ({skipped['size']} bytes)")
        return

    if args.action == "push":
        from pathlib import Path

        scope = f" in workspace {args.workspace}" if args.workspace else ""
        # one directory per skill, so a glob like ./skills/* pushes them all
        for raw in args.args:
            try:
                summary = await asyncio.to_thread(
                    skills.push,
                    project,
                    bucket,
                    Path(raw),
                    max_bytes=skills.MAX_SKILL_FILE_BYTES,
                    name=args.name,
                    workspace=args.workspace,
                    replace=args.replace,
                )
            except (OSError, ValueError) as exc:
                raise SystemExit(str(exc)) from exc
            pruned = f", pruned {summary['deleted']}" if summary["deleted"] else ""
            print(f"pushed {summary['files']} file(s) to skill {summary['skill']}{scope}{pruned}")
            for skipped in summary["skipped"]:
                why = skipped.get("reason") or f"{skipped['size']} bytes"
                print(f"    skipped {skipped['file']} ({why})")
        return

    if args.action == "files":
        if not args.args:
            raise SystemExit("usage: milos skills files <name>")
        rows = await asyncio.to_thread(skills.files, project, bucket, args.args[0], args.workspace)
        if not rows:
            raise SystemExit(f"no such skill: {args.args[0]}")
        for row in rows:
            updated = row["updated"].strftime("%Y-%m-%d %H:%M") if row["updated"] else ""
            print(f"{row['name']}  {row['size']}  {updated}")
        return

    if args.action == "cat":
        if len(args.args) < 2:
            raise SystemExit("usage: milos skills cat <name> <file>")
        import sys

        try:
            data, _content_type = await asyncio.to_thread(
                skills.read_file,
                project,
                bucket,
                args.args[0],
                args.args[1],
                max_bytes=skills.MAX_SKILL_FILE_BYTES,
                workspace=args.workspace,
            )
        except FileNotFoundError as exc:
            raise SystemExit(f"not found: {exc}") from exc
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        sys.stdout.buffer.write(data)
        return

    # list
    rows = await asyncio.to_thread(skills.stats, project, bucket, args.workspace)
    if args.workspace and not rows:
        print(f"workspace {args.workspace} has no skills")
        return
    for name in sorted(rows):
        row = rows[name]
        print(f"{name:<28}  {row['files']:>4} files  {row['description'] or ''}")


async def _policies(args) -> None:
    from .policy import canonical_hash, policy_from_yaml

    store = _store(args)

    if args.action == "apply":
        if not args.args:
            raise SystemExit("apply requires a policy YAML file")
        from pathlib import Path

        policy = policy_from_yaml(Path(args.args[0]).read_text())
        version = await store.create_policy(
            policy,
            {
                "hash": canonical_hash(policy),
                "applied_by": getpass.getuser(),
                "source": args.args[0],
            },
        )
        print(f"applied {version} (hash {canonical_hash(policy)[:12]}) — now active")
        return

    if args.action == "show":
        if args.args:
            version = args.args[0]
            doc = await store.get_policy(version)
        else:
            doc = await store.get_active_policy()
        if doc is None:
            raise SystemExit("no such policy version" if args.args else "no policy applied")
        print(json.dumps(doc, indent=2, default=str))
        return

    if args.action == "diff":
        if len(args.args) < 2:
            raise SystemExit("diff requires two versions")
        docs = [await store.get_policy(v) for v in args.args[:2]]
        for version, doc in zip(args.args[:2], docs):
            if doc is None:
                raise SystemExit(f"no such policy version: {version}")
        import difflib

        old, new = (json.dumps(d["policy"], indent=2, sort_keys=True) for d in docs)
        for line in difflib.unified_diff(
            old.splitlines(), new.splitlines(), args.args[0], args.args[1], lineterm=""
        ):
            print(line)
        return

    # list
    settings = await store.get_settings() or {}
    active = settings.get("policy_version")
    for doc in await store.list_policies():
        marker = "  * active" if doc.get("version") == active else ""
        print(
            f"{doc.get('version')}  {str(doc.get('hash') or '')[:12]}"
            f"  {doc.get('applied_by') or '-':<12}{marker}"
        )


def _date(value: str) -> float:
    return datetime.datetime.fromisoformat(value).replace(tzinfo=datetime.UTC).timestamp()


async def _evidence(args) -> None:
    from . import evidence

    project = _project(args)
    store = _store(args)
    bucket = evidence.default_bucket(project)

    if args.action == "export":
        if not (args.since and args.until):
            raise SystemExit("export requires --from and --to (YYYY-MM-DD)")
        manifest = await evidence.export(
            store,
            bucket,
            start=_date(args.since),
            end=_date(args.until),
            session_id=args.session,
            generated_by=getpass.getuser(),
        )
        bucket_name = env.default_evidence_bucket(None, project)
        print(f"exported gs://{bucket_name}/exports/{manifest['export_id']}/")
        for name, meta in manifest["files"].items():
            print(f"    {name:<16}  {meta['records']:>6} records  {meta['sha256'][:12]}")
        print(f"    bundle_hash {manifest['bundle_hash']}")
        return

    if not args.args:
        raise SystemExit("verify requires an export id")
    manifest = await asyncio.to_thread(evidence.verify, bucket, args.args[0])
    print(f"verified {args.args[0]}: bundle_hash {manifest['bundle_hash']}")


async def _incidents(args) -> None:
    from . import incidents

    store = _store(args)

    if args.action == "open":
        if not args.id or not args.reason:
            raise SystemExit("open requires a session id and --reason")
        incident = await incidents.open_incident(
            args.id,
            args.reason,
            severity=args.severity,
            opened_by=getpass.getuser(),
            store=store,
        )
        print(f"opened {incident['id']} on {args.id}")
        return

    if args.action == "close":
        if not args.id or not args.resolution:
            raise SystemExit("close requires an incident id and --resolution")
        await incidents.close_incident(
            args.id, args.resolution, closed_by=getpass.getuser(), store=store
        )
        print(f"closed {args.id}")
        return

    for incident in await incidents.list_all(store=store):
        print(
            f"{incident['id']}  {incident.get('status'):<7}  {incident.get('severity'):<7}"
            f"  {incident.get('session_id')}  {incident.get('reason') or ''}"
        )


async def _settings(args) -> None:
    from .options import DEFAULT_MODEL, options_from_doc

    store = _store(args)
    if args.action == "update":
        run_options = _run_options(args)
        run_options.validate()
        settings = await store.get_settings() or {}
        # merge=False would drop the policy pointer the settings doc also holds
        await store.update_settings({**settings, "options": run_options.serialize()})
        print("updated global settings")
        return
    settings = await store.get_settings()
    if not settings:
        print(f"no global settings stored (built-in default: model {DEFAULT_MODEL})")
        return
    parsed = options_from_doc(dict(settings.get("options") or {}))
    print(json.dumps(parsed.serialize(), indent=2, default=str))
    print(f"active policy: {settings.get('policy_version') or 'none applied'}")


def _run_option_flags(parser: argparse.ArgumentParser) -> None:
    """The shared run-option flags: the AgentOptions subset worth typing."""
    parser.add_argument("--model", default=None)
    parser.add_argument("--system-prompt", default=None)
    parser.add_argument("--allow", action="append", metavar="TOOL", help="repeatable")
    parser.add_argument("--permission-mode", default=None)
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--max-turns", type=int, default=None)
    parser.add_argument("--max-budget-usd", type=float, default=None)


def _risk_flags(parser: argparse.ArgumentParser) -> None:
    """The AI risk block (ISO 42001) an agent carries."""
    parser.add_argument("--risk-purpose", default=None)
    parser.add_argument("--risk-impact", default=None, choices=["low", "medium", "high"])
    parser.add_argument("--risk-owner", default=None)
    parser.add_argument("--risk-review-by", default=None, metavar="YYYY-MM-DD")


def main() -> None:
    parser = argparse.ArgumentParser(prog="milos")
    parser.add_argument("--project", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="one-shot query; streams the response")
    run.add_argument("prompt")
    run.add_argument("--agent", default=None)
    run.add_argument("--resume", default=None, metavar="SESSION_ID")
    run.add_argument("--from-event", default=None, metavar="EVENT_UUID")
    run.add_argument("--region", default=None)
    run.add_argument("--job", default=None)
    _run_option_flags(run)
    run.set_defaults(func=_run)

    sessions = sub.add_parser("sessions")
    sessions.add_argument(
        "action", nargs="?", default="list", choices=["list", "show", "delete", "purge"]
    )
    sessions.add_argument("session_id", nargs="?")
    sessions.add_argument(
        "--older-than", type=float, default=None, metavar="DAYS", help="purge: age threshold"
    )
    sessions.add_argument("--dry-run", action="store_true")
    sessions.set_defaults(func=_sessions)

    tail = sub.add_parser("tail")
    tail.add_argument("session_id")
    tail.set_defaults(func=_tail)

    rewind = sub.add_parser("rewind", help="branch a session's transcript from a past event")
    rewind.add_argument("session_id")
    rewind.add_argument("event_uuid")
    rewind.set_defaults(func=_rewind)

    approvals = sub.add_parser("approvals")
    approvals.add_argument("session_id", nargs="?")
    approvals.add_argument("action", nargs="?", default="list", choices=["list", "allow", "deny"])
    approvals.add_argument("call_hash", nargs="?")
    approvals.add_argument("-m", "--message", default=None)
    approvals.set_defaults(func=_approvals)

    kill = sub.add_parser("kill")
    kill.add_argument("session_id")
    kill.set_defaults(func=_kill)

    agents = sub.add_parser("agents")
    agents.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "create", "show", "update", "delete", "revisions"],
    )
    agents.add_argument("name", nargs="?")
    agents.add_argument("--description", default=None)
    _run_option_flags(agents)
    _risk_flags(agents)
    agents.set_defaults(func=_agents)

    workspaces = sub.add_parser("workspaces")
    workspaces.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "create", "show", "update", "delete", "claude-md"],
    )
    workspaces.add_argument("name", nargs="?")
    workspaces.add_argument("--description", default=None)
    workspaces.add_argument("--file", default=None, help="claude-md: local file to upload")
    _run_option_flags(workspaces)
    workspaces.set_defaults(func=_workspaces)

    skills = sub.add_parser("skills")
    skills.add_argument(
        "action", nargs="?", default="list", choices=["list", "push", "files", "cat", "sync"]
    )
    skills.add_argument("args", nargs="*")
    skills.add_argument("--workspace", default=None, help="operate on a workspace's skills")
    skills.add_argument(
        "--name", default=None, help="push: skill name (default: the directory's basename)"
    )
    skills.add_argument(
        "--replace",
        action="store_true",
        help="push: after uploading, delete bucket files the directory no longer "
        "carries (files skipped for size are kept)",
    )
    skills.set_defaults(func=_skills)

    policies = sub.add_parser("policies", help="org policy versions — apply/show/diff")
    policies.add_argument(
        "action", nargs="?", default="list", choices=["list", "apply", "show", "diff"]
    )
    policies.add_argument("args", nargs="*")
    policies.set_defaults(func=_policies)

    evidence = sub.add_parser("evidence", help="hashed audit bundles for the evidence bucket")
    evidence.add_argument("action", choices=["export", "verify"])
    evidence.add_argument("args", nargs="*", help="verify: the export id")
    evidence.add_argument("--from", dest="since", default=None, metavar="YYYY-MM-DD")
    evidence.add_argument("--to", dest="until", default=None, metavar="YYYY-MM-DD")
    evidence.add_argument("--session", default=None, help="export one session only")
    evidence.set_defaults(func=_evidence)

    incidents = sub.add_parser("incidents", help="AI incident records (ISO 42001)")
    incidents.add_argument("action", nargs="?", default="list", choices=["list", "open", "close"])
    incidents.add_argument("id", nargs="?", help="open: session id; close: incident id")
    incidents.add_argument("--reason", default=None)
    incidents.add_argument("--severity", default="medium", choices=["low", "medium", "high"])
    incidents.add_argument("--resolution", default=None)
    incidents.set_defaults(func=_incidents)

    settings = sub.add_parser("settings")
    settings.add_argument("action", nargs="?", default="show", choices=["show", "update"])
    _run_option_flags(settings)
    settings.set_defaults(func=_settings)

    args = parser.parse_args()
    if getattr(args, "action", None) in ("allow", "deny") and not getattr(args, "call_hash", None):
        parser.error("allow/deny require a call_hash")
    if args.command == "skills" and args.action == "push":
        if not args.args:
            parser.error("push requires a skill directory")
        if args.name and len(args.args) > 1:
            parser.error("--name names one skill, so it takes a single directory")
    try:
        asyncio.run(args.func(args))
    except KeyboardInterrupt:
        pass
    except MilosError as exc:
        # Bad policy YAML, unknown agent, unusable options: the user's problem
        # to fix, not a bug — print the message, skip the traceback.
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
