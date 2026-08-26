import pytest

import milos.runner
from milos.policy import canonical_hash, validate_policy
from milos.runner import run
from milos.types import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from .fakes import FakeStore

SID = "sess_run"


def feed(store, sid=SID):
    """The session's journal records in seq order."""
    return sorted(store.events.get(sid, {}).values(), key=lambda e: e["seq"])


def messages(store, sid=SID):
    """Only the records that render as messages (message + prompt), as docs."""
    from milos.journal import event_message

    return [doc for e in feed(store, sid) if (doc := event_message(e)) is not None]


class FakeClient:
    def __init__(self, options):
        self.options = options
        self.prompts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        self.prompts.append(prompt)

    async def receive_response(self):
        yield SystemMessage(subtype="init", data={})
        yield SystemMessage(subtype="thinking_tokens", data={})
        yield AssistantMessage(content=[TextBlock(text="did it")], model="m")
        yield ResultMessage(
            subtype="success",
            duration_ms=5,
            duration_api_ms=4,
            is_error=False,
            num_turns=1,
            session_id="claude-uuid-1",
            total_cost_usd=0.25,
        )

    async def interrupt(self):
        pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("MILOS_PROJECT", "proj-1")
    monkeypatch.setenv("MILOS_WORK_DIR", str(tmp_path / "work"))
    monkeypatch.setenv("MILOS_STAY_ALIVE", "0")
    monkeypatch.setenv("MILOS_APPROVAL_TIMEOUT", "1")


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(milos.runner, "Store", lambda project: fake)
    return fake


@pytest.fixture
def fake_harness(monkeypatch):
    clients = []

    def make(options):
        client = FakeClient(options)
        clients.append(client)
        return client

    monkeypatch.setattr(milos.runner, "ClaudeSDKClient", make)
    monkeypatch.setattr(milos.runner.state, "restore", lambda *a: 0)
    monkeypatch.setattr(milos.runner.state, "checkpoint", lambda *a: 0)
    return clients


async def make_session(store, options=None, *, policy=None, sid=SID):
    """Create a session stamped with a freshly-applied policy, the way
    attach_session would."""
    doc = validate_policy(policy or {})
    version = await store.create_policy(doc, {"hash": canonical_hash(doc)})
    await store.create_session(
        sid, options or {}, policy_version=version, policy_hash=canonical_hash(doc)
    )
    return version


async def test_runner_full_turn(env, store, fake_harness):
    await make_session(store, {"system_prompt": "sp", "model": "m"})
    await store.push_inbox(SID, "message", "do the thing")

    await run(SID)

    (client,) = fake_harness
    assert client.prompts == ["do the thing"]
    assert client.options.system_prompt == "sp"
    assert client.options.env["CLAUDE_CODE_USE_VERTEX"] == "1"
    assert client.options.env["ANTHROPIC_VERTEX_PROJECT_ID"] == "proj-1"
    assert "HOME" in client.options.env
    assert client.options.hooks and "PreToolUse" in client.options.hooks
    assert client.options.setting_sources == ["user"]

    # thinking_tokens progress events are dropped; everything else is journaled
    events = feed(store)
    assert [e["type"] for e in events] == [
        "lifecycle",  # claimed
        "prompt",
        "message",  # init
        "message",  # assistant
        "message",  # result
        "lifecycle",  # released
    ]
    assert [m["kind"] for m in messages(store)] == ["user", "system", "assistant", "result"]
    assert messages(store)[0]["content"] == "do the thing"
    assert [e["seq"] for e in events] == [1, 2, 3, 4, 5, 6]
    # the tree: each record chains to the previous one on the branch
    assert [e["parent_uuid"] for e in events] == [None] + [e["uuid"] for e in events[:-1]]
    assert all(e["branch"] == "main" for e in events)
    # context snapshots carry the policy stamp — every record is evidence of
    # which rules were in force
    assert events[1]["context"]["model"] == "m"
    assert events[1]["context"]["policy_version"] == "v000001"
    assert events[1]["context"]["policy_hash"]

    session = await store.get_session(SID)
    assert session["runtime"]["status"] == "idle"
    assert session["runtime"]["stop_reason"] == "success"
    assert session["seq_head"] == 6
    assert session["cost_usd"] == 0.25
    assert session["claude_session_id"] == "claude-uuid-1"
    assert session["runtime"]["lease_expires"] == 0.0


async def test_runner_runs_queued_prompts_as_separate_turns(env, store, fake_harness):
    await make_session(store)
    await store.push_inbox(SID, "message", "first")
    await store.push_inbox(SID, "message", "second")

    await run(SID)

    (client,) = fake_harness
    # never glued into one mega-prompt: each prompt gets its own query...
    assert client.prompts == ["first", "second"]
    # ...and the feed interleaves each prompt with its own turn
    docs = messages(store)
    assert [m["kind"] for m in docs] == ["user", "system", "assistant", "result"] * 2
    session = await store.get_session(SID)
    assert session["cost_usd"] == 0.5


