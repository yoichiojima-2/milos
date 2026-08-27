import time

import pytest

from milos.store import (
    START_GRACE_SECONDS,
    StoreProtocol,
    format_policy_version,
    lease_active,
    new_incident_id,
    new_session_id,
    start_pending,
)

from .fakes import FakeStore, append_message


def test_ids():
    a, b = new_session_id(), new_session_id()
    assert a.startswith("sess_") and b.startswith("sess_") and a != b
    assert new_incident_id().startswith("inc_")
    assert format_policy_version(3) == "v000003"


def test_lease_active():
    assert lease_active(None) is False
    assert lease_active({}) is False
    assert lease_active({"lease_expires": time.time() - 1}) is False
    assert lease_active({"lease_expires": time.time() + 60}) is True
    assert lease_active({"lease_expires": 100.0}, now=99.0) is True
    assert lease_active({"lease_expires": 100.0}, now=100.0) is False


def test_start_pending():
    assert start_pending(None) is False
    assert start_pending({"status": "running", "triggered_at": time.time()}) is False
    assert start_pending({"status": "starting", "triggered_at": time.time()}) is True
    stale = time.time() - START_GRACE_SECONDS - 1
    assert start_pending({"status": "starting", "triggered_at": stale}) is False


async def test_mark_starting_skips_live_and_dead_sessions():
    store = FakeStore()
    await store.create_session("sess_1", {})
    await store.mark_starting("sess_1")
    assert (await store.get_session("sess_1"))["runtime"]["status"] == "starting"

    await store.claim_session("sess_1", "lease-1", 60)
    await store.mark_starting("sess_1")
    assert (await store.get_session("sess_1"))["runtime"]["status"] == "running"

    await store.update_session("sess_1", status="terminated")
    await store.mark_starting("sess_1")
    assert (await store.get_session("sess_1"))["runtime"]["status"] == "terminated"

    await store.mark_starting("sess_missing")  # no such session: a no-op, not an error


async def test_session_carries_policy_stamp():
    store = FakeStore()
    await store.create_session("sess_1", {}, policy_version="v000002", policy_hash="abc")
    session = await store.get_session("sess_1")
    assert session["policy_version"] == "v000002"
    assert session["policy_hash"] == "abc"


async def test_inbox_peek_leaves_messages_for_the_caller_to_consume():
    store = FakeStore()
    await store.create_session("sess_1", {})
    first = await store.push_inbox("sess_1", "message", "one")
    second = await store.push_inbox("sess_1", "message", "two")
    await store.push_inbox("sess_1", "interrupt")

    queued = await store.peek_messages("sess_1")
    assert [(m["id"], m["text"]) for m in queued] == [(first, "one"), (second, "two")]
    assert await store.peek_messages("sess_1") == queued  # peeking is read-only

    await store.consume_message("sess_1", first)
    assert [m["text"] for m in await store.peek_messages("sess_1")] == ["two"]
    assert await store.take_interrupt("sess_1") is True


async def test_claim_is_reentrant_for_same_lease_only():
    store = FakeStore()
    await store.create_session("sess_1", {})
    assert await store.claim_session("sess_1", "lease-1", 60) is not None
    assert await store.claim_session("sess_1", "lease-1", 60) is not None
    assert await store.claim_session("sess_1", "lease-2", 60) is None


async def test_renew_lease_heartbeat():
    store = FakeStore()
    await store.create_session("sess_1", {})
    await store.claim_session("sess_1", "lease-1", 60)
    assert await store.renew_lease("sess_1", "lease-1", 60) is True
    assert await store.renew_lease("sess_1", "lease-2", 60) is False
    await store.update_session("sess_1", disabled=True)
    assert await store.renew_lease("sess_1", "lease-1", 60) is False


async def test_append_event_never_overwrites_on_stale_seq():
    store = FakeStore()
    await store.create_session("sess_1", {})
    first = await append_message(store, "sess_1", 1, {"kind": "user", "content": "a"})
    second = await append_message(store, "sess_1", 1, {"kind": "user", "content": "b"})
    events = await store.list_events("sess_1", "main", after=0)
    assert {e["uuid"] for e in events} == {first["uuid"], second["uuid"]}


