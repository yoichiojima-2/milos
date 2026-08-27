"""Sandbox entrypoint: `python -m milos.runner <session_id>`.

Claims the session lease, loads the policy the session was admitted under
(pinned by version — fail closed if it can't be read), restores state from
GCS, runs the claude_agent_sdk harness on the deployment's model backend with
the governance gate wired in, appends typed journal records (messages,
prompts, lifecycle — the gate adds tool_call/approval on the same writer) to
the session's active branch, then checkpoints and exits 0 (scale to zero).
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from typing import Any

from claude_agent_sdk import ClaudeSDKClient

from . import env, layout, remote, state
from .errors import PolicyError
from .gate import Gate
from .journal import MAIN_BRANCH, JournalWriter, active_branch, build_context, git_info
from .options import AgentOptions, build_sdk_options, model_env, options_from_doc
from .store import Store, is_dead
from .types import ResultMessage, SystemMessage, message_to_doc

INTERRUPT_POLL_SECONDS = 2.0
INBOX_POLL_SECONDS = 2.0

# System subtypes the CLI streams as per-chunk progress noise. Mirroring them
# would put one Firestore write per thinking chunk in the feed; the actual
# thinking text still arrives in the assistant message.
NOISY_SYSTEM_SUBTYPES = {"thinking_tokens"}


async def _watch_interrupt(store: Store, session_id: str, client: ClaudeSDKClient) -> None:
    """Background task: forward queued interrupts to the harness.

    Swallows transient store errors — a Firestore blip must not take down the
    run it is only supposed to be able to pause.
    """
    while True:
        try:
            if await store.take_interrupt(session_id):
                await client.interrupt()
        except Exception:
            pass
        await asyncio.sleep(INTERRUPT_POLL_SECONDS)


async def _handoff_queued(store: Store, config: env.RunnerEnv, session_id: str) -> None:
    """Trigger a fresh execution if this run released with mail still queued.

    The message loop closes before the shutdown tail (checkpointing), which
    takes seconds — and a prompt arriving in that window sees a live lease, so
    the sender triggers nothing and this run never looks at the inbox again.
    Never fatal, and never called on the failure path: re-triggering a run
    that just died is a loop, not a recovery.
    """
    try:
        if await store.peek_messages(session_id):
            await remote.ensure_running(store, session_id, AgentOptions(project=config.project))
    except Exception as error:
        print(f"handoff failed for {session_id}: {error}", file=sys.stderr)


async def _wait_for_messages(
    store: Store, session_id: str, stay_alive: float
) -> list[dict[str, Any]]:
    """Poll the inbox for up to stay_alive seconds of idleness.

    An empty return means "exit now": the idle window lapsed with no input, or
    the session was killed/terminated. The client re-triggers the job when the
    next prompt arrives, so exiting is cheap — that's the scale-to-zero deal.

    Peeks rather than consumes: the caller marks each message consumed only
    once its journal record is written, so this must not be called by anyone
    who won't.
    """
    waited = 0.0
    while waited <= stay_alive:
        messages = await store.peek_messages(session_id)
        if messages:
            return messages
        session = await store.get_session(session_id)
        if is_dead(session):
            return []
        await asyncio.sleep(INBOX_POLL_SECONDS)
        waited += INBOX_POLL_SECONDS
    return []


async def _heartbeat(
    store: Store,
    session_id: str,
    lease_id: str,
    workspace_ref: dict[str, str | None],
    *,
    ttl: float,
    interval: float,
    lost: asyncio.Event,
) -> None:
    """Background task: keep the session (and shared workspace) lease alive.

    A failed renewal means the lease was stolen or the session was killed —
    set the lost flag so the run stops instead of writing over the new owner.
    workspace_ref["name"] is filled in by run() only once the workspace lease
    is actually held, so the heartbeat never claims a workspace the run is
    still allowed to lose to a busy check. Transient store errors are
    swallowed: the ttl leaves slack for a retry.
    """
    while not lost.is_set():
        await asyncio.sleep(interval)
        try:
            alive = await store.renew_lease(session_id, lease_id, ttl)
            if alive and workspace_ref.get("name"):
                alive = await store.claim_workspace(workspace_ref["name"], session_id, ttl)
        except Exception:
            continue
        if not alive:
            lost.set()


async def _recover_cursor(
    store: Store, session_id: str, session: dict[str, Any], branch: str
) -> tuple[dict[str, Any], int, str | None, bool]:
    """Seed the journal cursor from the journal itself, never from the advisory
    seq_head: after a mid-turn crash the doc lags the records, and trusting it
    would re-issue seqs. A fresh branch has no records yet, so its base
    (written by create_branch) is the floor. Returns (branch_info, seq,
    tip_uuid, fresh_branch); a fresh rewind branch — nothing past its
    branch_created record — is the one claim that must fork the SDK session
    instead of resuming it (see build_sdk_options in run())."""
    branch_info = (session.get("branches") or {}).get(branch) or {}
    seq, tip_uuid = await store.recover_head(session_id, branch)
    base_seq = int(branch_info.get("base_seq") or 0)
    if seq < base_seq:
        seq, tip_uuid = base_seq, branch_info.get("base_uuid")
    fresh_branch = branch != MAIN_BRANCH and seq <= base_seq + 1
    return branch_info, seq, tip_uuid, fresh_branch


async def _load_pinned_policy(store: Store, session: dict[str, Any]) -> dict[str, Any]:
    """The policy the session was admitted under, by its stamped version.

    Pinned, not "current": what the gate enforces and what the session doc
    (and every journal record) says was in force must be the same document,
    resumes included. Missing stamp or missing doc fails closed — a session
    that cannot prove its rules does not run.
    """
    version = session.get("policy_version")
    if not version:
        raise PolicyError("session has no policy_version stamp — refusing to run ungoverned")
    doc = await store.get_policy(version)
    if doc is None:
        raise PolicyError(f"policy {version} not found — refusing to run ungoverned")
    return doc


async def run(session_id: str) -> None:
    config = env.RunnerEnv.from_env()

    store = Store(config.project)
    lease_id = uuid.uuid4().hex
    session = await store.claim_session(session_id, lease_id, config.lease_ttl)
    if session is None:
        return  # another execution holds the lease, or the session is gone/terminated

    options = options_from_doc(dict(session["options"]))
    options.project = config.project

    branch = active_branch(session)
    branch_info, seq, tip_uuid, fresh_branch = await _recover_cursor(
        store, session_id, session, branch
    )

    ws, home = config.work_dir / "ws", config.work_dir / "home"
    writer = JournalWriter(
        store,
        session_id,
        branch=branch,
        seq=seq,
        tip_uuid=tip_uuid,
        context=build_context(
            cwd=str(ws),
            model=options.model,
            permission_mode=options.permission_mode,
            workspace=options.workspace,
            lease_id=lease_id,
            claude_session_id=session.get("claude_session_id"),
            policy_version=session.get("policy_version"),
            policy_hash=session.get("policy_hash"),
        ),
    )
    await writer.append("lifecycle", {"event": "claimed", "lease_id": lease_id})

    # The heartbeat spans the whole leased run — GCS restore and checkpoint
    # included: with a short renewable ttl those phases alone can outlive
    # one lease.
    lost = asyncio.Event()
    leased_workspace: dict[str, str | None] = {"name": None}
    beat = asyncio.create_task(
        _heartbeat(
            store,
            session_id,
            lease_id,
            leased_workspace,
            ttl=config.lease_ttl,
            interval=config.heartbeat,
            lost=lost,
        )
    )

    async def fail_fast(
        reason: str, payload: dict, *, result: str | None = None, release_workspace: bool = False
    ) -> None:
        """Release everything with a zero-cost error result and no agent run."""
        await writer.append("lifecycle", {"event": reason, **payload})
        doc = message_to_doc(
            ResultMessage(
                subtype=reason,
                duration_ms=0,
                duration_api_ms=0,
                is_error=True,
                num_turns=0,
                session_id=session.get("claude_session_id") or "",
                total_cost_usd=0.0,
                result=result,
            )
        )
        await writer.append("message", doc)
        if release_workspace and options.workspace:
            await store.release_workspace(options.workspace, session_id)
        await store.release_session(
            session_id,
            status="idle",
            stop_reason=reason,
            seq_head=writer.seq,
            tip_uuid=writer.tip_uuid,
        )

    try:
        # The policy loads before any restore work — and before the workspace
        # claim: a session that cannot prove its rules fails fast without ever
        # consuming the one contended resource.
        try:
            policy_doc = await _load_pinned_policy(store, session)
        except PolicyError as error:
            await fail_fast("policy_error", {"error": str(error)}, result=str(error))
            return

        if options.workspace and not await store.claim_workspace(
            options.workspace, session_id, config.lease_ttl
        ):
            # Another session is live in this workspace: fail fast with an
            # error result so the waiting client terminates. The prompt stays
            # queued in the inbox and is consumed when the session is
            # re-triggered.
            await fail_fast("workspace_busy", {"workspace": options.workspace})
            return
        if options.workspace:
            leased_workspace["name"] = options.workspace  # heartbeat renews it from here on

        ws_prefix = (
            layout.workspace_prefix(options.workspace)
            if options.workspace
            else layout.session_prefix(session_id, "ws")
        )
        home_prefix = layout.session_prefix(session_id, "home")
        ws.mkdir(parents=True, exist_ok=True)
        home.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(state.restore, config.project, config.bucket, ws_prefix, ws)
        await asyncio.to_thread(state.restore, config.project, config.bucket, home_prefix, home)
        # Skills mount into HOME after the home restore, so the live prefixes
        # win over anything a stale checkpoint might carry: global skills for
        # every run, then the workspace's own — restored second, so a
        # workspace skill shadows a same-named global one. The SDK finds them
        # via setting_sources=["user"].
        await asyncio.to_thread(
            state.restore,
            config.project,
            config.bucket,
            layout.skills_root(),
            home / ".claude" / "skills",
        )
        if options.workspace:
            await asyncio.to_thread(
                state.restore,
                config.project,
                config.bucket,
                layout.skills_root(options.workspace),
                home / ".claude" / "skills",
            )

        # The working directory is restored now, so the context snapshot can
        # carry its git state; read once, not per event.
        writer.context["git"] = git_info(ws)

        gate = Gate(
            store,
            session_id,
            writer,
            policy=policy_doc["policy"],
            approval_timeout=env.approval_timeout(),
        )
        # A never-run rewind branch stores the SDK session of its base turn;
        # resuming it with fork_session tells the SDK to branch its own
        # transcript instead of continuing the abandoned tip. Detection is by
        # journal state (nothing past branch_created), not by comparing session
        # ids — the ids are equal exactly when the rewind targets the latest
        # run, which is the case that most needs the fork.
        branch_claude = branch_info.get("claude_session_id")
        fork = fresh_branch and bool(branch_claude)
        sdk_options = build_sdk_options(
            options,
            can_use_tool=gate.can_use_tool,
            cwd=str(ws),
            resume=branch_claude or session.get("claude_session_id"),
            fork_session=fork,
            env={**model_env(options), "HOME": str(home)},
            # "user" settings live in the sandboxed HOME above, so this only
            # ever loads milos-managed state — and it is what makes the
            # mounted ~/.claude/skills visible to the harness. "project" makes
            # the workspace's CLAUDE.md (restored at the workspace root) load
            # as the project memory for every session under the workspace.
            setting_sources=["user", "project"],
            hooks=gate.hooks(),
        )

        starting_cost = float(session.get("cost_usd") or 0.0)
        cost = starting_cost
        run_prompts: list[str] = []
        result_text: str | None = None
        claude_session_id = branch_claude or session.get("claude_session_id")
        stop_reason = "end_turn"

        async with ClaudeSDKClient(sdk_options) as client:
            watcher = asyncio.create_task(_watch_interrupt(store, session_id, client))
            try:
                while not lost.is_set():
                    messages = await _wait_for_messages(store, session_id, config.stay_alive)
                    if not messages:
                        break
                    # One query per prompt, so each gets its own turn (and its
                    # own result row) instead of being glued into one prompt.
                    for queued in messages:
                        text = queued["text"]
                        # The prompt is a first-class journal record, written
                        # *before* the inbox item is consumed, so a run that
                        # dies in between replays the prompt instead of
                        # swallowing it; the id ties the record back to the
                        # queued item.
                        await writer.append(
                            "prompt",
                            {"text": text, "source": "inbox", "inbox_id": queued["id"]},
                        )
                        await store.consume_message(session_id, queued["id"])
                        run_prompts.append(text)
                        await client.query(text)
                        async for message in client.receive_response():
                            # A lost lease mid-turn means another execution owns
                            # the journal now: stop appending immediately.
                            if lost.is_set():
                                break
                            if (
                                isinstance(message, SystemMessage)
                                and message.subtype in NOISY_SYSTEM_SUBTYPES
                            ):
                                continue
                            doc = message_to_doc(message)
                            if doc is not None:
                                await writer.append("message", doc)
                            if isinstance(message, ResultMessage):
                                cost += message.total_cost_usd or 0.0
                                claude_session_id = message.session_id
                                stop_reason = message.subtype
                                result_text = message.result or result_text
                        if lost.is_set():
                            break  # skip the flush: the doc belongs to the new owner
                        writer.context["claude_session_id"] = claude_session_id
                        await store.update_session(
                            session_id,
                            seq_head=writer.seq,
                            tip_uuid=writer.tip_uuid,
                            cost_usd=cost,
                            claude_session_id=claude_session_id,
                            **{f"branches.{branch}.claude_session_id": claude_session_id},
                        )
            finally:
                watcher.cancel()

        if lost.is_set():
            # The lease was stolen or the session killed mid-run: another
            # execution may own the session now, so write nothing — no
            # checkpoint, no release — and let the new owner's state stand.
            return

        await asyncio.to_thread(state.checkpoint, config.project, config.bucket, ws_prefix, ws)
        # Skills are mounted from the shared skills/ prefixes, not session
        # state: checkpointing them here would resurrect skills deleted from
        # the bucket.
        await asyncio.to_thread(
            state.checkpoint,
            config.project,
            config.bucket,
            home_prefix,
            home,
            (".claude/skills/",),
        )
        # The workspace lease drops before the released record: nothing after
        # this touches the shared directory, and holding the one contended
        # resource any longer would block a queued sibling session. Detach it
        # from the heartbeat first, or the next beat re-claims it.
        if options.workspace:
            leased_workspace["name"] = None
            await store.release_workspace(options.workspace, session_id)
        await writer.append("lifecycle", {"event": "released", "stop_reason": stop_reason})
        await store.release_session(
            session_id,
            status="idle",
            stop_reason=stop_reason,
            seq_head=writer.seq,
            tip_uuid=writer.tip_uuid,
            cost_usd=cost,
            claude_session_id=claude_session_id,
            **({"result": result_text} if result_text else {}),
        )
        await _handoff_queued(store, config, session_id)
    except Exception as error:
        # A crash used to end the process with the session still marked
        # "running" and the lease left to expire on its own — the session read
        # as "stalled" and no replacement execution could be triggered for it.
        # It releases like any other failed run instead.
        #
        # The journal belongs to whoever holds the lease: if it was lost, the
        # new owner's state stands and this run writes nothing. Re-raised
        # either way, so the execution still exits non-zero and Cloud Run
        # records the failure. The queued inbox is deliberately untouched — a
        # run that died before consuming a prompt leaves it for the next
        # execution rather than swallowing it, and nothing re-triggers here: a
        # deterministic failure would loop.
        if not lost.is_set():
            try:
                leased_workspace["name"] = None  # detach, or the next beat re-claims
                await fail_fast(
                    "error", {"error": repr(error)}, result=repr(error), release_workspace=True
                )
            except Exception as nested:
                # Handling a failure must not replace the original traceback.
                print(f"failure handling failed for {session_id}: {nested}", file=sys.stderr)
        raise
    finally:
        beat.cancel()


def main() -> None:
    session_id = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MILOS_SESSION")
    if not session_id:
        raise SystemExit("usage: python -m milos.runner <session_id>")
    asyncio.run(run(session_id))


if __name__ == "__main__":
    main()
