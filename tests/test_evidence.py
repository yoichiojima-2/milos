import json
import time

import pytest

from milos import evidence, incidents
from milos.errors import MilosError
from milos.policy import canonical_hash, validate_policy

from .fakes import FakeBucket, FakeStore, append_message

NOW = time.time()
RANGE = {"start": NOW - 3600, "end": NOW + 3600}


async def seed(store):
    policy = validate_policy({"tools": {"deny": ["WebSearch"]}})
    version = await store.create_policy(policy, {"hash": canonical_hash(policy)})
    await store.create_session("sess_1", {"model": "m"}, policy_version=version)
    await append_message(store, "sess_1", 1, {"kind": "user", "content": "hi"})
    await store.request_approval("sess_1", "h1", "Bash", {"command": "x"})
    await store.decide_approval("sess_1", "h1", allow=True, decided_by="cli")
    await store.create_agent("a1", {"options": {}, "risk": None})
    await incidents.open_incident("sess_1", "odd output", store=store)


async def test_export_writes_manifest_and_files():
    store, bucket = FakeStore(), FakeBucket()
    await seed(store)
    manifest = await evidence.export(store, bucket, **RANGE, generated_by="me@x")

    prefix = f"exports/{manifest['export_id']}/"
    names = bucket.list(prefix)
    assert prefix + "manifest.json" in names
    for name in evidence.FILES:
        assert prefix + name in names
        assert manifest["files"][name]["sha256"]
    assert manifest["generated_by"] == "me@x"

    sessions = bucket.download(prefix + "sessions.jsonl").decode().splitlines()
    (session,) = [json.loads(line) for line in sessions]
    assert session["id"] == "sess_1"
    assert session["policy_version"] == "v000001"
    events = [
        json.loads(line) for line in bucket.download(prefix + "events.jsonl").decode().splitlines()
    ]
    assert {e["type"] for e in events} == {"message", "lifecycle"}  # incident flag rides along
    (approval,) = [
        json.loads(line)
        for line in bucket.download(prefix + "approvals.jsonl").decode().splitlines()
    ]
    assert approval["decided_by"] == "cli"
    (policy_row,) = [
        json.loads(line)
        for line in bucket.download(prefix + "policies.jsonl").decode().splitlines()
    ]
    assert policy_row["version"] == "v000001"


async def test_export_filters_sessions_by_range():
    store, bucket = FakeStore(), FakeBucket()
    await store.create_session("sess_old", {})
    store.sessions["sess_old"]["created_at"] = NOW - 10_000
    await store.create_session("sess_new", {})
    manifest = await evidence.export(store, bucket, **RANGE)
    assert manifest["files"]["sessions.jsonl"]["records"] == 1


async def test_export_single_session():
    store, bucket = FakeStore(), FakeBucket()
    await seed(store)
    await store.create_session("sess_2", {})
    manifest = await evidence.export(store, bucket, **RANGE, session_id="sess_1")
    assert manifest["files"]["sessions.jsonl"]["records"] == 1
    with pytest.raises(MilosError, match="not found"):
        await evidence.export(store, bucket, **RANGE, session_id="sess_ghost")


async def test_verify_round_trip_and_tamper_detection():
    store, bucket = FakeStore(), FakeBucket()
    await seed(store)
    manifest = await evidence.export(store, bucket, **RANGE)
    export_id = manifest["export_id"]

    verified = evidence.verify(bucket, export_id)
    assert verified["bundle_hash"] == manifest["bundle_hash"]

    # any edit to any file fails verification
    name = f"exports/{export_id}/approvals.jsonl"
    bucket.blobs[name] = bucket.blobs[name].replace(b'"allow"', b'"deny"')
    with pytest.raises(MilosError, match="failed verification"):
        evidence.verify(bucket, export_id)


async def test_incident_open_close():
    store = FakeStore()
    await store.create_session("sess_1", {})
    incident = await incidents.open_incident(
        "sess_1", "prompt injection suspected", severity="high", opened_by="me", store=store
    )
    assert incident["status"] == "open"
    # the session transcript shows the flag inline
    (event,) = await store.list_events("sess_1", "main", after=0)
    assert event["payload"]["event"] == "incident_opened"
    assert event["payload"]["incident"] == incident["id"]

    closed = await incidents.close_incident(
        incident["id"], "false alarm", closed_by="me", store=store
    )
    assert closed["status"] == "closed"
    assert (await store.get_incident(incident["id"]))["resolution"] == "false alarm"


async def test_incident_guards():
    store = FakeStore()
    with pytest.raises(MilosError, match="not found"):
        await incidents.open_incident("sess_ghost", "x", store=store)
    await store.create_session("sess_1", {})
    with pytest.raises(MilosError, match="reason"):
        await incidents.open_incident("sess_1", "  ", store=store)
    with pytest.raises(MilosError, match="severity"):
        await incidents.open_incident("sess_1", "x", severity="huge", store=store)
    with pytest.raises(MilosError, match="not found"):
        await incidents.close_incident("inc_ghost", "done", store=store)
