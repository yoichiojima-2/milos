"""The execution contract. Every state change goes through here.

Invariants enforced by this module (and nowhere else):

1. A state change and the event that describes it commit in one transaction.
2. `seq` is allocated by the API; ids deduplicate, `seq` orders.
3. A retried user request (same actor and client_request_id, same content)
   returns the original result; the same key with different content is a
   conflict. `requests/{key}` is create-only, so two retries cannot both win.
4. Permissions and approvals are create-only; an approver may not be the
   session's operator.
5. Runner writes must carry the current lease token.
6. A disabled agent or a terminated session gets no further permissions.
7. The event stream is the journal; the model transcript lives in snapshots.

Every state change is one method that runs one function under `_commit`: the
function gets a `Tx`, reads what it needs, appends events with `Tx.append` (the
only place a `seq` is allocated) and returns a `Change`. `_commit` writes the
sessions the function touched, commits, and launches the job a `Change` names.
Reads come before writes inside a transaction, as Firestore requires.

`permit` is the one exception with two transactions: the request is journaled
(and the session parked when a person must decide) in the first, the audit
entry is written synchronously, and only then does the second create the
permission. Nothing that lets a tool run exists before the audit entry does.
"""

import fnmatch
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

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
    Inspection,
    Lease,
    Outcome,
    PendingCall,
    Permission,
    PermissionAnswer,
    Polled,
    Request,
    RunnerEvent,
    Session,
    SessionStatus,
    StopReason,
    Verdict,
    sha256_json,
    utcnow,
)
from .store import Filter, Reader, Store, Transaction

API_ACTOR = "api"
RUNNER_ACTOR = "runner"
INSPECTOR_ACTOR = "system:inspection"
STALE_LEASE = timedelta(seconds=60)
MAX_PAYLOAD_BYTES = 200_000
ACTIVE_STATUSES = (SessionStatus.RUNNING, SessionStatus.RESCHEDULING)


def new_session_id() -> str:
    return f"sess_{secrets.token_hex(12)}"


def _tool_allowed(patterns: list[str], tool_name: str) -> bool:
    return any(fnmatch.fnmatchcase(tool_name, p) for p in patterns)


async def load[M: Document](reader: Reader, model: type[M], path: str, missing: str) -> M:
    if not (doc := await reader.get(path)):
        raise NotFound(missing)
    return model.model_validate(doc)


@dataclass(frozen=True, slots=True)
class Change[T]:
    """What a transaction function returns: its result, and the session whose job to launch once committed."""

    result: T
    launch: Session | None = None


