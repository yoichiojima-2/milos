"""HTTP surface. One FastAPI app, two deployments of one image.

`role="public"` mounts the user routes and expects IAP in front; every request
carries a verified identity and the body is never trusted for it.
`role="internal"` mounts the runner, connector and scheduler routes; Cloud Run
IAM restricts invokers, and runners additionally present the session token in
`X-Milos-Session` plus their lease token in `X-Milos-Lease`.
"""

import secrets
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth import IAP_HEADER, Directory, IapVerifier, Principal, SessionTokens
from .errors import Forbidden, MilosError, Unauthorized
from .models import EventType, StopReason
from .service import RunnerEvent, Service

SCHEDULER_ACTOR = "scheduler"


class CreateSession(BaseModel):
    agent_id: str
    message: str
    client_request_id: str = Field(default_factory=lambda: secrets.token_hex(8))
    viewers: list[str] = Field(default_factory=list)
    approvers: list[str] = Field(default_factory=list)


class SendMessage(BaseModel):
    text: str
    client_request_id: str = Field(default_factory=lambda: secrets.token_hex(8))


class Interrupt(BaseModel):
    client_request_id: str = Field(default_factory=lambda: secrets.token_hex(8))


class Confirm(BaseModel):
    tool_use_id: str
    decision: Literal["allow", "deny"]


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

    if role == "public":
        app.include_router(_public(service, iap=iap, directory=directory, dev_user=dev_user))
    elif role == "internal":
        app.include_router(_internal(service, tokens=tokens))
    else:
        raise ValueError(f"unknown API role {role!r}")
    return app


# --- public ---------------------------------------------------------------------


def _public(
    service: Service,
    *,
    iap: IapVerifier | None,
    directory: Directory | None,
    dev_user: str | None,
):
    from fastapi import APIRouter

    router = APIRouter(prefix="/v1")

    async def principal(request: Request) -> Principal:
        if iap is not None:
            return iap.verify(request.headers.get(IAP_HEADER))
        if dev_user:
            return Principal(email=dev_user)
        raise Unauthorized("no identity provider configured")

    User = Annotated[Principal, Depends(principal)]

    async def in_group(email: str, groups: list[str]) -> bool:
        if directory is None:
            return False
        for group in groups:
            if await directory.is_member(email, group):
                return True
        return False

    @router.get("/agents")
    async def list_agents(user: User) -> list[dict[str, Any]]:
        return [
            {"agent": a.model_dump(mode="json"), "version": v.model_dump(mode="json")}
            for a, v in await service.list_agents()
        ]

    @router.get("/agents/{agent_id}")
    async def get_agent(agent_id: str, user: User) -> dict[str, Any]:
        agent, version = await service.get_agent(agent_id)
        return {"agent": agent.model_dump(mode="json"), "version": version.model_dump(mode="json")}

    @router.post("/sessions", status_code=201)
    async def create_session(body: CreateSession, user: User) -> dict[str, Any]:
        _, version = await service.get_agent(body.agent_id)
        if not await in_group(user.email, version.allowed_groups):
            raise Forbidden(f"{user.email} may not start {body.agent_id}")
        for approver in body.approvers:
            if not await in_group(approver, version.allowed_groups):
                raise Forbidden(f"approver {approver} is not allowed to use {body.agent_id}")
        session = await service.create_session(
            body.agent_id,
            body.message,
            operator=user.email,
            client_request_id=body.client_request_id,
            viewers=body.viewers,
            approvers=body.approvers,
        )
        return session.model_dump(mode="json")

    @router.get("/sessions")
    async def list_sessions(user: User) -> list[dict[str, Any]]:
        sessions = await service.list_sessions(operator=user.email)
        return [s.model_dump(mode="json") for s in sessions]

    async def viewable(session_id: str, user: Principal):
        session = await service.get_session(session_id)
        if not service.can_view(session, user.email):
            raise Forbidden("not a participant of this session")
        return session

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: str, user: User) -> dict[str, Any]:
        return (await viewable(session_id, user)).model_dump(mode="json")

    @router.get("/sessions/{session_id}/events")
    async def events(
        session_id: str, user: User, after: Annotated[int, Query(ge=0)] = 0
    ) -> list[dict[str, Any]]:
        await viewable(session_id, user)
        return [e.model_dump(mode="json") for e in await service.events(session_id, after=after)]

    @router.post("/sessions/{session_id}/messages", status_code=201)
    async def send(session_id: str, body: SendMessage, user: User) -> dict[str, Any]:
        session = await viewable(session_id, user)
        if user.email != session.operator:
            raise Forbidden("only the operator sends messages")
        event = await service.accept_message(
            session_id, body.text, actor=user.email, client_request_id=body.client_request_id
        )
        return event.model_dump(mode="json")

    @router.post("/sessions/{session_id}/interrupt", status_code=201)
    async def interrupt(session_id: str, body: Interrupt, user: User) -> dict[str, Any]:
        session = await viewable(session_id, user)
        if user.email != session.operator:
            raise Forbidden("only the operator interrupts")
        event = await service.interrupt(
            session_id, actor=user.email, client_request_id=body.client_request_id
        )
        return event.model_dump(mode="json")

    @router.post("/sessions/{session_id}/approvals", status_code=201)
    async def confirm(session_id: str, body: Confirm, user: User) -> dict[str, Any]:
        session = await service.get_session(session_id)
        _, version = await service.get_agent(session.agent_id)
        if not await in_group(user.email, version.allowed_groups):
            raise Forbidden(f"{user.email} may not approve for {session.agent_id}")
        approval = await service.confirm(
            session_id, body.tool_use_id, body.decision, actor=user.email
        )
        return approval.model_dump(mode="json")

    @router.post("/sessions/{session_id}/terminate")
    async def terminate(session_id: str, user: User) -> dict[str, Any]:
        await viewable(session_id, user)
        return (await service.terminate(session_id, actor=user.email)).model_dump(mode="json")

    return router


