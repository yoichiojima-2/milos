"""The HTTP surface: identity comes from IAP or the session token, never the body."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from milos.access import Access
from milos.api import create_app
from milos.auth import IAP_HEADER, Principal, TokenVerifier
from milos.errors import Unauthorized
from milos.models import sha256_json

from .conftest import definition


class FakeIap(TokenVerifier):
    """Treats the assertion header as the email itself."""

    def __init__(self) -> None:
        super().__init__(audience="test")

    def verify(self, token: str | None) -> Principal:
        if not token:
            raise Unauthorized("missing IAP assertion")
        return Principal(email=token)


@pytest.fixture
def public(service, tokens, access):
    app = create_app(service, role="public", tokens=tokens, verifier=FakeIap(), access=access)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://public")


@pytest.fixture
def internal(service, tokens, access):
    app = create_app(service, role="internal", tokens=tokens, access=access)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://internal")


def as_user(email: str) -> dict[str, str]:
    return {IAP_HEADER: email}


async def test_public_requires_iap(public, agent):
    response = await public.get("/v1/agents")
    assert response.status_code == 401


async def test_create_session_checks_group_membership(public, agent):
    body = {"agent_id": "analyst", "message": "hi", "client_request_id": "r1"}
    denied = await public.post("/v1/sessions", json=body, headers=as_user("x@other.org"))
    assert denied.status_code == 403
    created = await public.post("/v1/sessions", json=body, headers=as_user("alice@example.com"))
    assert created.status_code == 201
    session = created.json()
    assert session["operator"] == "alice@example.com" and session["status"] == "running"


async def test_only_participants_see_a_session(public, session):
    sid = session.session_id
    assert (await public.get(f"/v1/sessions/{sid}", headers=as_user("alice@example.com"))).status_code == 200
    assert (await public.get(f"/v1/sessions/{sid}", headers=as_user("bob@example.com"))).status_code == 403
    events = await public.get(f"/v1/sessions/{sid}/events", params={"after": 1}, headers=as_user("alice@example.com"))
    assert [e["seq"] for e in events.json()] == [2]


async def test_approval_over_http(public, internal, service, session, tokens):
    sid = session.session_id
    headers = {"X-Milos-Session": tokens.issue(sid), "X-Milos-Lease": session.lease.token}
    permit = await internal.post(
        f"/internal/sessions/{sid}/permissions",
        json={"tool_use_id": "t1", "tool_name": "Bash", "args": {"command": "ls"}},
        headers=headers,
    )
    assert permit.json()["outcome"] == "require_approval"
    own = await public.post(
        f"/v1/sessions/{sid}/approvals",
        json={"tool_use_id": "t1", "verdict": "allow"},
        headers=as_user("alice@example.com"),
    )
    assert own.status_code == 403
    other = await public.post(
        f"/v1/sessions/{sid}/approvals",
        json={"tool_use_id": "t1", "verdict": "allow"},
        headers=as_user("bob@example.com"),
    )
    assert other.status_code == 201 and other.json()["decided_by"] == "bob@example.com"


async def test_internal_rejects_tokens_for_other_sessions(internal, service, session, tokens, agent):
    other = await service.create_session("analyst", "x", operator="alice@example.com", client_request_id="r2")
    headers = {
        "X-Milos-Session": tokens.issue(other.session_id),
        "X-Milos-Lease": session.lease.token,
    }
    response = await internal.post(f"/internal/sessions/{session.session_id}/poll", headers=headers)
    assert response.status_code == 401
    forged = {
        "X-Milos-Session": f"{session.session_id}.deadbeef",
        "X-Milos-Lease": session.lease.token,
    }
    assert (await internal.post(f"/internal/sessions/{session.session_id}/poll", headers=forged)).status_code == 401


async def test_runner_round_trip(internal, service, session, tokens):
    sid = session.session_id
    headers = {"X-Milos-Session": tokens.issue(sid), "X-Milos-Lease": session.lease.token}
    context = await internal.get(f"/internal/sessions/{sid}", headers=headers)
    assert context.json()["version"]["model"].startswith("claude-")
    poll = await internal.post(f"/internal/sessions/{sid}/poll", headers=headers)
    assert poll.json()["stop"] is False and poll.json()["events"][0]["type"] == "user.message"
    assert (await internal.post(f"/internal/sessions/{sid}/ack", json={"seq": 2}, headers=headers)).status_code == 200
    reported = await internal.post(
        f"/internal/sessions/{sid}/events",
        json=[{"type": "agent.message", "payload": {"text": "done"}}],
        headers=headers,
    )
    assert reported.status_code == 201 and reported.json()[0]["seq"] == 3
    assert (await internal.post(f"/internal/sessions/{sid}/snapshot", json={"number": 1}, headers=headers)).status_code == 200
    finished = await internal.post(f"/internal/sessions/{sid}/finish", json={"stop_reason": "end_turn"}, headers=headers)
    assert finished.json()["status"] == "idle"
    stale = await internal.post(f"/internal/sessions/{sid}/poll", headers=headers)
    assert stale.status_code == 403


async def test_connector_permission_check(internal, service, session, tokens):
    sid = session.session_id
    headers = {"X-Milos-Session": tokens.issue(sid), "X-Milos-Lease": session.lease.token}
    await internal.post(
        f"/internal/sessions/{sid}/permissions",
        json={"tool_use_id": "t1", "tool_name": "Read", "args": {"path": "x"}},
        headers=headers,
    )
    found = await internal.get(
        f"/internal/sessions/{sid}/permissions",
        params={"tool_name": "Read", "args_sha256": sha256_json({"path": "x"})},
        headers=headers,
    )
    assert found.json() == {"permitted": True, "tool_use_id": "t1"}


async def test_scheduler_creates_idempotent_sessions(internal, agent, jobs):
    body = {"agent_id": "analyst", "message": "weekly"}
    headers = {
        "X-CloudScheduler-JobName": "weekly",
        "X-CloudScheduler-ScheduleTime": "2026-09-14T00:00:00+09:00",
    }
    first = await internal.post("/internal/sessions", json=body, headers=headers)
    second = await internal.post("/internal/sessions", json=body, headers=headers)
    assert first.status_code == 201 and first.json()["session_id"] == second.json()["session_id"]
    assert first.json()["operator"] == "scheduler"
    assert len(jobs.launched) == 1


async def test_inspect_endpoint(internal, session, clock):
    clock.advance(61)
    response = await internal.post("/internal/inspect")
    assert response.json()["restarted"] == [session.session_id]


async def test_terminate_over_http(public, service, agent):
    session = await service.create_session(
        "analyst", "hi", operator="alice@example.com", client_request_id="r", viewers=["viewer@example.com"]
    )
    sid = session.session_id
    # a viewer may watch but not end the session; the operator may
    assert (await public.post(f"/v1/sessions/{sid}/terminate", headers=as_user("viewer@example.com"))).status_code == 403
    response = await public.post(f"/v1/sessions/{sid}/terminate", headers=as_user("alice@example.com"))
    assert response.json()["status"] == "terminated"


async def test_publish_requires_admin_group(public, service):
    from .conftest import definition

    body = definition().model_dump(mode="json")
    assert (await public.post("/v1/agents", json=body, headers=as_user("alice@example.com"))).status_code == 403
    published = await public.post("/v1/agents", json=body, headers=as_user("admin@example.com"))
    assert published.status_code == 201 and published.json()["version"] == 1
    disabled = await public.patch("/v1/agents/analyst", json={"enabled": False}, headers=as_user("admin@example.com"))
    assert disabled.json()["enabled"] is False
    assert (
        await public.patch("/v1/agents/analyst", json={"enabled": True}, headers=as_user("alice@example.com"))
    ).status_code == 403
    assert (await public.get("/v1/agents", headers=as_user("alice@example.com"))).json()[0]["agent"]["enabled"] is False


async def test_scheduler_identity_is_verified(service, tokens, access, agent):
    app = create_app(
        service, role="internal", tokens=tokens, access=access, verifier=FakeIap(), scheduler_sa="scheduler@example.com"
    )
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://internal")
    body = {"agent_id": "analyst", "message": "weekly"}
    assert (await client.post("/internal/sessions", json=body)).status_code == 401
    runner = {"Authorization": "Bearer runner@example.com"}
    assert (await client.post("/internal/sessions", json=body, headers=runner)).status_code == 403
    assert (await client.post("/internal/inspect", headers=runner)).status_code == 403
    scheduler = {"Authorization": "Bearer scheduler@example.com"}
    assert (await client.post("/internal/sessions", json=body, headers=scheduler)).status_code == 201
    assert (await client.post("/internal/inspect", headers=scheduler)).status_code == 200


async def test_local_mode_without_a_directory_admits_everyone(service, tokens, agent):
    app = create_app(service, role="public", tokens=tokens, access=Access(None), dev_user="dev@example.com")
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://public")
    created = await client.post("/v1/sessions", json={"agent_id": "analyst", "message": "hi"})
    assert created.status_code == 201
    body = definition(purpose="local").model_dump(mode="json")
    assert (await client.post("/v1/agents", json=body)).status_code == 201


async def test_approver_lists_sessions_naming_them(public, agent):
    body = {"agent_id": "analyst", "message": "hi", "approvers": ["lead@example.com"]}
    created = await public.post("/v1/sessions", json=body, headers=as_user("alice@example.com"))
    assert created.status_code == 201
    as_lead = as_user("lead@example.com")
    assert (await public.get("/v1/sessions", headers=as_lead)).json() == []
    approving = await public.get("/v1/sessions", params={"role": "approver"}, headers=as_lead)
    assert [s["session_id"] for s in approving.json()] == [created.json()["session_id"]]
    assert (await public.get("/v1/sessions", params={"role": "owner"}, headers=as_lead)).status_code == 422
