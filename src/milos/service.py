"""The execution contract. Every state change goes through here.

Invariants enforced by this module (and nowhere else):

1. A state change and the event that describes it commit in one transaction.
2. `seq` is allocated by the API; ids deduplicate, `seq` orders.
3. A retried user request (same session, actor, client_request_id, content)
   returns the original result; the same key with different content is a
   conflict.
4. Permissions and approvals are create-only; an approver may not be the
   session's operator.
5. Runner writes must carry the current lease token.
6. A disabled agent or a terminated session gets no further permissions.
7. The event stream is the journal; the model transcript lives in snapshots.

The audit entry for a tool request is written synchronously after the
`agent.tool_use` event commits and before any permission document exists.
"""

import fnmatch
import secrets
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Literal

from .audit import AuditLog
from .auth import SessionTokens, new_token
from .errors import Conflict, Forbidden, Invalid, NotFound, Stopped
from .jobs import JobLauncher
from .models import (
    RUNNER_EVENTS,
    USER_EVENTS,
    Agent,
    AgentVersion,
    Approval,
    Document,
    Event,
    EventType,
    Lease,
    Permission,
    Session,
    SessionStatus,
    StopReason,
    ToolDecision,
    sha256_json,
    sha256_text,
    utcnow,
)
from .store import Reader, Store, Transaction

RUNNER_ACTOR = "runner"
INSPECTOR_ACTOR = "system:inspection"
STALE_LEASE = timedelta(seconds=60)
MAX_PAYLOAD_BYTES = 200_000
ACTIVE_STATUSES = [SessionStatus.RUNNING.value, SessionStatus.RESCHEDULING.value]

type DecisionKind = Literal["allow", "require_confirmation", "deny", "stop"]


@dataclass(frozen=True, slots=True)
class Decision:
    kind: DecisionKind
    reason: str
    tool_use_id: str
    approval_tool_use_id: str | None = None


@dataclass(frozen=True, slots=True)
class Poll:
    stop: bool
    events: list[Event]


@dataclass(slots=True)
class RunnerEvent:
    """What a runner may append: agent.message, tool.result, session.usage."""

    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    tool_use_id: str | None = None


def new_session_id() -> str:
    return f"sess_{secrets.token_hex(12)}"


async def _load[M: Document](reader: Reader, model: type[M], path: str, missing: str) -> M:
    if not (doc := await reader.get(path)):
        raise NotFound(missing)
    return model.model_validate(doc)


def _replay(rows: list[dict[str, Any]], content_sha256: str) -> dict[str, Any] | None:
    """The original row of a retried request, or None. The same key with a different body is a conflict."""
    if not rows:
        return None
    if rows[0]["content_sha256"] != content_sha256:
        raise Conflict("client_request_id reused with different content")
    return rows[0]


def _tool_allowed(patterns: list[str], tool_name: str) -> bool:
    return any(fnmatch.fnmatchcase(tool_name, p) for p in patterns)


