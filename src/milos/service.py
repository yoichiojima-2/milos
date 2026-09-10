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

from __future__ import annotations

import fnmatch
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from .audit import AuditLog
from .auth import SessionTokens, new_token
from .errors import Conflict, Forbidden, Invalid, NotFound, Stopped
from .jobs import JobLauncher
from .models import (
    USER_EVENTS,
    Agent,
    AgentVersion,
    Approval,
    Event,
    EventType,
    Lease,
    Permission,
    Session,
    SessionStatus,
    StopReason,
    sha256_json,
    sha256_text,
    utcnow,
)
from .store import Store, Transaction

RUNNER_ACTOR = "runner"
INSPECTOR_ACTOR = "system:inspection"
STALE_LEASE = timedelta(seconds=60)
MAX_PAYLOAD_BYTES = 200_000

DecisionKind = Literal["allow", "require_confirmation", "deny", "stop"]


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    reason: str
    tool_use_id: str
    approval_tool_use_id: str | None = None


@dataclass(frozen=True)
class Poll:
    stop: bool
    events: list[Event]


@dataclass
class RunnerEvent:
    """What a runner may append: agent.message, tool.result, session.usage."""

    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    tool_use_id: str | None = None


def new_session_id() -> str:
    return f"sess_{secrets.token_hex(12)}"


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
            agent = (
                Agent(**existing)
                if existing
                else Agent(agent_id=version.agent_id, latest_version=0)
            )
            published = version.model_copy(update={"version": agent.latest_version + 1})
            tx.create(f"agents/{version.agent_id}/versions/{published.version}", published.doc())
            tx.set(
                f"agents/{version.agent_id}",
                agent.model_copy(update={"latest_version": published.version}).doc(),
            )
            return published

        return await self.store.transaction(tx_fn)

    async def set_enabled(self, agent_id: str, enabled: bool) -> Agent:
        async def tx_fn(tx: Transaction) -> Agent:
            agent = await self._agent(tx, agent_id)
            agent = agent.model_copy(update={"enabled": enabled})
            tx.set(f"agents/{agent_id}", agent.doc())
            return agent

        return await self.store.transaction(tx_fn)

    async def get_agent(self, agent_id: str) -> tuple[Agent, AgentVersion]:
        agent_doc = await self.store.get(f"agents/{agent_id}")
        if not agent_doc:
            raise NotFound(f"agent {agent_id}")
        agent = Agent(**agent_doc)
        version_doc = await self.store.get(f"agents/{agent_id}/versions/{agent.latest_version}")
        if not version_doc:
            raise NotFound(f"agent {agent_id} has no published version")
        return agent, AgentVersion(**version_doc)

    async def list_agents(self) -> list[tuple[Agent, AgentVersion]]:
        out = []
        for doc in await self.store.query("agents", order_by="agent_id"):
            out.append(await self.get_agent(doc["agent_id"]))
        return out

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
        lease = Lease(runner_id=uuid.uuid4().hex, token=new_token(), last_poll_at=now)

        async def tx_fn(tx: Transaction) -> tuple[Session, bool]:
            existing = await tx.query(
                "sessions",
                where=[
                    ("operator", "==", operator),
                    ("client_request_id", "==", client_request_id),
                ],
            )
            if existing:
                if existing[0]["content_sha256"] != content_sha256:
                    raise Conflict("client_request_id reused with different content")
                return Session(**existing[0]), False
            agent = await self._agent(tx, agent_id)
            if not agent.enabled:
                raise Stopped(f"agent {agent_id} is disabled")
            version = await self._version(tx, agent_id, agent.latest_version)
            active = await tx.query(
                "sessions",
                where=[
                    ("agent_id", "==", agent_id),
                    (
                        "status",
                        "in",
                        [SessionStatus.RUNNING.value, SessionStatus.RESCHEDULING.value],
                    ),
                ],
            )
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
                lease=lease,
                created_at=now,
                updated_at=now,
            )
            events = [
                self._event(
                    EventType.USER_MESSAGE,
                    actor=operator,
                    client_request_id=client_request_id,
                    content_sha256=content_sha256,
                    payload={"text": message},
                ),
                self._status_event(SessionStatus.RUNNING, None),
            ]
            session = self._append(tx, session, events, create=True)
            return session, True

        session, created = await self.store.transaction(tx_fn)
        if created:
            await self._launch(session)
        return session

    async def accept_message(
        self, session_id: str, text: str, *, actor: str, client_request_id: str
    ) -> Event:
        return await self._accept_user_event(
            session_id,
            EventType.USER_MESSAGE,
            actor=actor,
            client_request_id=client_request_id,
            payload={"text": text},
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
            duplicates = await tx.query(
                f"sessions/{session_id}/events",
                where=[("actor", "==", actor), ("client_request_id", "==", client_request_id)],
            )
            if duplicates:
                if duplicates[0]["content_sha256"] != content_sha256:
                    raise Conflict("client_request_id reused with different content")
                return Event(**duplicates[0]), None
            event = self._event(
                type_,
                actor=actor,
                client_request_id=client_request_id,
                content_sha256=content_sha256,
                payload=payload,
            )
            relaunch = (
                type_ == EventType.USER_MESSAGE
                and session.status == SessionStatus.IDLE
                and session.stop_reason == StopReason.END_TURN
            )
            events = [event]
            updates: dict[str, Any] = {}
            if relaunch:
                updates = self._new_run(session)
                events.append(self._status_event(SessionStatus.RUNNING, None))
            session = self._append(tx, session, events, updates)
            return event, (session if relaunch else None)

        event, to_launch = await self.store.transaction(tx_fn)
        if to_launch:
            await self._launch(to_launch)
        return event

    async def confirm(
        self, session_id: str, tool_use_id: str, decision: Literal["allow", "deny"], *, actor: str
    ) -> Approval:
        now = self.now()

        async def tx_fn(tx: Transaction) -> tuple[Approval, Session]:
            session = await self._session(tx, session_id)
            if actor == session.operator:
                raise Forbidden("the operator cannot approve their own session")
            if session.approvers and actor not in session.approvers:
                raise Forbidden("not an approver of this session")
            if tool_use_id not in session.pending_tool_use_ids:
                raise Invalid(f"{tool_use_id} is not awaiting confirmation")
            if session.approval_expires_at and now > session.approval_expires_at:
                raise Invalid("the approval window has expired")
            request = await self._tool_use(tx, session_id, tool_use_id)
            approval = Approval(
                tool_use_id=tool_use_id,
                decision=decision,
                decided_by=actor,
                decided_at=now,
                expires_at=session.approval_expires_at or now,
                tool_name=request.payload["tool_name"],
                args_sha256=request.payload["args_sha256"],
            )
            tx.create(f"sessions/{session_id}/approvals/{tool_use_id}", approval.doc())
            session = await self._record_confirmation(tx, session, approval)
            return approval, session

        approval, session = await self.store.transaction(tx_fn)
        if session.status == SessionStatus.RUNNING and not session.pending_tool_use_ids:
            await self._launch(session)
        return approval

    async def terminate(self, session_id: str, *, actor: str) -> Session:
        async def tx_fn(tx: Transaction) -> Session:
            session = await self._session(tx, session_id)
            if session.status == SessionStatus.TERMINATED:
                return session
            updates = {
                "status": SessionStatus.TERMINATED.value,
                "stop_reason": StopReason.STOPPED.value,
                "pending_tool_use_ids": [],
                "approval_expires_at": None,
            }
            event = self._status_event(SessionStatus.TERMINATED, StopReason.STOPPED, actor=actor)
            return self._append(tx, session, [event], updates)

        return await self.store.transaction(tx_fn)

    async def get_session(self, session_id: str) -> Session:
        doc = await self.store.get(f"sessions/{session_id}")
        if not doc:
            raise NotFound(f"session {session_id}")
        return Session(**doc)

    async def list_sessions(self, *, operator: str | None = None, limit: int = 50) -> list[Session]:
        where = [("operator", "==", operator)] if operator else []
        docs = await self.store.query(
            "sessions", where=where, order_by="created_at", descending=True, limit=limit
        )
        return [Session(**d) for d in docs]

    async def events(self, session_id: str, *, after: int = 0, limit: int = 500) -> list[Event]:
        docs = await self.store.query(
            f"sessions/{session_id}/events",
            where=[("seq", ">", after)],
            order_by="seq",
            limit=limit,
        )
        return [Event(**d) for d in docs]

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
            session = await self._session(tx, session_id)
            self._check_lease(session, lease_token)
            agent = await self._agent(tx, session.agent_id)
            version = await self._version(tx, session.agent_id, session.agent_version)
            decision = await self._decide(
                tx, session, agent, version, tool_use_id, tool_name, args_sha256
            )
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
            session = self._append(tx, session, [event])
            return session, version, decision, event

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
        if decision.kind == "allow":
            await self.store.transaction(
                lambda tx: self._grant(
                    tx, session_id, lease_token, decision, tool_name, args_sha256
                )
            )
        elif decision.kind == "require_confirmation":
            await self.store.transaction(
                lambda tx: self._park(tx, session_id, lease_token, tool_use_id, version)
            )
        return decision

    async def poll(self, session_id: str, *, lease_token: str) -> Poll:
        """Runner heartbeat: unconsumed user events plus the stop signal."""
        now = self.now()

        async def tx_fn(tx: Transaction) -> Poll:
            session = await self._session(tx, session_id)
            self._check_lease(session, lease_token)
            agent = await self._agent(tx, session.agent_id)
            stop = session.status == SessionStatus.TERMINATED or not agent.enabled
            tx.update(f"sessions/{session_id}", {"lease.last_poll_at": now, "updated_at": now})
            docs = await tx.query(
                f"sessions/{session_id}/events",
                where=[("seq", ">", session.consumed_seq)],
                order_by="seq",
            )
            events = [Event(**d) for d in docs if d["type"] in USER_EVENTS]
            return Poll(stop=stop, events=events)

        return await self.store.transaction(tx_fn)

    async def ack(self, session_id: str, *, lease_token: str, seq: int) -> None:
        async def tx_fn(tx: Transaction) -> None:
            session = await self._session(tx, session_id)
            self._check_lease(session, lease_token)
            if seq > session.consumed_seq:
                tx.update(f"sessions/{session_id}", {"consumed_seq": seq, "updated_at": self.now()})

        await self.store.transaction(tx_fn)

    async def report(
        self, session_id: str, events: list[RunnerEvent], *, lease_token: str
    ) -> list[Event]:
        for item in events:
            if item.type not in (
                EventType.AGENT_MESSAGE,
                EventType.TOOL_RESULT,
                EventType.SESSION_USAGE,
            ):
                raise Invalid(f"runners may not append {item.type}")
            if len(str(item.payload)) > MAX_PAYLOAD_BYTES:
                raise Invalid("payload too large; store the body in the snapshot bucket")

        async def tx_fn(tx: Transaction) -> list[Event]:
            session = await self._session(tx, session_id)
            self._check_lease(session, lease_token)
            built = [
                self._event(
                    e.type, actor=RUNNER_ACTOR, tool_use_id=e.tool_use_id, payload=e.payload
                )
                for e in events
            ]
            self._append(tx, session, built)
            return built

        return await self.store.transaction(tx_fn)

    async def advance_snapshot(self, session_id: str, *, lease_token: str, number: int) -> None:
        async def tx_fn(tx: Transaction) -> None:
            session = await self._session(tx, session_id)
            self._check_lease(session, lease_token)
            if number != session.snapshot + 1:
                raise Invalid(f"snapshot {number} does not follow {session.snapshot}")
            tx.update(f"sessions/{session_id}", {"snapshot": number, "updated_at": self.now()})

        await self.store.transaction(tx_fn)

    async def finish(
        self, session_id: str, *, lease_token: str, stop_reason: StopReason
    ) -> Session:
        """Release the lease. Input that arrived during shutdown starts a fresh run."""

        async def tx_fn(tx: Transaction) -> tuple[Session, bool]:
            session = await self._session(tx, session_id)
            self._check_lease(session, lease_token)
            if session.status == SessionStatus.TERMINATED:
                return self._append(tx, session, [], {"lease": None}), False
            queued = await tx.query(
                f"sessions/{session_id}/events",
                where=[
                    ("seq", ">", session.consumed_seq),
                    ("type", "==", EventType.USER_MESSAGE.value),
                ],
                limit=1,
            )
            if stop_reason == StopReason.END_TURN and queued:
                updates = self._new_run(session)
                events = [self._status_event(SessionStatus.RUNNING, None)]
                return self._append(tx, session, events, updates), True
            if stop_reason == StopReason.REQUIRES_ACTION and session.pending_tool_use_ids:
                # already parked by permit(); just drop the lease
                return self._append(tx, session, [], {"lease": None}), False
            updates = {
                "status": SessionStatus.IDLE.value,
                "stop_reason": stop_reason.value,
                "lease": None,
            }
            return self._append(
                tx, session, [self._status_event(SessionStatus.IDLE, stop_reason)], updates
            ), False

        session, relaunch = await self.store.transaction(tx_fn)
        if relaunch:
            await self._launch(session)
        return session

    async def permission(
        self, session_id: str, *, tool_name: str, args_sha256: str
    ) -> Permission | None:
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
        return Permission(**max(docs, key=lambda d: d["created_at"]))

    # --- inspection (Cloud Scheduler) --------------------------------------

    async def inspect(self) -> dict[str, list[str]]:
        """Expire approvals and restart stalled runs. Idempotent; runs every minute."""
        now = self.now()
        report: dict[str, list[str]] = {"expired": [], "restarted": [], "attention": []}

        expired = await self.store.query("sessions", where=[("approval_expires_at", "<=", now)])
        for doc in expired:
            session = Session(**doc)
            if session.status == SessionStatus.TERMINATED or not session.pending_tool_use_ids:
                continue
            if await self._expire(session.session_id):
                report["expired"].append(session.session_id)

        active = await self.store.query(
            "sessions",
            where=[
                ("status", "in", [SessionStatus.RUNNING.value, SessionStatus.RESCHEDULING.value])
            ],
        )
        for doc in active:
            session = Session(**doc)
            if not session.lease or session.lease.last_poll_at > now - STALE_LEASE:
                continue
            outcome = await self._restart(session.session_id)
            if outcome:
                report[outcome].append(session.session_id)
        return report

    async def _expire(self, session_id: str) -> bool:
        now = self.now()

        async def tx_fn(tx: Transaction) -> Session | None:
            session = await self._session(tx, session_id)
            if not session.pending_tool_use_ids or session.status != SessionStatus.IDLE:
                return None
            for tool_use_id in list(session.pending_tool_use_ids):
                request = await self._tool_use(tx, session_id, tool_use_id)
                approval = Approval(
                    tool_use_id=tool_use_id,
                    decision="deny",
                    decided_by=INSPECTOR_ACTOR,
                    decided_at=now,
                    expires_at=session.approval_expires_at or now,
                    timed_out=True,
                    tool_name=request.payload["tool_name"],
                    args_sha256=request.payload["args_sha256"],
                )
                tx.create(f"sessions/{session_id}/approvals/{tool_use_id}", approval.doc())
                session = await self._record_confirmation(tx, session, approval)
            return session

        session = await self.store.transaction(tx_fn)
        if session and session.status == SessionStatus.RUNNING:
            await self._launch(session)
        return session is not None

    async def _restart(self, session_id: str) -> str | None:
        async def tx_fn(tx: Transaction) -> tuple[Session, str] | None:
            session = await self._session(tx, session_id)
            if not session.lease or session.lease.last_poll_at > self.now() - STALE_LEASE:
                return None
            if session.status == SessionStatus.RESCHEDULING:
                # the restart itself stalled: stop retrying, ask a human
                updates = {
                    "status": SessionStatus.IDLE.value,
                    "stop_reason": StopReason.NEEDS_ATTENTION.value,
                    "lease": None,
                }
                event = self._status_event(
                    SessionStatus.IDLE, StopReason.NEEDS_ATTENTION, actor=INSPECTOR_ACTOR
                )
                return self._append(tx, session, [event], updates), "attention"
            updates = self._new_run(session, status=SessionStatus.RESCHEDULING)
            event = self._status_event(SessionStatus.RESCHEDULING, None, actor=INSPECTOR_ACTOR)
            return self._append(tx, session, [event], updates), "restarted"

        result = await self.store.transaction(tx_fn)
        if not result:
            return None
        session, outcome = result
        if outcome == "restarted":
            await self._launch(session)
        return outcome

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
        if not agent.enabled:
            return Decision("stop", "agent disabled", tool_use_id)
        if session.status == SessionStatus.TERMINATED:
            return Decision("stop", "session terminated", tool_use_id)
        if not _tool_allowed(version.allowed_tools, tool_name):
            return Decision("deny", f"{tool_name} is not in the agent's allowed tools", tool_use_id)
        if not _tool_allowed(version.approval_required, tool_name):
            return Decision("allow", "allowed by definition", tool_use_id)
        approvals = await tx.query(
            f"sessions/{session.session_id}/approvals",
            where=[("tool_name", "==", tool_name), ("args_sha256", "==", args_sha256)],
        )
        if approvals:
            newest = Approval(**max(approvals, key=lambda d: d["decided_at"]))
            if newest.decision == "deny":
                who = "timed out" if newest.timed_out else f"denied by {newest.decided_by}"
                return Decision("deny", f"{tool_name} was {who}", tool_use_id)
            consumed = await tx.query(
                f"sessions/{session.session_id}/permissions",
                where=[("approval_tool_use_id", "==", newest.tool_use_id)],
                limit=1,
            )
            if not consumed:
                return Decision(
                    "allow", f"approved by {newest.decided_by}", tool_use_id, newest.tool_use_id
                )
        return Decision("require_confirmation", f"{tool_name} requires human approval", tool_use_id)

    async def _grant(
        self,
        tx: Transaction,
        session_id: str,
        lease_token: str,
        decision: Decision,
        tool_name: str,
        args_sha256: str,
    ) -> None:
        session = await self._session(tx, session_id)
        self._check_lease(session, lease_token)
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

    async def _park(
        self,
        tx: Transaction,
        session_id: str,
        lease_token: str,
        tool_use_id: str,
        version: AgentVersion,
    ) -> None:
        session = await self._session(tx, session_id)
        self._check_lease(session, lease_token)
        if tool_use_id in session.pending_tool_use_ids:
            return
        expires = self.now() + timedelta(seconds=version.approval_ttl_sec)
        updates = {
            "status": SessionStatus.IDLE.value,
            "stop_reason": StopReason.REQUIRES_ACTION.value,
            "pending_tool_use_ids": [*session.pending_tool_use_ids, tool_use_id],
            "approval_expires_at": expires,
        }
        event = self._status_event(SessionStatus.IDLE, StopReason.REQUIRES_ACTION)
        self._append(tx, session, [event], updates)

    async def _record_confirmation(
        self, tx: Transaction, session: Session, approval: Approval
    ) -> Session:
        pending = [t for t in session.pending_tool_use_ids if t != approval.tool_use_id]
        events = [
            self._event(
                EventType.USER_TOOL_CONFIRMATION,
                actor=approval.decided_by,
                tool_use_id=approval.tool_use_id,
                payload={
                    "decision": approval.decision,
                    "timed_out": approval.timed_out,
                    "tool_name": approval.tool_name,
                },
            )
        ]
        updates: dict[str, Any] = {"pending_tool_use_ids": pending}
        if not pending and session.status == SessionStatus.IDLE:
            updates.update(self._new_run(session))
            updates["approval_expires_at"] = None
            events.append(self._status_event(SessionStatus.RUNNING, None))
        return self._append(tx, session, events, updates)

    def _new_run(
        self, session: Session, status: SessionStatus = SessionStatus.RUNNING
    ) -> dict[str, Any]:
        lease = Lease(runner_id=uuid.uuid4().hex, token=new_token(), last_poll_at=self.now())
        return {"status": status.value, "stop_reason": None, "lease": lease.doc()}

    async def _launch(self, session: Session) -> None:
        assert session.lease is not None
        version_doc = await self.store.get(
            f"agents/{session.agent_id}/versions/{session.agent_version}"
        )
        version = AgentVersion(**version_doc)
        env = {
            **self.runner_env,
            "MILOS_SESSION_ID": session.session_id,
            "MILOS_SESSION_TOKEN": self.tokens.issue(session.session_id),
            "MILOS_LEASE_TOKEN": session.lease.token,
            "MILOS_RUNNER_ID": session.lease.runner_id,
        }
        await self.jobs.launch(session.session_id, env=env, runner_sa=version.runner_sa)

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
        now = self.now()
        seq = session.last_event_seq
        for event in events:
            seq += 1
            event.seq = seq
            tx.create(f"sessions/{session.session_id}/events/{event.event_id}", event.doc())
        fields = {**(updates or {}), "last_event_seq": seq, "updated_at": now}
        session = session.model_copy(update=self._typed(session, fields))
        if create:
            tx.create(f"sessions/{session.session_id}", session.doc())
        else:
            tx.update(f"sessions/{session.session_id}", fields)
        return session

    @staticmethod
    def _typed(session: Session, fields: dict[str, Any]) -> dict[str, Any]:
        out = dict(fields)
        if "lease" in out and out["lease"] is not None:
            out["lease"] = Lease(**out["lease"])
        if "status" in out:
            out["status"] = SessionStatus(out["status"])
        if out.get("stop_reason") is not None:
            out["stop_reason"] = StopReason(out["stop_reason"])
        return out

    def _event(self, type_: EventType, *, actor: str, **fields: Any) -> Event:
        return Event(
            event_id=uuid.uuid4().hex,
            seq=0,
            type=type_,
            actor=actor,
            created_at=self.now(),
            **fields,
        )

    def _status_event(
        self, status: SessionStatus, stop_reason: StopReason | None, *, actor: str = "api"
    ) -> Event:
        return self._event(
            EventType.SESSION_STATUS,
            actor=actor,
            payload={
                "status": status.value,
                "stop_reason": stop_reason.value if stop_reason else None,
            },
        )

    @staticmethod
    def _check_lease(session: Session, lease_token: str) -> None:
        if not session.lease or session.lease.token != lease_token:
            raise Forbidden("stale lease token")

    @staticmethod
    async def _session(tx: Transaction, session_id: str) -> Session:
        doc = await tx.get(f"sessions/{session_id}")
        if not doc:
            raise NotFound(f"session {session_id}")
        return Session(**doc)

    @staticmethod
    async def _agent(tx: Transaction, agent_id: str) -> Agent:
        doc = await tx.get(f"agents/{agent_id}")
        if not doc:
            raise NotFound(f"agent {agent_id}")
        return Agent(**doc)

    @staticmethod
    async def _version(tx: Transaction, agent_id: str, version: int) -> AgentVersion:
        doc = await tx.get(f"agents/{agent_id}/versions/{version}")
        if not doc:
            raise NotFound(f"agent {agent_id} version {version} is not published")
        return AgentVersion(**doc)

    @staticmethod
    async def _tool_use(tx: Transaction, session_id: str, tool_use_id: str) -> Event:
        docs = await tx.query(
            f"sessions/{session_id}/events",
            where=[
                ("tool_use_id", "==", tool_use_id),
                ("type", "==", EventType.AGENT_TOOL_USE.value),
            ],
            limit=1,
        )
        if not docs:
            raise NotFound(f"no tool request {tool_use_id}")
        return Event(**docs[0])


def _tool_allowed(patterns: list[str], tool_name: str) -> bool:
    return any(fnmatch.fnmatchcase(tool_name, p) for p in patterns)
