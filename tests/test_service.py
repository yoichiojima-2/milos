"""The invariants (data model §3) and the acceptance table (build & verify §7)."""

from __future__ import annotations

import pytest

from milos.errors import AlreadyExists, Conflict, Forbidden, Invalid, NotFound, Stopped
from milos.models import EventType, SessionStatus, StopReason
from milos.service import RunnerEvent

from .conftest import definition


def lease(session):
    return session.lease.token


async def test_create_session_writes_first_event_and_launches(service, session, jobs, store):
    assert session.status == SessionStatus.RUNNING
    assert session.agent_version == 1
    events = await service.events(session.session_id)
    assert [e.type for e in events] == [EventType.USER_MESSAGE, EventType.SESSION_STATUS]
    assert [e.seq for e in events] == [1, 2]
    launched = jobs.launched[0]
    assert launched["env"]["MILOS_LEASE_TOKEN"] == session.lease.token
    assert launched["env"]["MILOS_SESSION_TOKEN"].startswith(session.session_id + ".")
    assert launched["agent_id"] == "analyst"
    assert "MILOS_API_URL" in launched["env"]


async def test_create_session_rejects_missing_definition(service):
    with pytest.raises(NotFound):
        await service.create_session("ghost", "hi", operator="a@example.com", client_request_id="r")


async def test_create_session_rejects_disabled_agent(service, agent):
    await service.set_enabled("analyst", False)
    with pytest.raises(Stopped):
        await service.create_session("analyst", "hi", operator="a@example.com", client_request_id="r")


async def test_create_session_enforces_concurrency(service, agent):
    for i in range(2):
        await service.create_session("analyst", "hi", operator="a@example.com", client_request_id=f"r{i}")
    with pytest.raises(Invalid):
        await service.create_session("analyst", "hi", operator="a@example.com", client_request_id="r9")


async def test_create_session_is_idempotent_on_client_request_id(service, agent, jobs):
    first = await service.create_session("analyst", "hi", operator="a@example.com", client_request_id="sched-1")
    again = await service.create_session("analyst", "hi", operator="a@example.com", client_request_id="sched-1")
    assert again.session_id == first.session_id
    assert len(jobs.launched) == 1
    with pytest.raises(Conflict):
        await service.create_session("analyst", "other", operator="a@example.com", client_request_id="sched-1")


# --- invariant 3: idempotent input ------------------------------------------


async def test_message_replay_returns_same_event(service, session):
    sid = session.session_id
    a = await service.accept_message(sid, "more", actor="alice@example.com", client_request_id="m1")
    b = await service.accept_message(sid, "more", actor="alice@example.com", client_request_id="m1")
    assert a.event_id == b.event_id and a.seq == 3
    with pytest.raises(Conflict):
        await service.accept_message(sid, "different", actor="alice@example.com", client_request_id="m1")


async def test_message_to_idle_session_relaunches(service, session, jobs):
    sid = session.session_id
    await service.ack(sid, lease_token=lease(session), seq=1)
    await service.finish(sid, lease_token=lease(session), stop_reason=StopReason.END_TURN)
    assert (await service.get_session(sid)).status == SessionStatus.IDLE
    await service.accept_message(sid, "again", actor="alice@example.com", client_request_id="m2")
    after = await service.get_session(sid)
    assert after.status == SessionStatus.RUNNING
    assert after.lease.token != lease(session)
    assert len(jobs.launched) == 2


async def test_message_arriving_during_shutdown_starts_a_new_run(service, session, jobs):
    sid = session.session_id
    await service.ack(sid, lease_token=lease(session), seq=1)
    await service.accept_message(sid, "late", actor="alice@example.com", client_request_id="m3")
    await service.finish(sid, lease_token=lease(session), stop_reason=StopReason.END_TURN)
    assert (await service.get_session(sid)).status == SessionStatus.RUNNING
    assert len(jobs.launched) == 2


# --- invariant 5: lease token -------------------------------------------------


async def test_stale_lease_is_rejected(service, session):
    sid = session.session_id
    with pytest.raises(Forbidden):
        await service.poll(sid, lease_token="old")
    with pytest.raises(Forbidden):
        await service.report(sid, [RunnerEvent(EventType.AGENT_MESSAGE, {"text": "x"})], lease_token="old")
    with pytest.raises(Forbidden):
        await service.permit(sid, lease_token="old", tool_use_id="t", tool_name="Read", args={})