class Service:
    def __init__(
        self,
        store: Store,
        audit: AuditLog,
        jobs: JobLauncher,
        tokens: SessionTokens,
        *,
        runner_env: dict[str, str] | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store = store
        self.audit = audit
        self.jobs = jobs
        self.tokens = tokens
        self.runner_env = runner_env or {}
        self.now = now

    # --- agents -------------------------------------------------------------

    async def publish(self, version: AgentVersion) -> AgentVersion:
        """Publish a validated definition as the agent's next version."""

        async def tx_fn(tx: Transaction) -> AgentVersion:
            existing = await tx.get(f"agents/{version.agent_id}")
            agent = Agent.model_validate(existing) if existing else Agent(agent_id=version.agent_id, latest_version=0)
            published = version.model_copy(update={"version": agent.latest_version + 1})
            tx.create(f"agents/{version.agent_id}/versions/{published.version}", published.doc())
            tx.set(f"agents/{version.agent_id}", agent.model_copy(update={"latest_version": published.version}).doc())
            return published

        return await self.store.transaction(tx_fn)

    async def set_enabled(self, agent_id: str, enabled: bool) -> Agent:
        async def tx_fn(tx: Transaction) -> Agent:
            agent = (await self._agent(tx, agent_id)).model_copy(update={"enabled": enabled})
            tx.set(f"agents/{agent_id}", agent.doc())
            return agent

        return await self.store.transaction(tx_fn)

    async def get_agent(self, agent_id: str) -> tuple[Agent, AgentVersion]:
        agent = await self._agent(self.store, agent_id)
        return agent, await self._latest(agent)

    async def list_agents(self) -> list[tuple[Agent, AgentVersion]]:
        agents = [Agent.model_validate(doc) for doc in await self.store.query("agents", order_by="agent_id")]
        return [(agent, await self._latest(agent)) for agent in agents]

    async def _latest(self, agent: Agent) -> AgentVersion:
        return await self._version(self.store, agent.agent_id, agent.latest_version)

    # --- sessions: user side ------------------------------------------------

    async def create_session(
        self,
        agent_id: str,
        message: str,
        *,
        operator: str,
        client_request_id: str,
        viewers: list[str] | None = None,
        approvers: list[str] | None = None,
    ) -> Session:
        now = self.now()
        content_sha256 = sha256_text(f"{agent_id}\n{message}")

        async def tx_fn(tx: Transaction) -> tuple[Session, bool]:
            same_request = [("operator", "==", operator), ("client_request_id", "==", client_request_id)]
            if (existing := _replay(await tx.query("sessions", where=same_request), content_sha256)) is not None:
                return Session.model_validate(existing), False
            agent = await self._agent(tx, agent_id)
            if not agent.enabled:
                raise Stopped(f"agent {agent_id} is disabled")
            version = await self._version(tx, agent_id, agent.latest_version)
            active = await tx.query("sessions", where=[("agent_id", "==", agent_id), ("status", "in", ACTIVE_STATUSES)])
            if len(active) >= version.max_concurrent_sessions:
                raise Invalid(f"agent {agent_id} is at its concurrency limit")
            session = Session(
                session_id=new_session_id(),
                agent_id=agent_id,
                agent_version=version.version,
                definition_sha256=version.definition_sha256,
                status=SessionStatus.RUNNING,
                classification=max(version.data_classes, default="none"),
                operator=operator,
                client_request_id=client_request_id,
                content_sha256=content_sha256,
                viewers=viewers or [],
                approvers=approvers or [],
                lease=self._new_lease(),
                created_at=now,
                updated_at=now,
            )
            first = self._event(
                EventType.USER_MESSAGE,
                actor=operator,
                client_request_id=client_request_id,
                content_sha256=content_sha256,
                payload={"text": message},
            )
            return self._set_status(tx, session, SessionStatus.RUNNING, None, events=[first], create=True), True

        session, created = await self.store.transaction(tx_fn)
        if created:
            await self._launch(session)
        return session

    async def accept_message(self, session_id: str, text: str, *, actor: str, client_request_id: str) -> Event:
        return await self._accept_user_event(
            session_id, EventType.USER_MESSAGE, actor=actor, client_request_id=client_request_id, payload={"text": text}
        )

    async def interrupt(self, session_id: str, *, actor: str, client_request_id: str) -> Event:
        return await self._accept_user_event(
            session_id, EventType.USER_INTERRUPT, actor=actor, client_request_id=client_request_id
        )

    async def _accept_user_event(
        self,
        session_id: str,
        type_: EventType,
        *,
        actor: str,
        client_request_id: str,
        payload: dict[str, Any] | None = None,
    ) -> Event:
        payload = payload or {}
        content_sha256 = sha256_json({"type": type_.value, **payload})

        async def tx_fn(tx: Transaction) -> tuple[Event, Session | None]:
            session = await self._session(tx, session_id)
            if session.status == SessionStatus.TERMINATED:
                raise Stopped("session is terminated")
            same_request = [("actor", "==", actor), ("client_request_id", "==", client_request_id)]
            rows = await tx.query(f"sessions/{session_id}/events", where=same_request)
            if (existing := _replay(rows, content_sha256)) is not None:
                return Event.model_validate(existing), None
            event = self._event(
                type_, actor=actor, client_request_id=client_request_id, content_sha256=content_sha256, payload=payload
            )
            resumes = (
                type_ == EventType.USER_MESSAGE
                and session.status == SessionStatus.IDLE
                and session.stop_reason == StopReason.END_TURN
            )
            if resumes:
                return event, self._start_run(tx, session, events=[event])
            self._append(tx, session, [event])
            return event, None

        event, to_launch = await self.store.transaction(tx_fn)
        if to_launch:
            await self._launch(to_launch)
        return event

    async def confirm(self, session_id: str, tool_use_id: str, decision: ToolDecision, *, actor: str) -> Approval:
        async def tx_fn(tx: Transaction) -> tuple[Approval, Session]:
            session = await self._session(tx, session_id)
            if actor == session.operator:
                raise Forbidden("the operator cannot approve their own session")
            if session.approvers and actor not in session.approvers:
                raise Forbidden("not an approver of this session")
            if tool_use_id not in session.pending_tool_use_ids:
                raise Invalid(f"{tool_use_id} is not awaiting confirmation")
            if session.approval_expires_at and self.now() > session.approval_expires_at:
                raise Invalid("the approval window has expired")
            return await self._decide_tool_use(tx, session, tool_use_id, decision=decision, by=actor)

        approval, session = await self.store.transaction(tx_fn)
        if session.status == SessionStatus.RUNNING and not session.pending_tool_use_ids:
            await self._launch(session)
        return approval

    async def terminate(self, session_id: str, *, actor: str) -> Session:
        async def tx_fn(tx: Transaction) -> Session:
            session = await self._session(tx, session_id)
            if session.status == SessionStatus.TERMINATED:
                return session
            return self._set_status(
                tx,
                session,
                SessionStatus.TERMINATED,
                StopReason.STOPPED,
                actor=actor,
                pending_tool_use_ids=[],
                approval_expires_at=None,
            )

        return await self.store.transaction(tx_fn)

    async def get_session(self, session_id: str) -> Session:
        return await self._session(self.store, session_id)

    async def context(self, session_id: str) -> tuple[Session, AgentVersion]:
        """What a runner needs at start: its session and the definition version pinned to it."""
        session = await self.get_session(session_id)
        return session, await self._version(self.store, session.agent_id, session.agent_version)

    async def list_sessions(self, *, operator: str | None = None, limit: int = 50) -> list[Session]:
        where = [("operator", "==", operator)] if operator else []
        docs = await self.store.query("sessions", where=where, order_by="created_at", descending=True, limit=limit)
        return [Session.model_validate(d) for d in docs]

    async def events(self, session_id: str, *, after: int = 0, limit: int = 500) -> list[Event]:
        docs = await self.store.query(f"sessions/{session_id}/events", where=[("seq", ">", after)], order_by="seq", limit=limit)
        return [Event.model_validate(d) for d in docs]

    def can_view(self, session: Session, email: str) -> bool:
        return email == session.operator or email in session.viewers or email in session.approvers

    # --- sessions: runner side ----------------------------------------------

    async def permit(
        self,
        session_id: str,
        *,
        lease_token: str,
        tool_use_id: str,
        tool_name: str,
        args: dict[str, Any],
    ) -> Decision:
        """Decide one tool call. The request is journaled and audited before it can be allowed."""
        args_sha256 = sha256_json(args)

        async def request(tx: Transaction) -> tuple[Session, AgentVersion, Decision, Event]:
            session = await self._leased(tx, session_id, lease_token)
            agent = await self._agent(tx, session.agent_id)
            version = await self._version(tx, session.agent_id, session.agent_version)
            decision = await self._decide(tx, session, agent, version, tool_use_id, tool_name, args_sha256)
            event = self._event(
                EventType.AGENT_TOOL_USE,
                actor=RUNNER_ACTOR,
                tool_use_id=tool_use_id,
                payload={
                    "tool_name": tool_name,
                    "args": args,
                    "args_sha256": args_sha256,
                    "decision": decision.kind,
                    "reason": decision.reason,
                },
            )
            return self._append(tx, session, [event]), version, decision, event

        session, version, decision, event = await self.store.transaction(request)
        # Synchronous audit entry: if this raises, no permission is created.
        self.audit.write(
            {
                "type": "tool_request",
                "session_id": session_id,
                "agent_id": session.agent_id,
                "agent_version": session.agent_version,
                "definition_sha256": session.definition_sha256,
                "classification": session.classification,
                "event_id": event.event_id,
                "seq": event.seq,
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "args_sha256": args_sha256,
                "decision": decision.kind,
                "reason": decision.reason,
                "at": event.created_at.isoformat(),
            }
        )
        match decision.kind:
            case "allow":
                await self.store.transaction(
                    lambda tx: self._grant(tx, session_id, lease_token, decision, tool_name, args_sha256)
                )
            case "require_confirmation":
                await self.store.transaction(lambda tx: self._park(tx, session_id, lease_token, tool_use_id, version))
        return decision

    async def poll(self, session_id: str, *, lease_token: str) -> Poll:
        """Runner heartbeat: unconsumed user events plus the stop signal."""
        now = self.now()

        async def tx_fn(tx: Transaction) -> Poll:
            session = await self._leased(tx, session_id, lease_token)
            agent = await self._agent(tx, session.agent_id)
            tx.update(f"sessions/{session_id}", {"lease.last_poll_at": now, "updated_at": now})
            docs = await tx.query(f"sessions/{session_id}/events", where=[("seq", ">", session.consumed_seq)], order_by="seq")
            events = [Event.model_validate(d) for d in docs if d["type"] in USER_EVENTS]
            return Poll(stop=session.status == SessionStatus.TERMINATED or not agent.enabled, events=events)

        return await self.store.transaction(tx_fn)

    async def ack(self, session_id: str, *, lease_token: str, seq: int) -> None:
        async def tx_fn(tx: Transaction) -> None:
            session = await self._leased(tx, session_id, lease_token)
            if seq > session.consumed_seq:
                tx.update(f"sessions/{session_id}", {"consumed_seq": seq, "updated_at": self.now()})

        await self.store.transaction(tx_fn)

    async def report(self, session_id: str, events: list[RunnerEvent], *, lease_token: str) -> list[Event]:
        for item in events:
            if item.type not in RUNNER_EVENTS:
                raise Invalid(f"runners may not append {item.type}")
            if len(str(item.payload)) > MAX_PAYLOAD_BYTES:
                raise Invalid("payload too large; store the body in the snapshot bucket")

        async def tx_fn(tx: Transaction) -> list[Event]:
            session = await self._leased(tx, session_id, lease_token)
            built = [self._event(e.type, actor=RUNNER_ACTOR, tool_use_id=e.tool_use_id, payload=e.payload) for e in events]
            self._append(tx, session, built)
            return built

        return await self.store.transaction(tx_fn)

    async def advance_snapshot(self, session_id: str, *, lease_token: str, number: int) -> None:
        async def tx_fn(tx: Transaction) -> None:
            session = await self._leased(tx, session_id, lease_token)
            if number != session.snapshot + 1:
                raise Invalid(f"snapshot {number} does not follow {session.snapshot}")
            tx.update(f"sessions/{session_id}", {"snapshot": number, "updated_at": self.now()})

        await self.store.transaction(tx_fn)

    async def finish(self, session_id: str, *, lease_token: str, stop_reason: StopReason) -> Session:
        """Release the lease. Input that arrived during shutdown starts a fresh run."""

        async def tx_fn(tx: Transaction) -> tuple[Session, bool]:
            session = await self._leased(tx, session_id, lease_token)
            if session.status == SessionStatus.TERMINATED:
                return self._append(tx, session, [], {"lease": None}), False
            queued = await tx.query(
                f"sessions/{session_id}/events",
                where=[("seq", ">", session.consumed_seq), ("type", "==", EventType.USER_MESSAGE.value)],
                limit=1,
            )
            match stop_reason:
                case StopReason.END_TURN if queued:
                    return self._start_run(tx, session), True
                case StopReason.REQUIRES_ACTION if session.pending_tool_use_ids:
                    # already parked by permit(); just drop the lease
                    return self._append(tx, session, [], {"lease": None}), False
                case _:
                    return self._set_status(tx, session, SessionStatus.IDLE, stop_reason, lease=None), False

        session, relaunch = await self.store.transaction(tx_fn)
        if relaunch:
            await self._launch(session)
        return session

    async def permission(self, session_id: str, *, tool_name: str, args_sha256: str) -> Permission | None:
        """Connector check: the newest permission for this exact call under the current lease."""
        session = await self.get_session(session_id)
        if not session.lease:
            return None
        docs = await self.store.query(
            f"sessions/{session_id}/permissions",
            where=[
                ("tool_name", "==", tool_name),
                ("args_sha256", "==", args_sha256),
                ("lease_token", "==", session.lease.token),
            ],
        )
        if not docs:
            return None
        return Permission.model_validate(max(docs, key=lambda d: d["created_at"]))

    # --- inspection (Cloud Scheduler) --------------------------------------

    async def inspect(self) -> dict[str, list[str]]:
        """Expire approvals and restart stalled runs. Idempotent; runs every minute."""
        now = self.now()
        report: dict[str, list[str]] = {"expired": [], "restarted": [], "attention": []}

        for doc in await self.store.query("sessions", where=[("approval_expires_at", "<=", now)]):
            session = Session.model_validate(doc)
            if session.status == SessionStatus.TERMINATED or not session.pending_tool_use_ids:
                continue
            if await self._expire(session.session_id):
                report["expired"].append(session.session_id)

        for doc in await self.store.query("sessions", where=[("status", "in", ACTIVE_STATUSES)]):
            session = Session.model_validate(doc)
            if self._stalled(session, now) and (outcome := await self._restart(session.session_id)):
                report[outcome].append(session.session_id)
        return report

    async def _expire(self, session_id: str) -> bool:
        async def tx_fn(tx: Transaction) -> Session | None:
            session = await self._session(tx, session_id)
            if not session.pending_tool_use_ids or session.status != SessionStatus.IDLE:
                return None
            for tool_use_id in list(session.pending_tool_use_ids):
                _, session = await self._decide_tool_use(
                    tx, session, tool_use_id, decision="deny", by=INSPECTOR_ACTOR, timed_out=True
                )
            return session

        session = await self.store.transaction(tx_fn)
        if session and session.status == SessionStatus.RUNNING:
            await self._launch(session)
        return session is not None

    async def _restart(self, session_id: str) -> str | None:
        async def tx_fn(tx: Transaction) -> tuple[Session, str] | None:
            session = await self._session(tx, session_id)
            if not self._stalled(session, self.now()):
                return None
            if session.status == SessionStatus.RESCHEDULING:
                # the restart itself stalled: stop retrying, ask a human
                needs_attention = self._set_status(
                    tx, session, SessionStatus.IDLE, StopReason.NEEDS_ATTENTION, actor=INSPECTOR_ACTOR, lease=None
                )
                return needs_attention, "attention"
            return self._start_run(tx, session, status=SessionStatus.RESCHEDULING, actor=INSPECTOR_ACTOR), "restarted"

        if (result := await self.store.transaction(tx_fn)) is None:
            return None
        session, outcome = result
        if outcome == "restarted":
            await self._launch(session)
        return outcome

    @staticmethod
    def _stalled(session: Session, now: datetime) -> bool:
        return session.lease is not None and session.lease.last_poll_at <= now - STALE_LEASE

    # --- internals ----------------------------------------------------------

    async def _decide(
        self,
        tx: Transaction,
        session: Session,
        agent: Agent,
        version: AgentVersion,
        tool_use_id: str,
        tool_name: str,
        args_sha256: str,
    ) -> Decision:
        verdict = partial(Decision, tool_use_id=tool_use_id)
        if not agent.enabled:
            return verdict("stop", "agent disabled")
        if session.status == SessionStatus.TERMINATED:
            return verdict("stop", "session terminated")
        if not _tool_allowed(version.allowed_tools, tool_name):
            return verdict("deny", f"{tool_name} is not in the agent's allowed tools")
        if not _tool_allowed(version.approval_required, tool_name):
            return verdict("allow", "allowed by definition")
        approvals = await tx.query(
            f"sessions/{session.session_id}/approvals",
            where=[("tool_name", "==", tool_name), ("args_sha256", "==", args_sha256)],
        )
        if approvals:
            newest = Approval.model_validate(max(approvals, key=lambda d: d["decided_at"]))
            if newest.decision == "deny":
                who = "timed out" if newest.timed_out else f"denied by {newest.decided_by}"
                return verdict("deny", f"{tool_name} was {who}")
            consumed = await tx.query(
                f"sessions/{session.session_id}/permissions",
                where=[("approval_tool_use_id", "==", newest.tool_use_id)],
                limit=1,
            )
            if not consumed:
                return verdict("allow", f"approved by {newest.decided_by}", approval_tool_use_id=newest.tool_use_id)
        return verdict("require_confirmation", f"{tool_name} requires human approval")

    async def _grant(
        self,
        tx: Transaction,
        session_id: str,
        lease_token: str,
        decision: Decision,
        tool_name: str,
        args_sha256: str,
    ) -> None:
        session = await self._leased(tx, session_id, lease_token)
        agent = await self._agent(tx, session.agent_id)
        if not agent.enabled or session.status == SessionStatus.TERMINATED:
            raise Stopped("stopped between request and grant")
        permission = Permission(
            tool_use_id=decision.tool_use_id,
            lease_token=lease_token,
            tool_name=tool_name,
            args_sha256=args_sha256,
            approval_tool_use_id=decision.approval_tool_use_id,
            created_at=self.now(),
        )
        tx.create(f"sessions/{session_id}/permissions/{decision.tool_use_id}", permission.doc())
        event = self._event(
            EventType.TOOL_PERMITTED,
            actor=RUNNER_ACTOR,
            tool_use_id=decision.tool_use_id,
            payload={"tool_name": tool_name, "args_sha256": args_sha256, "reason": decision.reason},
        )
        self._append(tx, session, [event])

    async def _park(self, tx: Transaction, session_id: str, lease_token: str, tool_use_id: str, version: AgentVersion) -> None:
        session = await self._leased(tx, session_id, lease_token)
        if tool_use_id in session.pending_tool_use_ids:
            return
        self._set_status(
            tx,
            session,
            SessionStatus.IDLE,
            StopReason.REQUIRES_ACTION,
            pending_tool_use_ids=[*session.pending_tool_use_ids, tool_use_id],
            approval_expires_at=self.now() + timedelta(seconds=version.approval_ttl_sec),
        )

    async def _decide_tool_use(
        self,
        tx: Transaction,
        session: Session,
        tool_use_id: str,
        *,
        decision: ToolDecision,
        by: str,
        timed_out: bool = False,
    ) -> tuple[Approval, Session]:
        """Record a human (or timeout) decision on a parked tool call and resume the session when nothing is pending."""
        now = self.now()
        request = await self._tool_use(tx, session.session_id, tool_use_id)
        approval = Approval(
            tool_use_id=tool_use_id,
            decision=decision,
            decided_by=by,
            decided_at=now,
            expires_at=session.approval_expires_at or now,
            timed_out=timed_out,
            tool_name=request.payload["tool_name"],
            args_sha256=request.payload["args_sha256"],
        )
        tx.create(f"sessions/{session.session_id}/approvals/{tool_use_id}", approval.doc())
        pending = [t for t in session.pending_tool_use_ids if t != tool_use_id]
        event = self._event(
            EventType.USER_TOOL_CONFIRMATION,
            actor=by,
            tool_use_id=tool_use_id,
            payload={"decision": decision, "timed_out": timed_out, "tool_name": approval.tool_name},
        )
        if not pending and session.status == SessionStatus.IDLE:
            session = self._start_run(tx, session, events=[event], pending_tool_use_ids=pending, approval_expires_at=None)
        else:
            session = self._append(tx, session, [event], {"pending_tool_use_ids": pending})
        return approval, session

    def _new_lease(self) -> Lease:
        return Lease(runner_id=uuid.uuid4().hex, token=new_token(), last_poll_at=self.now())

    async def _launch(self, session: Session) -> None:
        assert session.lease is not None
        env = {
            **self.runner_env,
            "MILOS_SESSION_ID": session.session_id,
            "MILOS_SESSION_TOKEN": self.tokens.issue(session.session_id),
            "MILOS_LEASE_TOKEN": session.lease.token,
            "MILOS_RUNNER_ID": session.lease.runner_id,
        }
        await self.jobs.launch(session.session_id, agent_id=session.agent_id, env=env)

    def _append(
        self,
        tx: Transaction,
        session: Session,
        events: list[Event],
        updates: dict[str, Any] | None = None,
        *,
        create: bool = False,
    ) -> Session:
        """Allocate seq for each event and write them with the session in one transaction."""
        seq = session.last_event_seq
        for event in events:
            seq += 1
            event.seq = seq
            tx.create(f"sessions/{session.session_id}/events/{event.event_id}", event.doc())
        fields = {**(updates or {}), "last_event_seq": seq, "updated_at": self.now()}
        session = session.model_copy(update=fields)
        plain = session.doc()
        if create:
            tx.create(f"sessions/{session.session_id}", plain)
        else:
            tx.update(f"sessions/{session.session_id}", {k: plain[k] for k in fields})
        return session

    def _set_status(
        self,
        tx: Transaction,
        session: Session,
        status: SessionStatus,
        stop_reason: StopReason | None,
        *,
        actor: str = "api",
        events: Iterable[Event] = (),
        create: bool = False,
        **updates: Any,
    ) -> Session:
        """Write `events`, then a status event, and move the session to `status`, all in one transaction."""
        status_event = self._event(
            EventType.SESSION_STATUS,
            actor=actor,
            payload={"status": status.value, "stop_reason": stop_reason.value if stop_reason else None},
        )
        fields = {"status": status, "stop_reason": stop_reason, **updates}
        return self._append(tx, session, [*events, status_event], fields, create=create)

    def _start_run(
        self,
        tx: Transaction,
        session: Session,
        *,
        status: SessionStatus = SessionStatus.RUNNING,
        actor: str = "api",
        events: Iterable[Event] = (),
        **updates: Any,
    ) -> Session:
        """Issue a fresh lease; the caller launches the job once the transaction has committed."""
        return self._set_status(tx, session, status, None, actor=actor, events=events, lease=self._new_lease(), **updates)

    def _event(self, type_: EventType, *, actor: str, **fields: Any) -> Event:
        return Event(event_id=uuid.uuid4().hex, seq=0, type=type_, actor=actor, created_at=self.now(), **fields)

    @staticmethod
    def _check_lease(session: Session, lease_token: str) -> None:
        if not session.lease or session.lease.token != lease_token:
            raise Forbidden("stale lease token")

    async def _leased(self, tx: Transaction, session_id: str, lease_token: str) -> Session:
        """The session, provided the caller holds its current lease."""
        session = await self._session(tx, session_id)
        self._check_lease(session, lease_token)
        return session

    @staticmethod
    async def _session(reader: Reader, session_id: str) -> Session:
        return await _load(reader, Session, f"sessions/{session_id}", f"session {session_id}")

    @staticmethod
    async def _agent(reader: Reader, agent_id: str) -> Agent:
        return await _load(reader, Agent, f"agents/{agent_id}", f"agent {agent_id}")

    @staticmethod
    async def _version(reader: Reader, agent_id: str, version: int) -> AgentVersion:
        path = f"agents/{agent_id}/versions/{version}"
        return await _load(reader, AgentVersion, path, f"agent {agent_id} version {version} is not published")

    @staticmethod
    async def _tool_use(tx: Transaction, session_id: str, tool_use_id: str) -> Event:
        docs = await tx.query(
            f"sessions/{session_id}/events",
            where=[("tool_use_id", "==", tool_use_id), ("type", "==", EventType.AGENT_TOOL_USE.value)],
            limit=1,
        )
        if not docs:
            raise NotFound(f"no tool request {tool_use_id}")
        return Event.model_validate(docs[0])
