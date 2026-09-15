"""HTTP surface. One FastAPI app, two deployments of one image.

`role="public"` mounts the user routes and expects IAP in front; every request
carries a verified identity and the body is never trusted for it.
`role="internal"` mounts the runner, connector and scheduler routes; Cloud Run
IAM restricts invokers, runners additionally present the session token in
`X-Milos-Session` plus their lease token in `X-Milos-Lease`, and the scheduler
routes verify the caller's Google identity token against `scheduler_sa`.

Authorization rules live in `access.py`; this module only applies them.
"""

from collections.abc import Mapping
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from . import console
from .access import Access, can_decide, can_operate, can_view
from .audit import CloudAuditLog, StderrAuditLog
from .auth import IAP_HEADER, CloudIdentityDirectory, Principal, SessionTokens, TokenVerifier
from .errors import Forbidden, MilosError, Unauthorized
from .jobs import CloudRunJobs, NoJobs
from .models import (
    Ack,
    Agent,
    AgentPatch,
    AgentVersion,
    Approval,
    Event,
    Finish,
    Inspection,
    Me,
    NewApproval,
    NewInterrupt,
    NewMessage,
    NewSession,
    PermissionAnswer,
    PermissionLookup,
    PermissionRequest,
    Polled,
    Published,
    RunnerContext,
    RunnerEvent,
    Session,
    SessionView,
    SnapshotPointer,
)
from .service import Service
from .settings import ApiSettings
from .store import FirestoreStore

SCHEDULER_ACTOR = "scheduler"


def create_app(
    service: Service,
    *,
    role: str,
    tokens: SessionTokens,
    access: Access,
    verifier: TokenVerifier | None = None,
    dev_user: str | None = None,
    scheduler_sa: str | None = None,
    static: Mapping[str, bytes] | None = None,
) -> FastAPI:
    app = FastAPI(title="milos", docs_url=None, redoc_url=None)

    @app.exception_handler(MilosError)
    async def on_error(_: Request, error: MilosError) -> JSONResponse:
        return JSONResponse({"error": type(error).__name__, "detail": str(error)}, error.status)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "role": role}

    match role:
        case "public":
            app.include_router(_public(service, access=access, verifier=verifier, dev_user=dev_user))
            if static:
                console.mount(app, static)  # the console page and its assets; the routers above take precedence
        case "internal":
            app.include_router(_internal(service, tokens=tokens, verifier=verifier, scheduler_sa=scheduler_sa))
        case _:
            raise ValueError(f"unknown API role {role!r}")
    return app


# --- public ---------------------------------------------------------------------


def _public(service: Service, *, access: Access, verifier: TokenVerifier | None, dev_user: str | None) -> APIRouter:
    router = APIRouter(prefix="/v1")

    async def principal(request: Request) -> Principal:
        if verifier is not None:
            return verifier.verify(request.headers.get(IAP_HEADER))
        if dev_user:
            return Principal(email=dev_user)
        raise Unauthorized("no identity provider configured")

    User = Annotated[Principal, Depends(principal)]

    async def require_member(email: str, groups: list[str], message: str) -> None:
        if not await access.member(email, groups):
            raise Forbidden(message)

    async def admin(user: User) -> Principal:
        if not await access.admin(user.email):
            raise Forbidden(f"{user.email} is not in the admin group")
        return user

    async def viewable(session_id: str, user: User) -> Session:
        session = await service.get_session(session_id)
        if not can_view(session, user.email):
            raise Forbidden("not a participant of this session")
        return session

    async def operated(session: Annotated[Session, Depends(viewable)], user: User) -> Session:
        if not can_operate(session, user.email):
            raise Forbidden("only the operator may do this")
        return session

    Admin = Annotated[Principal, Depends(admin)]
    Viewable = Annotated[Session, Depends(viewable)]
    Operated = Annotated[Session, Depends(operated)]

    @router.get("/me")
    async def me(user: User) -> Me:
        """The caller as the API sees them; the console's only source of identity."""
        return Me(email=user.email, admin=await access.admin(user.email), now=service.now())

    @router.get("/agents")
    async def list_agents(user: User) -> list[Published]:
        return [Published(agent=a, version=v) for a, v in await service.list_agents()]

    @router.get("/agents/{agent_id}")
    async def get_agent(agent_id: str, user: User) -> Published:
        agent, version = await service.get_agent(agent_id)
        return Published(agent=agent, version=version)

    @router.post("/agents", status_code=201)
    async def publish(body: AgentVersion, user: Admin) -> AgentVersion:
        """Publish a validated definition as its agent's next version."""
        return await service.publish(body)

    @router.patch("/agents/{agent_id}")
    async def patch_agent(agent_id: str, body: AgentPatch, user: Admin) -> Agent:
        return await service.set_enabled(agent_id, body.enabled)

    @router.post("/sessions", status_code=201)
    async def create_session(body: NewSession, user: User) -> SessionView:
        _, version = await service.get_agent(body.agent_id)
        await require_member(user.email, version.allowed_groups, f"{user.email} may not start {body.agent_id}")
        for approver in body.approvers:
            await require_member(approver, version.allowed_groups, f"approver {approver} is not allowed to use {body.agent_id}")
        session = await service.create_session(
            body.agent_id,
            body.message,
            operator=user.email,
            client_request_id=body.client_request_id,
            viewers=body.viewers,
            approvers=body.approvers,
        )
        return SessionView.of(session)

    @router.get("/sessions")
    async def list_sessions(user: User, role: Literal["operator", "approver"] = "operator") -> list[SessionView]:
        """`role=operator`: sessions the caller started; `role=approver`: sessions naming the caller as approver."""
        if role == "approver":
            sessions = await service.list_sessions(approver=user.email)
        else:
            sessions = await service.list_sessions(operator=user.email)
        return [SessionView.of(s) for s in sessions]

    @router.get("/sessions/{session_id}")
    async def get_session(session: Viewable) -> SessionView:
        return SessionView.of(session)

    @router.get("/sessions/{session_id}/events")
    async def events(session: Viewable, after: Annotated[int, Query(ge=0)] = 0) -> list[Event]:
        return await service.events(session.session_id, after=after)

    @router.post("/sessions/{session_id}/messages", status_code=201)
    async def send(session: Operated, body: NewMessage, user: User) -> Event:
        return await service.accept_message(
            session.session_id, body.text, actor=user.email, client_request_id=body.client_request_id
        )

    @router.post("/sessions/{session_id}/interrupt", status_code=201)
    async def interrupt(session: Operated, body: NewInterrupt, user: User) -> Event:
        return await service.interrupt(session.session_id, actor=user.email, client_request_id=body.client_request_id)

    @router.post("/sessions/{session_id}/approvals", status_code=201)
    async def decide(session_id: str, body: NewApproval, user: User) -> Approval:
        # Approvers need not be participants: anyone in the agent's groups but the operator,
        # or the named approvers when the session lists some.
        session = await service.get_session(session_id)
        if not can_decide(session, user.email):
            raise Forbidden(f"{user.email} may not decide for this session")
        _, version = await service.get_agent(session.agent_id)
        await require_member(user.email, version.allowed_groups, f"{user.email} may not approve for {session.agent_id}")
        return await service.decide(session_id, body.tool_use_id, verdict=body.verdict, by=user.email)

    @router.post("/sessions/{session_id}/terminate")
    async def terminate(session: Operated, user: User) -> SessionView:
        return SessionView.of(await service.terminate(session.session_id, actor=user.email))

    return router