async def test_runner_may_only_append_its_own_event_types(service, session):
    with pytest.raises(Invalid):
        await service.report(
            session.session_id,
            [RunnerEvent(EventType.USER_MESSAGE, {"text": "forged"})],
            lease_token=lease(session),
        )


# --- journal before tool (REQ-D-09) ------------------------------------------


async def test_allowed_tool_is_journaled_audited_then_permitted(service, session, audit, store):
    sid = session.session_id
    decision = await service.permit(sid, lease_token=lease(session), tool_use_id="t1", tool_name="Read", args={"path": "a.csv"})
    assert decision.kind == "allow"
    types = [e.type for e in await service.events(sid)]
    assert types[-2:] == [EventType.AGENT_TOOL_USE, EventType.TOOL_PERMITTED]
    assert audit.entries[0]["tool_use_id"] == "t1" and "args" not in audit.entries[0]
    assert store.docs[f"sessions/{sid}/permissions/t1"]["lease_token"] == lease(session)


async def test_audit_failure_means_no_permission(service, session, audit, store):
    audit.fail = True
    with pytest.raises(RuntimeError):
        await service.permit(
            session.session_id,
            lease_token=lease(session),
            tool_use_id="t1",
            tool_name="Read",
            args={},
        )
    assert f"sessions/{session.session_id}/permissions/t1" not in store.docs
    # the request itself is still on record
    assert [e.type for e in await service.events(session.session_id)][-1] == EventType.AGENT_TOOL_USE


async def test_unlisted_tool_is_denied(service, session):
    decision = await service.permit(
        session.session_id,
        lease_token=lease(session),
        tool_use_id="t",
        tool_name="WebFetch",
        args={},
    )
    assert decision.kind == "deny"


async def test_permission_is_create_only(service, session, store):
    sid = session.session_id
    await service.permit(sid, lease_token=lease(session), tool_use_id="t1", tool_name="Read", args={})
    with pytest.raises(AlreadyExists):
        await service.permit(sid, lease_token=lease(session), tool_use_id="t1", tool_name="Read", args={})


# --- approvals (REQ-D-17) -----------------------------------------------------


async def test_approval_flow_parks_session_and_resumes_on_decision(service, session, jobs, clock):
    sid = session.session_id
    decision = await service.permit(
        sid,
        lease_token=lease(session),
        tool_use_id="t1",
        tool_name="Bash",
        args={"command": "rm x"},
    )
    assert decision.kind == "require_confirmation"
    parked = await service.get_session(sid)
    assert parked.status == SessionStatus.IDLE
    assert parked.stop_reason == StopReason.REQUIRES_ACTION
    assert parked.pending_tool_use_ids == ["t1"]
    assert parked.approval_expires_at == clock.now.replace() + __import__("datetime").timedelta(seconds=600)
    await service.finish(sid, lease_token=lease(session), stop_reason=StopReason.REQUIRES_ACTION)

    with pytest.raises(Forbidden):
        await service.confirm(sid, "t1", "allow", actor="alice@example.com")  # the operator
    approval = await service.confirm(sid, "t1", "allow", actor="bob@example.com")
    assert approval.decided_by == "bob@example.com"
    resumed = await service.get_session(sid)
    assert resumed.status == SessionStatus.RUNNING and resumed.pending_tool_use_ids == []
    assert len(jobs.launched) == 2

    # the re-issued call (new id, same content) consumes the approval once
    again = await service.permit(
        sid,
        lease_token=lease(resumed),
        tool_use_id="t2",
        tool_name="Bash",
        args={"command": "rm x"},
    )
    assert again.kind == "allow" and again.approval_tool_use_id == "t1"
    third = await service.permit(
        sid,
        lease_token=lease(resumed),
        tool_use_id="t3",
        tool_name="Bash",
        args={"command": "rm x"},
    )
    assert third.kind == "require_confirmation"


