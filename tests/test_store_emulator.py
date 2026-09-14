"""The Firestore implementation against the emulator: `FIRESTORE_EMULATOR_HOST=127.0.0.1:8081 uv run pytest -m emulator`.

Skipped without the emulator. These tests cover what the in-memory fake only
mirrors: create-only conflicts, the read-before-write rule, enum filter values,
and the service's own flow on real transactions.
"""

from __future__ import annotations

import uuid

import pytest

from milos.errors import AlreadyExists
from milos.models import Outcome, SessionStatus, StopReason, Verdict
from milos.service import Service
from milos.store import FirestoreStore

from .conftest import EMULATOR_HOST, definition

pytestmark = pytest.mark.emulator


@pytest.fixture
def firestore(monkeypatch) -> FirestoreStore:
    if not EMULATOR_HOST:
        pytest.skip("FIRESTORE_EMULATOR_HOST is not set")
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", EMULATOR_HOST)
    return FirestoreStore(project=f"emu-{uuid.uuid4().hex[:8]}")  # a fresh project per test: an empty database


async def test_create_only_and_read_before_write(firestore):
    async def create(tx):
        tx.create("things/one", {"n": 1})

    await firestore.transaction(create)
    with pytest.raises(AlreadyExists):
        await firestore.transaction(create)

    async def read_after_write(tx):
        tx.update("things/one", {"n": 2})
        await tx.get("things/one")

    with pytest.raises(Exception, match="(?i)read"):
        await firestore.transaction(read_after_write)


async def test_enum_filter_values_and_limit(firestore):
    async def seed(tx):
        for i, status in enumerate((SessionStatus.RUNNING, SessionStatus.IDLE, SessionStatus.RUNNING)):
            tx.create(f"sessions/s{i}", {"status": status.value, "seq": i})

    await firestore.transaction(seed)
    running = await firestore.query("sessions", where=[("status", "in", (SessionStatus.RUNNING,))], order_by="seq")
    assert [d["seq"] for d in running] == [0, 2]
    assert len(await firestore.query("sessions", where=[("status", "==", SessionStatus.RUNNING)], limit=1)) == 1
    assert await firestore.query("sessions", limit=0) == []


async def test_service_flow_on_real_transactions(firestore, audit, jobs, tokens, clock):
    """Start, park, decide, resume, finish: the fake's semantics hold on the real store."""
    service = Service(firestore, audit, jobs, tokens, runner_env={}, now=clock)
    await service.publish(definition())
    session = await service.create_session("analyst", "hi", operator="alice@example.com", client_request_id="r1")
    assert session.lease is not None
    again = await service.create_session("analyst", "hi", operator="alice@example.com", client_request_id="r1")
    assert again.session_id == session.session_id and len(jobs.launched) == 1

    parked = await service.permit(
        session.session_id, lease_token=session.lease.token, tool_use_id="t1", tool_name="Bash", args={}
    )
    assert parked.outcome == Outcome.REQUIRE_APPROVAL
    assert (await service.get_session(session.session_id)).stop_reason == StopReason.REQUIRES_ACTION
    await service.finish(session.session_id, lease_token=session.lease.token, stop_reason=StopReason.REQUIRES_ACTION)

    await service.decide(session.session_id, "t1", verdict=Verdict.ALLOW, by="bob@example.com")
    resumed = await service.get_session(session.session_id)
    assert resumed.status == SessionStatus.RUNNING and resumed.lease is not None and len(jobs.launched) == 2
    allowed = await service.permit(
        session.session_id, lease_token=resumed.lease.token, tool_use_id="t2", tool_name="Bash", args={}
    )
    assert allowed.outcome == Outcome.ALLOW and allowed.approval_tool_use_id == "t1"
    # the approval is consumed once: the same call again waits for a new one
    again = await service.permit(
        session.session_id, lease_token=resumed.lease.token, tool_use_id="t3", tool_name="Bash", args={}
    )
    assert again.outcome == Outcome.REQUIRE_APPROVAL
    # permissions are create-only on the real store too
    await service.permit(session.session_id, lease_token=resumed.lease.token, tool_use_id="r1", tool_name="Read", args={})
    with pytest.raises(AlreadyExists):
        await service.permit(session.session_id, lease_token=resumed.lease.token, tool_use_id="r1", tool_name="Read", args={})
    await service.decide(session.session_id, "t3", verdict=Verdict.DENY, by="bob@example.com")
    resumed = await service.get_session(session.session_id)
    assert resumed.lease is not None

    await service.ack(session.session_id, lease_token=resumed.lease.token, seq=1)
    finished = await service.finish(session.session_id, lease_token=resumed.lease.token, stop_reason=StopReason.END_TURN)
    assert finished.status == SessionStatus.IDLE and finished.lease is None
    assert [s.session_id for s in await service.list_sessions(operator="alice@example.com")] == [session.session_id]