# --- internal -------------------------------------------------------------------


def _internal(service: Service, *, tokens: SessionTokens):
    from fastapi import APIRouter

    router = APIRouter(prefix="/internal")

    async def session_of(
        session_id: str, token: Annotated[str | None, Header(alias="X-Milos-Session")] = None
    ) -> str:
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
    async def scheduled_session(body: CreateSession) -> dict[str, Any]:
        """Cloud Scheduler: create a session with the schedule's idempotency key."""
        session = await service.create_session(
            body.agent_id,
            body.message,
            operator=SCHEDULER_ACTOR,
            client_request_id=body.client_request_id,
            viewers=body.viewers,
            approvers=body.approvers,
        )
        return session.model_dump(mode="json")

    @router.post("/inspect")
    async def inspect() -> dict[str, list[str]]:
        return await service.inspect()

    @router.get("/sessions/{session_id}")
    async def context(session_id: Owned) -> dict[str, Any]:
        session = await service.get_session(session_id)
        version_doc = await service.store.get(
            f"agents/{session.agent_id}/versions/{session.agent_version}"
        )
        return {"session": session.model_dump(mode="json"), "version": version_doc or {}}

    @router.post("/sessions/{session_id}/permit")
    async def permit(session_id: Owned, lease: Lease, body: Permit) -> dict[str, Any]:
        decision = await service.permit(
            session_id,
            lease_token=lease,
            tool_use_id=body.tool_use_id,
            tool_name=body.tool_name,
            args=body.args,
        )
        return {"decision": decision.kind, "reason": decision.reason}

    @router.post("/sessions/{session_id}/poll")
    async def poll(session_id: Owned, lease: Lease) -> dict[str, Any]:
        result = await service.poll(session_id, lease_token=lease)
        return {"stop": result.stop, "events": [e.model_dump(mode="json") for e in result.events]}

    @router.post("/sessions/{session_id}/ack")
    async def ack(session_id: Owned, lease: Lease, body: Ack) -> dict[str, str]:
        await service.ack(session_id, lease_token=lease, seq=body.seq)
        return {"status": "ok"}

    @router.post("/sessions/{session_id}/events", status_code=201)
    async def report(session_id: Owned, lease: Lease, body: list[RunnerEventIn]) -> list[dict]:
        events = await service.report(
            session_id,
            [RunnerEvent(e.type, e.payload, e.tool_use_id) for e in body],
            lease_token=lease,
        )
        return [e.model_dump(mode="json") for e in events]

    @router.post("/sessions/{session_id}/snapshot")
    async def snapshot(session_id: Owned, lease: Lease, body: Snapshot) -> dict[str, str]:
        await service.advance_snapshot(session_id, lease_token=lease, number=body.number)
        return {"status": "ok"}

    @router.post("/sessions/{session_id}/finish")
    async def finish(session_id: Owned, lease: Lease, body: Finish) -> dict[str, Any]:
        session = await service.finish(session_id, lease_token=lease, stop_reason=body.stop_reason)
        return session.model_dump(mode="json")

    @router.get("/sessions/{session_id}/permissions")
    async def permission(session_id: Owned, tool_name: str, args_sha256: str) -> dict[str, Any]:
        """Connector check: is this exact call permitted under the current lease?"""
        found = await service.permission(session_id, tool_name=tool_name, args_sha256=args_sha256)
        return {"permitted": found is not None, "tool_use_id": found.tool_use_id if found else None}

    return router


def build_from_env() -> FastAPI:
    """Entry point for `uvicorn milos.api:app`-style deployments."""
    from .audit import CloudAuditLog, StderrAuditLog
    from .auth import CloudIdentityDirectory
    from .jobs import CloudRunJobs, NoJobs
    from .settings import ApiSettings
    from .store import FirestoreStore

    settings = ApiSettings.from_env()
    tokens = SessionTokens(settings.token_key)
    audit = StderrAuditLog() if settings.dev_user else CloudAuditLog(settings.project)
    jobs = (
        NoJobs()
        if settings.dev_user
        else CloudRunJobs(settings.project, settings.region, settings.runner_job)
    )
    service = Service(
        FirestoreStore(project=settings.project),
        audit,
        jobs,
        tokens,
        runner_env=settings.runner_env(),
    )
    return create_app(
        service,
        role=settings.role,
        tokens=tokens,
        iap=IapVerifier(settings.iap_audience) if settings.iap_audience else None,
        directory=None if settings.dev_user else CloudIdentityDirectory(),
        dev_user=settings.dev_user,
    )