class Tx:
    """One operation's transaction: the clock read once, the loaders, and the only write paths."""

    def __init__(self, transaction: Transaction, now: datetime) -> None:
        self._tx = transaction
        self.now = now
        self._loaded: dict[str, dict[str, Any]] = {}
        self._sessions: dict[str, Session] = {}

    # --- reads ---

    async def _load[M: Document](self, model: type[M], path: str, missing: str) -> M:
        loaded = await load(self._tx, model, path, missing)
        self._loaded[path] = loaded.doc()
        return loaded

    async def session(self, session_id: str) -> Session:
        return await self._load(Session, f"sessions/{session_id}", f"session {session_id}")

    async def leased(self, session_id: str, lease_token: str) -> Session:
        """The session, provided the caller holds its current lease."""
        session = await self.session(session_id)
        if not session.lease or session.lease.token != lease_token:
            raise Forbidden("stale lease token")
        return session

    async def agent(self, agent_id: str) -> Agent:
        return await self._load(Agent, f"agents/{agent_id}", f"agent {agent_id}")

    async def agent_if_any(self, agent_id: str) -> Agent | None:
        doc = await self._tx.get(f"agents/{agent_id}")
        return Agent.model_validate(doc) if doc else None

    async def version(self, agent_id: str, number: int) -> AgentVersion:
        path = f"agents/{agent_id}/versions/{number}"
        return await self._load(AgentVersion, path, f"agent {agent_id} version {number} is not published")

    async def event(self, session_id: str, event_id: str) -> Event:
        return await load(self._tx, Event, f"sessions/{session_id}/events/{event_id}", f"event {event_id}")

    async def query(self, collection: str, **kwargs: Any) -> list[dict[str, Any]]:
        return await self._tx.query(collection, **kwargs)

    async def replay(self, actor: str, client_request_id: str, content_sha256: str) -> Request | None:
        """The record of a retried request, or None. The same key with different content is a conflict."""
        doc = await self._tx.get(f"requests/{Request.key(actor, client_request_id)}")
        if doc is None:
            return None
        request = Request.model_validate(doc)
        if request.content_sha256 != content_sha256:
            raise Conflict("client_request_id reused with different content")
        return request

    # --- writes ---

    def record(self, actor: str, client_request_id: str, content_sha256: str, session: Session, event: Event | None) -> None:
        """Remember a user request so a retry returns the same result (create-only: retries cannot race)."""
        request = Request(
            actor=actor,
            client_request_id=client_request_id,
            content_sha256=content_sha256,
            session_id=session.session_id,
            event_id=event.event_id if event else None,
            created_at=self.now,
        )
        self.create(f"requests/{Request.key(actor, client_request_id)}", request)

    def create(self, path: str, document: Document) -> None:
        self._tx.create(path, document.doc())

    def set(self, path: str, document: Document) -> None:
        self._tx.set(path, document.doc())

    def append(
        self,
        session: Session,
        type_: EventType,
        *,
        actor: str,
        payload: dict[str, Any] | None = None,
        tool_use_id: str | None = None,
    ) -> tuple[Session, Event]:
        """Journal one event with the next `seq`; returns the session as it now stands."""
        seq = session.last_event_seq + 1
        event = Event(
            event_id=uuid.uuid4().hex,
            seq=seq,
            type=type_,
            actor=actor,
            tool_use_id=tool_use_id,
            payload=payload or {},
            created_at=self.now,
        )
        self._tx.create(f"sessions/{session.session_id}/events/{event.event_id}", event.doc())
        updates: dict[str, Any] = {"last_event_seq": seq}
        if type_ == EventType.USER_MESSAGE:
            updates["last_message_seq"] = seq
        return self.save(session.model_copy(update=updates)), event

    def save(self, session: Session) -> Session:
        """Remember the session's latest state; `flush` writes each session once."""
        self._sessions[session.session_id] = session
        return session

    def flush(self) -> None:
        for session in self._sessions.values():
            path = f"sessions/{session.session_id}"
            plain = session.model_copy(update={"updated_at": self.now}).doc()
            if (before := self._loaded.get(path)) is None:
                self._tx.create(path, plain)
            elif changed := {k: v for k, v in plain.items() if before.get(k) != v}:
                self._tx.update(path, changed)
        self._sessions.clear()


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

    async def _commit[T](self, fn: Callable[[Tx], Awaitable[Change[T]]]) -> T:
        """Run `fn` in one transaction with one clock reading, then launch the job it named."""
        now = self.now()

        async def run(transaction: Transaction) -> Change[T]:
            tx = Tx(transaction, now)
            change = await fn(tx)
            tx.flush()
            return change

        change = await self.store.transaction(run)
        if change.launch is not None:
            await self._launch(change.launch)
        return change.result

    # --- agents -------------------------------------------------------------

    async def publish(self, version: AgentVersion) -> AgentVersion:
        """Publish a validated definition as the agent's next version."""

        async def fn(tx: Tx) -> Change[AgentVersion]:
            agent = await tx.agent_if_any(version.agent_id) or Agent(agent_id=version.agent_id, latest_version=0)
            published = version.model_copy(update={"version": agent.latest_version + 1, "published_at": tx.now})
            tx.create(f"agents/{version.agent_id}/versions/{published.version}", published)
            tx.set(f"agents/{version.agent_id}", agent.model_copy(update={"latest_version": published.version}))
            return Change(published)

        return await self._commit(fn)

    async def set_enabled(self, agent_id: str, enabled: bool) -> Agent:
        async def fn(tx: Tx) -> Change[Agent]:
            agent = (await tx.agent(agent_id)).model_copy(update={"enabled": enabled})
            tx.set(f"agents/{agent_id}", agent)
            return Change(agent)

        return await self._commit(fn)

    async def get_agent(self, agent_id: str) -> tuple[Agent, AgentVersion]:
        agent = await load(self.store, Agent, f"agents/{agent_id}", f"agent {agent_id}")
        return agent, await self._latest(agent)

    async def list_agents(self) -> list[tuple[Agent, AgentVersion]]:
        agents = [Agent.model_validate(doc) for doc in await self.store.query("agents", order_by="agent_id")]
        return [(agent, await self._latest(agent)) for agent in agents]

    async def _latest(self, agent: Agent) -> AgentVersion:
        path = f"agents/{agent.agent_id}/versions/{agent.latest_version}"
        return await load(self.store, AgentVersion, path, f"agent {agent.agent_id} has no published version")

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
        content = sha256_json({"agent_id": agent_id, "message": message, "viewers": viewers, "approvers": approvers})

        async def fn(tx: Tx) -> Change[Session]:
            if (request := await tx.replay(operator, client_request_id, content)) is not None:
                return Change(await tx.session(request.session_id))
            agent = await tx.agent(agent_id)
            if not agent.enabled:
                raise Stopped(f"agent {agent_id} is disabled")
            version = await tx.version(agent_id, agent.latest_version)
            active = await tx.query("sessions", where=[("agent_id", "==", agent_id), ("status", "in", ACTIVE_STATUSES)])
            if len(active) >= version.max_concurrent_sessions:
                raise Invalid(f"agent {agent_id} is at its concurrency limit")
            session = Session(
                session_id=new_session_id(),
                agent_id=agent_id,
                agent_version=version.version,
                definition_sha256=version.definition_sha256,
                status=SessionStatus.RUNNING,
                operator=operator,
                viewers=viewers or [],
                approvers=approvers or [],
                created_at=tx.now,
                updated_at=tx.now,
            )
            session, _ = tx.append(session, EventType.USER_MESSAGE, actor=operator, payload={"text": message})
            session = self._run(tx, session)
            tx.record(operator, client_request_id, content, session, None)
            return Change(session, launch=session)

        return await self._commit(fn)

    async def accept_message(self, session_id: str, text: str, *, actor: str, client_request_id: str) -> Event:
        return await self._user_event(session_id, EventType.USER_MESSAGE, actor, client_request_id, {"text": text})

    async def interrupt(self, session_id: str, *, actor: str, client_request_id: str) -> Event:
        return await self._user_event(session_id, EventType.USER_INTERRUPT, actor, client_request_id, {})

    async def _user_event(
        self, session_id: str, type_: EventType, actor: str, client_request_id: str, payload: dict[str, Any]
    ) -> Event:
        content = sha256_json({"session_id": session_id, "type": type_, **payload})

        async def fn(tx: Tx) -> Change[Event]:
            if (request := await tx.replay(actor, client_request_id, content)) is not None:
                return Change(await tx.event(request.session_id, request.event_id or ""))
            session = await tx.session(session_id)
            if session.status == SessionStatus.TERMINATED:
                raise Stopped("session is terminated")
            session, event = tx.append(session, type_, actor=actor, payload=payload)
            tx.record(actor, client_request_id, content, session, event)
            if type_ == EventType.USER_MESSAGE and session.stop_reason == StopReason.END_TURN:
                return Change(event, launch=self._run(tx, session))
            return Change(event)

        return await self._commit(fn)

    async def decide(self, session_id: str, tool_use_id: str, *, verdict: Verdict, by: str) -> Approval:
        """Record a person's verdict on a parked tool call; the session resumes once nothing is pending."""

        async def fn(tx: Tx) -> Change[Approval]:
            session = await tx.session(session_id)
            if by == session.operator:
                raise Forbidden("the operator cannot approve their own session")
            if session.approvers and by not in session.approvers:
                raise Forbidden("not an approver of this session")
            call = next((c for c in session.pending if c.tool_use_id == tool_use_id), None)
            if call is None:
                raise Invalid(f"{tool_use_id} is not awaiting approval")
            if session.approval_expires_at and tx.now > session.approval_expires_at:
                raise Invalid("the approval window has expired")
            approval, session = self._approve(tx, session, call, verdict=verdict, by=by)
            return Change(approval, launch=session if session.status == SessionStatus.RUNNING else None)

        return await self._commit(fn)

    async def terminate(self, session_id: str, *, actor: str) -> Session:
        async def fn(tx: Tx) -> Change[Session]:
            session = await tx.session(session_id)
            if session.status == SessionStatus.TERMINATED:
                return Change(session)
            session = session.model_copy(update={"pending": [], "approval_expires_at": None})
            return Change(self._move(tx, session, SessionStatus.TERMINATED, StopReason.STOPPED, actor=actor))

        return await self._commit(fn)

    async def get_session(self, session_id: str) -> Session:
        return await load(self.store, Session, f"sessions/{session_id}", f"session {session_id}")

    async def context(self, session_id: str) -> tuple[Session, AgentVersion]:
        """What a runner needs at start: its session and the definition version pinned to it."""
        session = await self.get_session(session_id)
        path = f"agents/{session.agent_id}/versions/{session.agent_version}"
        return session, await load(self.store, AgentVersion, path, f"version {session.agent_version} is not published")

    async def list_sessions(
        self, *, operator: str | None = None, approver: str | None = None, limit: int = 50
    ) -> list[Session]:
        """Newest first; `operator` selects a user's own sessions, `approver` those naming them as approver."""
        where: list[Filter] = []
        if operator:
            where.append(("operator", "==", operator))
        if approver:
            where.append(("approvers", "array_contains", approver))
        docs = await self.store.query("sessions", where=where, order_by="created_at", descending=True, limit=limit)
        return [Session.model_validate(d) for d in docs]

    async def events(self, session_id: str, *, after: int = 0, limit: int = 500) -> list[Event]:
        docs = await self.store.query(f"sessions/{session_id}/events", where=[("seq", ">", after)], order_by="seq", limit=limit)
        return [Event.model_validate(d) for d in docs]

    # --- sessions: runner side ----------------------------------------------

    async def permit(
        self, session_id: str, *, lease_token: str, tool_use_id: str, tool_name: str, args: dict[str, Any]
    ) -> PermissionAnswer:
        """Answer one tool call. The request is journaled and audited before anything can be permitted."""
        args_sha256 = sha256_json(args)

        async def request(tx: Tx) -> Change[tuple[Session, AgentVersion, Event, PermissionAnswer]]:
            session = await tx.leased(session_id, lease_token)
            agent = await tx.agent(session.agent_id)
            version = await tx.version(session.agent_id, session.agent_version)
            answer = await self._outcome(tx, session, agent, version, tool_use_id, tool_name, args_sha256)
            payload = {
                "tool_name": tool_name,
                "args": args,
                "args_sha256": args_sha256,
                "outcome": answer.outcome,
                "reason": answer.reason,
            }
            session, event = tx.append(
                session, EventType.AGENT_TOOL_USE, actor=RUNNER_ACTOR, tool_use_id=tool_use_id, payload=payload
            )
            parked = any(c.tool_use_id == tool_use_id for c in session.pending)
            if answer.outcome == Outcome.REQUIRE_APPROVAL and not parked:
                call = PendingCall(
                    tool_use_id=tool_use_id, tool_name=tool_name, args_sha256=args_sha256, event_id=event.event_id
                )
                expires = tx.now + timedelta(seconds=version.approval_ttl_sec)
                session = session.model_copy(update={"pending": [*session.pending, call], "approval_expires_at": expires})
                session = self._move(tx, session, SessionStatus.IDLE, StopReason.REQUIRES_ACTION)
            return Change((session, version, event, answer))

        session, version, event, answer = await self._commit(request)
        # Synchronous audit entry: if this raises, no permission is created.
        self.audit.write(
            {
                "type": "tool_request",
                "session_id": session_id,
                "agent_id": session.agent_id,
                "agent_version": session.agent_version,
                "definition_sha256": session.definition_sha256,
                "data_classes": version.data_classes,
                "event_id": event.event_id,
                "seq": event.seq,
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "args_sha256": args_sha256,
                "outcome": answer.outcome,
                "reason": answer.reason,
                "at": event.created_at.isoformat(),
            }
        )
        if answer.outcome != Outcome.ALLOW:
            return answer

        async def grant(tx: Tx) -> Change[None]:
            session = await tx.leased(session_id, lease_token)
            agent = await tx.agent(session.agent_id)
            if not agent.enabled or session.status == SessionStatus.TERMINATED:
                raise Stopped("stopped between request and grant")
            permission = Permission(
                tool_use_id=tool_use_id,
                lease_token=lease_token,
                tool_name=tool_name,
                args_sha256=args_sha256,
                approval_tool_use_id=answer.approval_tool_use_id,
                created_at=tx.now,
            )
            tx.create(f"sessions/{session_id}/permissions/{tool_use_id}", permission)
            payload = {"tool_name": tool_name, "args_sha256": args_sha256, "reason": answer.reason}
            tx.append(session, EventType.TOOL_PERMITTED, actor=RUNNER_ACTOR, tool_use_id=tool_use_id, payload=payload)
            return Change(None)

        await self._commit(grant)
        return answer

    async def poll(self, session_id: str, *, lease_token: str) -> Polled:
        """Runner heartbeat: unconsumed user events plus the stop signal."""

        async def fn(tx: Tx) -> Change[Polled]:
            session = await tx.leased(session_id, lease_token)
            agent = await tx.agent(session.agent_id)
            docs = await tx.query(f"sessions/{session_id}/events", where=[("seq", ">", session.consumed_seq)], order_by="seq")
            events = [Event.model_validate(d) for d in docs if d["type"] in USER_EVENTS]
            if session.lease is not None:  # `leased` checked it; this keeps the type checker with us
                tx.save(session.model_copy(update={"lease": session.lease.model_copy(update={"last_poll_at": tx.now})}))
            stop = session.status == SessionStatus.TERMINATED or not agent.enabled
            return Change(Polled(stop=stop, events=events))

        return await self._commit(fn)

    async def ack(self, session_id: str, *, lease_token: str, seq: int) -> Session:
        async def fn(tx: Tx) -> Change[Session]:
            session = await tx.leased(session_id, lease_token)
            if seq > session.consumed_seq:
                session = tx.save(session.model_copy(update={"consumed_seq": seq}))
            return Change(session)

        return await self._commit(fn)

    async def report(self, session_id: str, events: list[RunnerEvent], *, lease_token: str) -> list[Event]:
        for item in events:
            if item.type not in RUNNER_EVENTS:
                raise Invalid(f"runners may not append {item.type}")
            if len(str(item.payload)) > MAX_PAYLOAD_BYTES:
                raise Invalid("payload too large; store the body in the snapshot bucket")

        async def fn(tx: Tx) -> Change[list[Event]]:
            session = await tx.leased(session_id, lease_token)
            journaled = []
            for item in events:
                session, event = tx.append(
                    session, item.type, actor=RUNNER_ACTOR, tool_use_id=item.tool_use_id, payload=item.payload
                )
                journaled.append(event)
            return Change(journaled)

        return await self._commit(fn)

    async def advance_snapshot(self, session_id: str, *, lease_token: str, number: int) -> Session:
        async def fn(tx: Tx) -> Change[Session]:
            session = await tx.leased(session_id, lease_token)
            if number != session.snapshot + 1:
                raise Invalid(f"snapshot {number} does not follow {session.snapshot}")
            return Change(tx.save(session.model_copy(update={"snapshot": number})))

        return await self._commit(fn)

    async def finish(self, session_id: str, *, lease_token: str, stop_reason: StopReason) -> Session:
        """Release the lease. Input that arrived during shutdown starts a fresh run."""

        async def fn(tx: Tx) -> Change[Session]:
            session = await tx.leased(session_id, lease_token)
            if session.status == SessionStatus.TERMINATED:
                return Change(tx.save(session.model_copy(update={"lease": None})))
            match stop_reason:
                case StopReason.END_TURN if session.last_message_seq > session.consumed_seq:
                    session = self._run(tx, session)
                    return Change(session, launch=session)
                case StopReason.REQUIRES_ACTION if session.pending:
                    # already parked by permit(); just drop the lease
                    return Change(tx.save(session.model_copy(update={"lease": None})))
                case _:
                    session = session.model_copy(update={"lease": None})
                    return Change(self._move(tx, session, SessionStatus.IDLE, stop_reason))

        return await self._commit(fn)

    async def permission(self, session_id: str, *, tool_name: str, args_sha256: str) -> Permission | None:
        """The permission a connector may act on: this exact call, under the session's current lease."""
        session = await self.get_session(session_id)
        if session.lease is None:
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

    async def inspect(self) -> Inspection:
        """Expire approvals and restart stalled runs. Idempotent; runs every minute."""
        now = self.now()
        report = Inspection()
        for doc in await self.store.query("sessions", where=[("approval_expires_at", "<=", now)]):
            session = Session.model_validate(doc)
            if session.status == SessionStatus.IDLE and session.pending and await self._expire(session.session_id):
                report.expired.append(session.session_id)
        for doc in await self.store.query("sessions", where=[("status", "in", ACTIVE_STATUSES)]):
            session = Session.model_validate(doc)
            if self._stalled(session, now) and (restarted := await self._restart(session.session_id)):
                bucket = report.attention if restarted.stop_reason == StopReason.NEEDS_ATTENTION else report.restarted
                bucket.append(session.session_id)
        return report

    async def _expire(self, session_id: str) -> bool:
        async def fn(tx: Tx) -> Change[bool]:
            session = await tx.session(session_id)
            if not session.pending or session.status != SessionStatus.IDLE:
                return Change(False)
            for call in list(session.pending):
                _, session = self._approve(tx, session, call, verdict=Verdict.DENY, by=INSPECTOR_ACTOR, timed_out=True)
            return Change(True, launch=session if session.status == SessionStatus.RUNNING else None)

        return await self._commit(fn)

    async def _restart(self, session_id: str) -> Session | None:
        async def fn(tx: Tx) -> Change[Session | None]:
            session = await tx.session(session_id)
            if not self._stalled(session, tx.now):
                return Change(None)
            if session.status == SessionStatus.RESCHEDULING:
                # the restart itself stalled: stop retrying, ask a human
                session = session.model_copy(update={"lease": None})
                moved = self._move(tx, session, SessionStatus.IDLE, StopReason.NEEDS_ATTENTION, actor=INSPECTOR_ACTOR)
                return Change(moved)
            session = self._run(tx, session, status=SessionStatus.RESCHEDULING, actor=INSPECTOR_ACTOR)
            return Change(session, launch=session)

        return await self._commit(fn)

    @staticmethod
    def _stalled(session: Session, now: datetime) -> bool:
        return session.lease is not None and session.lease.last_poll_at <= now - STALE_LEASE

    # --- internals ----------------------------------------------------------

    async def _outcome(
        self,
        tx: Tx,
        session: Session,
        agent: Agent,
        version: AgentVersion,
        tool_use_id: str,
        tool_name: str,
        args_sha256: str,
    ) -> PermissionAnswer:
        """The policy: the definition first, then any approval already recorded for this exact call."""

        def answer(outcome: Outcome, reason: str, approval_tool_use_id: str | None = None) -> PermissionAnswer:
            return PermissionAnswer(
                tool_use_id=tool_use_id, outcome=outcome, reason=reason, approval_tool_use_id=approval_tool_use_id
            )

        if not agent.enabled:
            return answer(Outcome.STOP, "agent disabled")
        if session.status == SessionStatus.TERMINATED:
            return answer(Outcome.STOP, "session terminated")
        if not _tool_allowed(version.allowed_tools, tool_name):
            return answer(Outcome.DENY, f"{tool_name} is not in the agent's allowed tools")
        if not _tool_allowed(version.approval_required, tool_name):
            return answer(Outcome.ALLOW, "allowed by definition")
        approvals = await tx.query(
            f"sessions/{session.session_id}/approvals",
            where=[("tool_name", "==", tool_name), ("args_sha256", "==", args_sha256)],
        )
        if approvals:
            newest = Approval.model_validate(max(approvals, key=lambda d: d["decided_at"]))
            if newest.verdict == Verdict.DENY:
                who = "timed out" if newest.timed_out else f"denied by {newest.decided_by}"
                return answer(Outcome.DENY, f"{tool_name} was {who}")
            consumed = await tx.query(
                f"sessions/{session.session_id}/permissions",
                where=[("approval_tool_use_id", "==", newest.tool_use_id)],
                limit=1,
            )
            if not consumed:
                return answer(Outcome.ALLOW, f"approved by {newest.decided_by}", newest.tool_use_id)
        return answer(Outcome.REQUIRE_APPROVAL, f"{tool_name} requires human approval")

    def _approve(
        self, tx: Tx, session: Session, call: PendingCall, *, verdict: Verdict, by: str, timed_out: bool = False
    ) -> tuple[Approval, Session]:
        """Record a verdict on a parked call; resume the session when nothing is pending."""
        approval = Approval(
            tool_use_id=call.tool_use_id,
            verdict=verdict,
            decided_by=by,
            decided_at=tx.now,
            expires_at=session.approval_expires_at or tx.now,
            timed_out=timed_out,
            tool_name=call.tool_name,
            args_sha256=call.args_sha256,
        )
        tx.create(f"sessions/{session.session_id}/approvals/{call.tool_use_id}", approval)
        pending = [c for c in session.pending if c.tool_use_id != call.tool_use_id]
        expires = session.approval_expires_at if pending else None
        session = session.model_copy(update={"pending": pending, "approval_expires_at": expires})
        payload = {"verdict": verdict, "timed_out": timed_out, "tool_name": call.tool_name}
        session, _ = tx.append(session, EventType.USER_APPROVAL, actor=by, tool_use_id=call.tool_use_id, payload=payload)
        if not pending and session.status == SessionStatus.IDLE:
            session = self._run(tx, session)
        return approval, session

    def _move(
        self, tx: Tx, session: Session, status: SessionStatus, stop_reason: StopReason | None, *, actor: str = API_ACTOR
    ) -> Session:
        """Move the session to `status` and journal it, in the caller's transaction."""
        session = session.model_copy(update={"status": status, "stop_reason": stop_reason})
        payload = {"status": status, "stop_reason": stop_reason}
        session, _ = tx.append(session, EventType.SESSION_STATUS, actor=actor, payload=payload)
        return session

    def _run(
        self, tx: Tx, session: Session, *, status: SessionStatus = SessionStatus.RUNNING, actor: str = API_ACTOR
    ) -> Session:
        """Issue a fresh lease and move to `status`; the caller names the session in its `Change` to launch the job."""
        lease = Lease(runner_id=uuid.uuid4().hex, token=new_token(), last_poll_at=tx.now)
        return self._move(tx, session.model_copy(update={"lease": lease}), status, None, actor=actor)

    async def _launch(self, session: Session) -> None:
        if session.lease is None:
            raise RuntimeError("a session cannot be launched without a lease")
        env = {
            **self.runner_env,
            "MILOS_SESSION_ID": session.session_id,
            "MILOS_SESSION_TOKEN": self.tokens.issue(session.session_id),
            "MILOS_LEASE_TOKEN": session.lease.token,
            "MILOS_RUNNER_ID": session.lease.runner_id,
        }
        await self.jobs.launch(session.session_id, agent_id=session.agent_id, env=env)