# --- internal -------------------------------------------------------------------


def _internal(
    service: Service, *, tokens: SessionTokens, verifier: TokenVerifier | None, scheduler_sa: str | None
) -> APIRouter:
    router = APIRouter(prefix="/internal")

    async def scheduler(request: Request) -> None:
        """Only the scheduler identity creates scheduled sessions or runs inspection."""
        if scheduler_sa is None:
            return  # local development: no identity to check
        if verifier is None:
            raise Unauthorized("scheduler identity cannot be verified without a token verifier")
        bearer = request.headers.get("Authorization", "")
        caller = verifier.verify(bearer.removeprefix("Bearer ").strip() or None)
        if caller.email != scheduler_sa:
            raise Forbidden(f"{caller.email} is not the scheduler")

    Scheduler = Annotated[None, Depends(scheduler)]

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
        body: NewSession,
        _: Scheduler,
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
    async def inspect(_: Scheduler) -> Inspection:
        return await service.inspect()

    @router.get("/sessions/{session_id}")
    async def context(session_id: Owned) -> RunnerContext:
        session, version = await service.context(session_id)
        return RunnerContext(session=session, version=version)

    @router.post("/sessions/{session_id}/permissions")
    async def permit(session_id: Owned, lease: Lease, body: PermissionRequest) -> PermissionAnswer:
        return await service.permit(
            session_id, lease_token=lease, tool_use_id=body.tool_use_id, tool_name=body.tool_name, args=body.args
        )

    @router.post("/sessions/{session_id}/poll")
    async def poll(session_id: Owned, lease: Lease) -> Polled:
        return await service.poll(session_id, lease_token=lease)

    @router.post("/sessions/{session_id}/ack")
    async def ack(session_id: Owned, lease: Lease, body: Ack) -> Session:
        return await service.ack(session_id, lease_token=lease, seq=body.seq)

    @router.post("/sessions/{session_id}/events", status_code=201)
    async def report(session_id: Owned, lease: Lease, body: list[RunnerEvent]) -> list[Event]:
        return await service.report(session_id, body, lease_token=lease)

    @router.post("/sessions/{session_id}/snapshot")
    async def snapshot(session_id: Owned, lease: Lease, body: SnapshotPointer) -> Session:
        return await service.advance_snapshot(session_id, lease_token=lease, number=body.number)

    @router.post("/sessions/{session_id}/finish")
    async def finish(session_id: Owned, lease: Lease, body: Finish) -> Session:
        return await service.finish(session_id, lease_token=lease, stop_reason=body.stop_reason)

    @router.get("/sessions/{session_id}/permissions")
    async def permission(session_id: Owned, tool_name: str, args_sha256: str) -> PermissionLookup:
        """Connector check: is this exact call permitted under the current lease?"""
        found = await service.permission(session_id, tool_name=tool_name, args_sha256=args_sha256)
        return PermissionLookup(permitted=found is not None, tool_use_id=found.tool_use_id if found else None)

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
    if settings.api_role == "public":
        verifier = TokenVerifier.for_iap(settings.iap_audience) if settings.iap_audience else None
    else:
        verifier = TokenVerifier(settings.internal_url) if settings.scheduler_sa else None
    return create_app(
        service,
        role=settings.api_role,
        tokens=tokens,
        access=Access(None if local else CloudIdentityDirectory(), admin_group=settings.admin_group),
        verifier=verifier,
        dev_user=settings.dev_user,
        scheduler_sa=settings.scheduler_sa,
        static=console.load_static() if settings.api_role == "public" else None,
    )