async def test_changed_arguments_need_a_new_approval(service, session, jobs):
    sid = session.session_id
    await service.permit(sid, lease_token=lease(session), tool_use_id="t1", tool_name="Bash", args={"command": "ls"})
    await service.confirm(sid, "t1", "allow", actor="bob@example.com")
    resumed = await service.get_session(sid)
    changed = await service.permit(
        sid,
        lease_token=lease(resumed),
        tool_use_id="t2",
        tool_name="Bash",
        args={"command": "ls -a"},
    )
    assert changed.kind == "require_confirmation"


async def test_denied_approval_denies_the_reissued_call(service, session):
    sid = session.session_id
    await service.permit(sid, lease_token=lease(session), tool_use_id="t1", tool_name="Bash", args={"command": "ls"})
    await service.confirm(sid, "t1", "deny", actor="bob@example.com")
    resumed = await service.get_session(sid)
    again = await service.permit(sid, lease_token=lease(resumed), tool_use_id="t2", tool_name="Bash", args={"command": "ls"})
    assert again.kind == "deny" and "bob@example.com" in again.reason


async def test_approval_is_create_only(service, session):
    sid = session.session_id
    await service.permit(sid, lease_token=lease(session), tool_use_id="t1", tool_name="Bash", args={})
    await service.confirm(sid, "t1", "allow", actor="bob@example.com")
    with pytest.raises(Invalid):
        await service.confirm(sid, "t1", "deny", actor="carol@example.com")


async def test_expired_approval_is_recorded_as_timed_out_deny(service, session, clock, store, jobs):
    sid = session.session_id
    await service.permit(sid, lease_token=lease(session), tool_use_id="t1", tool_name="Bash", args={})
    clock.advance(601)
    report = await service.inspect()
    assert report["expired"] == [sid]
    approval = store.docs[f"sessions/{sid}/approvals/t1"]
    assert approval["decision"] == "deny" and approval["timed_out"] is True
    with pytest.raises(Invalid):
        await service.confirm(sid, "t1", "allow", actor="bob@example.com")
    assert f"sessions/{sid}/permissions/t1" not in store.docs
    assert (await service.get_session(sid)).status == SessionStatus.RUNNING  # resumed to learn the denial


async def test_only_listed_approvers_may_decide(service, agent):
    session = await service.create_session(
        "analyst",
        "hi",
        operator="alice@example.com",
        client_request_id="r",
        approvers=["lead@example.com"],
    )
    sid = session.session_id
    await service.permit(sid, lease_token=lease(session), tool_use_id="t1", tool_name="Bash", args={})
    with pytest.raises(Forbidden):
        await service.confirm(sid, "t1", "allow", actor="bob@example.com")
    await service.confirm(sid, "t1", "allow", actor="lead@example.com")


# --- stop (REQ-D-18) ----------------------------------------------------------


async def test_terminate_denies_every_further_permission(service, session):
    sid = session.session_id
    await service.terminate(sid, actor="alice@example.com")
    decision = await service.permit(sid, lease_token=lease(session), tool_use_id="t1", tool_name="Read", args={})
    assert decision.kind == "stop"
    assert (await service.poll(sid, lease_token=lease(session))).stop is True
    with pytest.raises(Stopped):
        await service.accept_message(sid, "x", actor="alice@example.com", client_request_id="m")


async def test_disabled_agent_stops_running_sessions(service, session):
    await service.set_enabled("analyst", False)
    decision = await service.permit(session.session_id, lease_token=lease(session), tool_use_id="t1", tool_name="Read", args={})
    assert decision.kind == "stop"
    assert (await service.poll(session.session_id, lease_token=lease(session))).stop is True


async def test_stop_between_request_and_grant_creates_no_permission(service, session, store, audit, monkeypatch):
    sid = session.session_id
    original = audit.write

    def write_then_terminate(entry):
        original(entry)
        import asyncio

        asyncio.get_event_loop().run_until_complete  # noqa: B018 (documenting intent)
        store.docs[f"sessions/{sid}"]["status"] = "terminated"

    audit.write = write_then_terminate
    with pytest.raises(Stopped):
        await service.permit(sid, lease_token=lease(session), tool_use_id="t1", tool_name="Read", args={})
    assert f"sessions/{sid}/permissions/t1" not in store.docs


# --- runner loop ---------------------------------------------------------------


