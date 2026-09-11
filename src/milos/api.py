"""HTTP surface. One FastAPI app, two deployments of one image.

`role="public"` mounts the user routes and expects IAP in front; every request
carries a verified identity and the body is never trusted for it.
`role="internal"` mounts the runner, connector and scheduler routes; Cloud Run
IAM restricts invokers, and runners additionally present the session token in
`X-Milos-Session` plus their lease token in `X-Milos-Lease`.
"""

import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .audit import CloudAuditLog, StderrAuditLog
from .auth import IAP_HEADER, CloudIdentityDirectory, Directory, IapVerifier, Principal, SessionTokens
from .errors import Forbidden, MilosError, Unauthorized
from .jobs import CloudRunJobs, NoJobs
from .models import Agent, AgentVersion, Approval, Event, EventType, Session, StopReason, ToolDecision
from .service import RunnerEvent, Service
from .settings import ApiSettings
from .store import FirestoreStore

SCHEDULER_ACTOR = "scheduler"

ClientRequestId = Annotated[str, Field(default_factory=lambda: secrets.token_hex(8))]


class CreateSession(BaseModel):
    agent_id: str
    message: str
    client_request_id: ClientRequestId
    viewers: list[str] = Field(default_factory=list)
    approvers: list[str] = Field(default_factory=list)


class SendMessage(BaseModel):
    text: str
    client_request_id: ClientRequestId


class Interrupt(BaseModel):
    client_request_id: ClientRequestId


class Confirm(BaseModel):
    tool_use_id: str
    decision: ToolDecision


class Permit(BaseModel):
    tool_use_id: str
    tool_name: str
    args: dict[str, Any] = Field(default_factory=dict)


class Ack(BaseModel):
    seq: int


class RunnerEventIn(BaseModel):
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    tool_use_id: str | None = None


class Snapshot(BaseModel):
    number: int


class Finish(BaseModel):
    stop_reason: StopReason


class Published(BaseModel):
    agent: Agent
    version: AgentVersion


class RunnerContext(BaseModel):
    session: Session
    version: AgentVersion


class Polled(BaseModel):
    stop: bool
    events: list[Event]


def create_app(
    service: Service,
    *,
    role: str,
    tokens: SessionTokens,
    iap: IapVerifier | None = None,
    directory: Directory | None = None,
    dev_user: str | None = None,
) -> FastAPI:
    app = FastAPI(title="milos", docs_url=None, redoc_url=None)

    @app.exception_handler(MilosError)
    async def on_error(_: Request, error: MilosError) -> JSONResponse:
        return JSONResponse({"error": type(error).__name__, "detail": str(error)}, error.status)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "role": role}

    match role:
        case "public":
            app.include_router(_public(service, iap=iap, directory=directory, dev_user=dev_user))
        case "internal":
            app.include_router(_internal(service, tokens=tokens))
        case _:
            raise ValueError(f"unknown API role {role!r}")
    return app


# --- public ---------------------------------------------------------------------


def _public(service: Service, *, iap: IapVerifier | None, directory: Directory | None, dev_user: str | None) -> APIRouter:
    router = APIRouter(prefix="/v1")

    async def principal(request: Request) -> Principal:
        if iap is not None:
            return iap.verify(request.headers.get(IAP_HEADER))
        if dev_user:
            return Principal(email=dev_user)
        raise Unauthorized("no identity provider configured")

    User = Annotated[Principal, Depends(principal)]

    async def require_member(email: str, groups: list[str], message: str) -> None:
        for group in groups:
            if directory is not None and await directory.is_member(email, group):
                return
        raise Forbidden(message)

    async def viewable(session_id: str, user: User) -> Session:
        session = await service.get_session(session_id)
        if not service.can_view(session, user.email):
            raise Forbidden("not a participant of this session")
        return session

    async def operated(session: Annotated[Session, Depends(viewable)], user: User) -> Session:
        if user.email != session.operator:
            raise Forbidden("only the operator may do this")
        return session

    Viewable = Annotated[Session, Depends(viewable)]
    Operated = Annotated[Session, Depends(operated)]

    @router.get("/agents")
    async def list_agents(user: User) -> list[Published]:
        return [Published(agent=a, version=v) for a, v in await service.list_agents()]

    @router.get("/agents/{agent_id}")
    async def get_agent(agent_id: str, user: User) -> Published:
        agent, version = await service.get_agent(agent_id)
        return Published(agent=agent, version=version)

    @router.post("/sessions", status_code=201)
    async def create_session(body: CreateSession, user: User) -> Session:
        _, version = await service.get_agent(body.agent_id)
        await require_member(user.email, version.allowed_groups, f"{user.email} may not start {body.agent_id}")
        for approver in body.approvers:
            await require_member(approver, version.allowed_groups, f"approver {approver} is not allowed to use {body.agent_id}")
        return await service.create_session(
            body.agent_id,
            body.message,
            operator=user.email,
            client_request_id=body.client_request_id,
            viewers=body.viewers,
            approvers=body.approvers,
        )

    @router.get("/sessions")
    async def list_sessions(user: User) -> list[Session]:
        return await service.list_sessions(operator=user.email)

    @router.get("/sessions/{session_id}")
    async def get_session(session: Viewable) -> Session:
        return session

    @router.get("/sessions/{session_id}/events")
    async def events(session: Viewable, after: Annotated[int, Query(ge=0)] = 0) -> list[Event]:
        return await service.events(session.session_id, after=after)

    @router.post("/sessions/{session_id}/messages", status_code=201)
    async def send(session: Operated, body: SendMessage, user: User) -> Event:
        return await service.accept_message(
            session.session_id, body.text, actor=user.email, client_request_id=body.client_request_id
        )

    @router.post("/sessions/{session_id}/interrupt", status_code=201)
    async def interrupt(session: Operated, body: Interrupt, user: User) -> Event:
        return await service.interrupt(session.session_id, actor=user.email, client_request_id=body.client_request_id)

    @router.post("/sessions/{session_id}/approvals", status_code=201)
    async def confirm(session_id: str, body: Confirm, user: User) -> Approval:
        # Approvers need not be participants; membership of the agent's groups is what qualifies them.
        session = await service.get_session(session_id)
        _, version = await service.get_agent(session.agent_id)
        await require_member(user.email, version.allowed_groups, f"{user.email} may not approve for {session.agent_id}")
        return await service.confirm(session_id, body.tool_use_id, body.decision, actor=user.email)

    @router.post("/sessions/{session_id}/terminate")
    async def terminate(session: Viewable, user: User) -> Session:
        return await service.terminate(session.session_id, actor=user.email)

    return router