async def test_recover_head_reads_past_stale_seq_head():
    store = FakeStore()
    await store.create_session("sess_1", {})
    for seq in (1, 2, 3):
        await append_message(store, "sess_1", seq, {"kind": "assistant", "content": []})
    await store.update_session("sess_1", seq_head=1)
    seq, tip = await store.recover_head("sess_1", "main")
    assert seq == 3
    assert await store.recover_head("sess_1", "br_x") == (0, None)


async def test_create_branch_switches_active_and_guards():
    store = FakeStore()
    await store.create_session("sess_1", {})
    base = await append_message(store, "sess_1", 1, {"kind": "result"})
    await store.create_branch(
        "sess_1", "br_a", base_uuid=base["uuid"], base_seq=1, claude_session_id="c1"
    )
    session = await store.get_session("sess_1")
    assert session["active_branch"] == "br_a"
    with pytest.raises(ValueError):
        await store.create_branch(
            "sess_1", "br_a", base_uuid=None, base_seq=0, claude_session_id=None
        )
    await store.claim_session("sess_1", "lease-1", 60)
    with pytest.raises(RuntimeError):
        await store.create_branch(
            "sess_1", "br_b", base_uuid=None, base_seq=0, claude_session_id=None
        )


async def test_policy_versions_allocate_and_activate():
    store = FakeStore()
    assert await store.get_active_policy() is None
    v1 = await store.create_policy({"tools": {"deny": []}}, {"hash": "h1", "applied_by": "me"})
    v2 = await store.create_policy({"tools": {"deny": ["Bash"]}}, {"hash": "h2"})
    assert (v1, v2) == ("v000001", "v000002")
    active = await store.get_active_policy()
    assert active["version"] == "v000002"
    assert active["policy"]["tools"]["deny"] == ["Bash"]
    # older versions stay readable — the record of what was once enforced
    assert (await store.get_policy("v000001"))["hash"] == "h1"
    assert [p["version"] for p in await store.list_policies()] == ["v000001", "v000002"]


def test_no_policy_update_method_exists():
    # Policy versions are immutable; the absence of the method is the control.
    assert not hasattr(FakeStore(), "update_policy")
    assert not hasattr(FakeStore(), "delete_policy")

    from milos.store import Store

    assert not hasattr(Store, "update_policy")
    assert not hasattr(Store, "delete_policy")


async def test_agent_revisions_number_from_one():
    store = FakeStore()
    await store.create_agent("a1", {"options": {}})
    assert await store.create_agent_revision("a1", {"options": {}}) == 1
    assert await store.create_agent_revision("a1", {"options": {"model": "x"}}) == 2
    revisions = await store.list_agent_revisions("a1")
    assert [r["revision"] for r in revisions] == [1, 2]
    await store.delete_agent("a1")
    assert await store.list_agent_revisions("a1") == []


async def test_incidents_crud():
    store = FakeStore()
    await store.create_incident("inc_1", {"session_id": "sess_1", "status": "open"})
    assert (await store.get_incident("inc_1"))["status"] == "open"
    await store.update_incident("inc_1", status="closed", resolution="fixed")
    assert (await store.get_incident("inc_1"))["resolution"] == "fixed"
    assert [i["id"] for i in await store.list_incidents()] == ["inc_1"]


def test_fake_store_satisfies_protocol():
    # The contract test: FakeStore must expose every StoreProtocol method,
    # so the suite can't quietly test against a drifted fake.
    assert isinstance(FakeStore(), StoreProtocol)


async def test_workspace_lease_claim_and_contend():
    store = FakeStore()
    assert await store.claim_workspace("ws", "sess_a", 60) is True
    assert await store.claim_workspace("ws", "sess_a", 60) is True  # re-entrant for the holder
    assert await store.claim_workspace("ws", "sess_b", 60) is False


async def test_workspace_lease_expiry_reclaimable():
    store = FakeStore()
    await store.claim_workspace("ws", "sess_a", -1)  # already expired
    assert await store.claim_workspace("ws", "sess_b", 60) is True


async def test_workspace_release_only_by_holder():
    store = FakeStore()
    await store.claim_workspace("ws", "sess_a", 60)
    await store.release_workspace("ws", "sess_b")  # no-op
    assert store.workspaces["ws"]["lease_session_id"] == "sess_a"
    await store.release_workspace("ws", "sess_a")
    assert store.workspaces["ws"]["lease_session_id"] is None
    assert store.workspaces["ws"]["lease_expires"] == 0.0