async def test_poll_returns_unconsumed_user_events_and_updates_heartbeat(service, session, clock):
    sid = session.session_id
    clock.advance(5)
    poll = await service.poll(sid, lease_token=lease(session))
    assert poll.stop is False
    assert [e.type for e in poll.events] == [EventType.USER_MESSAGE]
    assert (await service.get_session(sid)).lease.last_poll_at == clock.now
    await service.ack(sid, lease_token=lease(session), seq=poll.events[-1].seq)
    assert (await service.poll(sid, lease_token=lease(session))).events == []


async def test_interrupt_is_delivered_through_poll(service, session):
    sid = session.session_id
    await service.ack(sid, lease_token=lease(session), seq=2)
    await service.interrupt(sid, actor="alice@example.com", client_request_id="i1")
    poll = await service.poll(sid, lease_token=lease(session))
    assert [e.type for e in poll.events] == [EventType.USER_INTERRUPT]


async def test_snapshot_pointer_advances_in_order(service, session):
    sid = session.session_id
    with pytest.raises(Invalid):
        await service.advance_snapshot(sid, lease_token=lease(session), number=2)
    await service.advance_snapshot(sid, lease_token=lease(session), number=1)
    assert (await service.get_session(sid)).snapshot == 1


async def test_stalled_run_is_rescheduled_once_then_needs_attention(service, session, clock, jobs):
    sid = session.session_id
    clock.advance(61)
    assert (await service.inspect())["restarted"] == [sid]
    rescheduled = await service.get_session(sid)
    assert rescheduled.status == SessionStatus.RESCHEDULING
    assert rescheduled.lease.token != lease(session)
    assert len(jobs.launched) == 2
    # the old job cannot write any more
    with pytest.raises(Forbidden):
        await service.report(
            sid,
            [RunnerEvent(EventType.AGENT_MESSAGE, {"text": "late"})],
            lease_token=lease(session),
        )
    # the new runner checks in and the session is running again
    await service.poll(sid, lease_token=rescheduled.lease.token)
    clock.advance(61)
    assert (await service.inspect())["attention"] == [sid]
    assert (await service.get_session(sid)).stop_reason == StopReason.NEEDS_ATTENTION


async def test_budget_reached_is_recorded(service, session):
    sid = session.session_id
    await service.report(
        sid,
        [RunnerEvent(EventType.SESSION_USAGE, {"total_cost_usd": 5.2, "num_turns": 20})],
        lease_token=lease(session),
    )
    await service.finish(sid, lease_token=lease(session), stop_reason=StopReason.BUDGET_REACHED)
    after = await service.get_session(sid)
    assert after.status == SessionStatus.IDLE and after.stop_reason == StopReason.BUDGET_REACHED


async def test_connector_permission_lookup(service, session):
    sid = session.session_id
    await service.permit(sid, lease_token=lease(session), tool_use_id="t1", tool_name="Read", args={"path": "x"})
    from milos.models import sha256_json

    found = await service.permission(sid, tool_name="Read", args_sha256=sha256_json({"path": "x"}))
    assert found and found.tool_use_id == "t1"
    assert await service.permission(sid, tool_name="Read", args_sha256=sha256_json({"path": "y"})) is None


async def test_publish_increments_version(service):
    v1 = await service.publish(definition())
    v2 = await service.publish(definition(purpose="v2"))
    assert (v1.version, v2.version) == (1, 2)
    agent, latest = await service.get_agent("analyst")
    assert agent.latest_version == 2 and latest.purpose == "v2"


async def test_local_launcher_prints_the_runner_environment(capsys):
    from milos.jobs import NoJobs

    assert (
        await NoJobs().launch("sess_1", agent_id="analyst", env={"MILOS_LEASE_TOKEN": "lt", "MILOS_SESSION_TOKEN": "st"})
        == "local-sess_1"
    )
    err = capsys.readouterr().err
    assert "MILOS_SESSION_TOKEN=st" in err and "MILOS_LEASE_TOKEN=lt" in err


async def test_list_sessions_by_approver(service, agent):
    mine = await service.create_session(
        "analyst", "hi", operator="alice@example.com", client_request_id="r1", approvers=["lead@example.com"]
    )
    await service.create_session("analyst", "hi", operator="bob@example.com", client_request_id="r2")
    assert [s.session_id for s in await service.list_sessions(approver="lead@example.com")] == [mine.session_id]
    assert await service.list_sessions(approver="alice@example.com") == []