# --- internal -------------------------------------------------------------------


def _internal(service: Service, *, tokens: SessionTokens) -> APIRouter:
    router = APIRouter(prefix="/internal")

    async def session_of(session_id: str, token: Annotated[str | None, Header(alias="X-Milos-Session")] = None) -> str:
        """The session token must name the session in the path.

        It travels in its own header: `Authorization` carries the caller's
        Google identity token, which Cloud Run IAM checks before we run.
        """
        if tokens.verify(token) != session_id:
            raise Unauthorized("session token does not match the session")
        return session_id

    Owned = Annotated[str, Depends(session_of)]
    Lease = Annotated[str, Header(alias="X-Milos-Lease")]

    @router.post("/sessions", status_code=201)
    async def scheduled_session(
        body: CreateSession,
        job: Annotated[str | None, Header(alias="X-CloudScheduler-JobName")] = None,
        scheduled_at: Annotated[str | None, Header(alias="X-CloudScheduler-ScheduleTime")] = None,
    ) -> Session:
        """Cloud Scheduler: one session per scheduled time, however often it retries."""
        return await service.create_session(
            body.agent_id,
            body.message,
            operator=SCHEDULER_ACTOR,
            client_request_id=f"{job}:{scheduled_at}" if job and scheduled_at else body.client_request_id,
            viewers=body.viewers,
            approvers=body.approvers,
        )

    @router.post("/inspect")
    async def inspect() -> dict[str, list[str]]:
        return await service.inspect()

    @router.get("/sessions/{session_id}")
    async def context(session_id: Owned) -> RunnerContext:
        session, version = await service.context(session_id)
        return RunnerContext(session=session, version=version)

    @router.post("/sessions/{session_id}/permit")
    async def permit(session_id: Owned, lease: Lease, body: Permit) -> dict[str, str]:
        decision = await service.permit(
            session_id, lease_token=lease, tool_use_id=body.tool_use_id, tool_name=body.tool_name, args=body.args
        )
        return {"decision": decision.kind, "reason": decision.reason}

    @router.post("/sessions/{session_id}/poll")
    async def poll(session_id: Owned, lease: Lease) -> Polled:
        result = await service.poll(session_id, lease_token=lease)
        return Polled(stop=result.stop, events=result.events)

    @router.post("/sessions/{session_id}/ack")
    async def ack(session_id: Owned, lease: Lease, body: Ack) -> dict[str, str]:
        await service.ack(session_id, lease_token=lease, seq=body.seq)
        return {"status": "ok"}

    @router.post("/sessions/{session_id}/events", status_code=201)
    async def report(session_id: Owned, lease: Lease, body: list[RunnerEventIn]) -> list[Event]:
        return await service.report(session_id, [RunnerEvent(**e.model_dump()) for e in body], lease_token=lease)

    @router.post("/sessions/{session_id}/snapshot")
    async def snapshot(session_id: Owned, lease: Lease, body: Snapshot) -> dict[str, str]:
        await service.advance_snapshot(session_id, lease_token=lease, number=body.number)
        return {"status": "ok"}

    @router.post("/sessions/{session_id}/finish")
    async def finish(session_id: Owned, lease: Lease, body: Finish) -> Session:
        return await service.finish(session_id, lease_token=lease, stop_reason=body.stop_reason)

    @router.get("/sessions/{session_id}/permissions")
    async def permission(session_id: Owned, tool_name: str, args_sha256: str) -> dict[str, Any]:
        """Connector check: is this exact call permitted under the current lease?"""
        found = await service.permission(session_id, tool_name=tool_name, args_sha256=args_sha256)
        return {"permitted": found is not None, "tool_use_id": found.tool_use_id if found else None}

    return router


def build_from_env() -> FastAPI:
    """Entry point for `uvicorn milos.api:app`-style deployments."""
    settings = ApiSettings.from_env()
    tokens = SessionTokens(settings.token_key)
    local = bool(settings.dev_user)  # no GCP: sessions are created but never run, audit goes to stderr
    service = Service(
        FirestoreStore(project=settings.project),
        StderrAuditLog() if local else CloudAuditLog(settings.project),
        NoJobs() if local else CloudRunJobs(settings.project, settings.region, settings.runner_job_prefix),
        tokens,
        runner_env=settings.runner_env(),
    )
    return create_app(
        service,
        role=settings.api_role,
        tokens=tokens,
        iap=IapVerifier(settings.iap_audience) if settings.iap_audience else None,
        directory=None if local else CloudIdentityDirectory(),
        dev_user=settings.dev_user,
    )