async def test_runner_exits_when_lease_held(env, store, fake_harness):
    await make_session(store)
    await store.claim_session(SID, "other-lease", 3600)

    await run(SID)

    assert fake_harness == []  # never started the harness


async def test_runner_exits_when_disabled(env, store, fake_harness):
    await make_session(store)
    await store.update_session(SID, disabled=True)

    await run(SID)

    assert fake_harness == []


async def test_runner_fails_fast_without_policy_stamp(env, store, fake_harness):
    # A session doc with no policy_version (created out-of-band) must not run.
    await store.create_session(SID, {})
    await store.push_inbox(SID, "message", "go")

    await run(SID)

    assert fake_harness == []
    session = await store.get_session(SID)
    assert session["runtime"]["status"] == "idle"
    assert session["runtime"]["stop_reason"] == "policy_error"
    (doc,) = messages(store)
    assert doc["kind"] == "result" and doc["is_error"] is True


async def test_runner_fails_fast_when_pinned_policy_missing(env, store, fake_harness):
    await store.create_session(SID, {}, policy_version="v000009", policy_hash="h")
    await store.push_inbox(SID, "message", "go")

    await run(SID)

    assert fake_harness == []
    assert (await store.get_session(SID))["runtime"]["stop_reason"] == "policy_error"


async def test_runner_pins_gate_to_the_admitted_policy(env, store, fake_harness):
    """The gate enforces the session's stamped version even after a newer
    policy is applied — what evidence says was in force is what ran."""
    await make_session(store, policy={"tools": {"deny": ["Bash"]}})
    # a newer, permissive policy becomes active after admission
    permissive = validate_policy({})
    await store.create_policy(permissive, {"hash": canonical_hash(permissive)})
    await store.push_inbox(SID, "message", "go")

    await run(SID)

    (client,) = fake_harness
    gate_hook = client.options.hooks["PreToolUse"][0].hooks[0]
    result = await gate_hook({"tool_name": "Bash", "tool_input": {}}, "t1", None)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


async def test_runner_recovers_head_from_journal_after_crash(env, store, fake_harness):
    from .fakes import append_message

    await make_session(store)
    # crash scenario: three records journaled but seq_head flushed only to 1
    for seq in (1, 2, 3):
        await append_message(store, SID, seq, {"kind": "assistant", "content": []})
    await store.update_session(SID, seq_head=1)
    await store.push_inbox(SID, "message", "go")

    await run(SID)

    events = feed(store)
    # nothing overwritten: the 3 pre-crash records survive and the new run
    # continued at seq 4
    assert len(events) == 3 + 6
    assert [e["seq"] for e in events] == list(range(1, 10))
    assert events[3]["type"] == "lifecycle" and events[3]["payload"]["event"] == "claimed"
    assert events[3]["parent_uuid"] == events[2]["uuid"]


async def test_runner_forks_sdk_session_on_fresh_branch(env, store, fake_harness):
    from .fakes import append_message

    await make_session(store)
    base = await append_message(store, SID, 1, {"kind": "result"})
    await store.update_session(SID, claude_session_id="c-old", seq_head=1)
    await store.create_branch(
        SID, "br_a", base_uuid=base["uuid"], base_seq=1, claude_session_id="c-turn1"
    )
    await store.push_inbox(SID, "message", "try again")

    await run(SID)

    (client,) = fake_harness
    # the SDK resumed the branch's base session and forked it there
    assert client.options.resume == "c-turn1"
    assert client.options.fork_session is True
    branch_events = await store.list_events(SID, "br_a", after=0)
    assert [e["seq"] for e in branch_events] == [2, 3, 4, 5, 6, 7]
    session = await store.get_session(SID)
    assert session["branches"]["br_a"]["claude_session_id"] == "claude-uuid-1"


async def test_runner_forks_even_when_branch_shares_current_sdk_session(env, store, fake_harness):
    # Rewinding into the latest run: the branch's base SDK session IS the
    # session's current one. Fork detection must key off journal state (a
    # never-run branch), not id inequality.
    from .fakes import append_message

    await make_session(store)
    base = await append_message(store, SID, 1, {"kind": "result"})
    await store.update_session(SID, claude_session_id="c-current", seq_head=1)
    await store.create_branch(
        SID, "br_a", base_uuid=base["uuid"], base_seq=1, claude_session_id="c-current"
    )
    await store.push_inbox(SID, "message", "try again")

    await run(SID)

    (client,) = fake_harness
    assert client.options.resume == "c-current"
    assert client.options.fork_session is True


async def test_runner_stops_writing_when_lease_lost(env, store, monkeypatch):
    import asyncio

    class SlowClient(FakeClient):
        async def receive_response(self):
            async for message in super().receive_response():
                await asyncio.sleep(0.03)
                yield message

    monkeypatch.setenv("MILOS_LEASE_TTL", "0.05")  # heartbeat every ~0.017s
    monkeypatch.setattr(milos.runner, "ClaudeSDKClient", SlowClient)
    monkeypatch.setattr(milos.runner.state, "restore", lambda *a: 0)
    monkeypatch.setattr(milos.runner.state, "checkpoint", lambda *a: 0)
    await make_session(store)
    await store.push_inbox(SID, "message", "go")

    # the lease is stolen as soon as the runner starts heartbeating
    original = store.renew_lease

    async def stolen(session_id, lease_id, ttl):
        store.sessions[SID]["runtime"]["lease_id"] = "thief"
        return await original(session_id, lease_id, ttl)

    monkeypatch.setattr(store, "renew_lease", stolen)

    await run(SID)

    session = await store.get_session(SID)
    # the loser wrote no release: the thief's claim state stands untouched
    assert session["runtime"]["status"] == "running"
    assert session["runtime"]["stop_reason"] is None
    assert session["runtime"]["lease_id"] == "thief"


async def test_prompt_is_journaled_before_the_inbox_item_is_consumed(
    env, store, fake_harness, monkeypatch
):
    """The record goes in first, so a run that dies between the two replays the
    prompt on the next execution instead of swallowing it."""
    order = []
    append, consume = store.append_event, store.consume_message

    async def track_append(session_id, event):
        if event["type"] == "prompt":
            order.append(("journal", event["payload"]["inbox_id"]))
        await append(session_id, event)

    async def track_consume(session_id, message_id):
        order.append(("consume", message_id))
        await consume(session_id, message_id)

    monkeypatch.setattr(store, "append_event", track_append)
    monkeypatch.setattr(store, "consume_message", track_consume)

    await make_session(store)
    message_id = await store.push_inbox(SID, "message", "do the thing")

    await run(SID)

    assert order == [("journal", message_id), ("consume", message_id)]


async def test_a_run_that_dies_releases_and_leaves_the_prompt_queued(
    env, store, fake_harness, no_job_trigger, monkeypatch
):
    """A crash used to end the process with the session still "running" and the
    lease left to expire: the session read as stalled with nothing in the
    transcript, and no replacement could be triggered while that stale lease
    looked alive."""

    def boom(*args):
        raise RuntimeError("bucket rewritten mid-restore")

    monkeypatch.setattr(milos.runner.state, "restore", boom)
    await make_session(store)
    await store.push_inbox(SID, "message", "do the thing")

    with pytest.raises(RuntimeError):
        await run(SID)

    session = await store.get_session(SID)
    assert session["runtime"]["status"] == "idle"
    assert session["runtime"]["stop_reason"] == "error"
    assert session["runtime"]["lease_expires"] == 0.0

    events = feed(store)
    assert [e["type"] for e in events] == ["lifecycle", "lifecycle", "message"]
    assert events[1]["payload"]["event"] == "error"
    (doc,) = messages(store)
    assert doc["kind"] == "result" and doc["subtype"] == "error" and doc["is_error"] is True

    # the prompt waits for whoever runs next, and the failed run does not
    # re-trigger itself — a deterministic failure would loop forever
    assert [m["text"] for m in await store.peek_messages(SID)] == ["do the thing"]
    assert no_job_trigger.session_ids == []


async def test_release_hands_off_a_prompt_that_arrived_during_shutdown(
    env, store, fake_harness, no_job_trigger, monkeypatch
):
    """The message loop closes before the shutdown tail, so a prompt arriving
    in that window sees a live lease and its sender triggers nothing. The
    releasing run is the only one left who can."""

    def checkpoint_and_race(*args):
        # a second client sends one while this run is checkpointing (the
        # checkpoint runs once per prefix; one racing prompt is enough)
        queue = store.inbox.setdefault(SID, [])
        if not any(item["id"] == "late" for item in queue):
            queue.append(
                {"id": "late", "kind": "message", "text": "late", "ts": 1.0, "consumed": False}
            )
        return 0

    monkeypatch.setattr(milos.runner.state, "checkpoint", checkpoint_and_race)
    await make_session(store)
    await store.push_inbox(SID, "message", "do the thing")

    await run(SID)

    assert no_job_trigger.session_ids == [SID]
    session = await store.get_session(SID)
    assert session["runtime"]["status"] == "starting"
    assert [m["text"] for m in await store.peek_messages(SID)] == ["late"]


async def test_release_with_an_empty_inbox_triggers_nothing(
    env, store, fake_harness, no_job_trigger
):
    await make_session(store)
    await store.push_inbox(SID, "message", "do the thing")

    await run(SID)

    assert no_job_trigger.session_ids == []
