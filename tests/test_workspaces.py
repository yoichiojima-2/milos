import pytest

from milos import workspaces
from milos.options import AgentOptions
from milos.workspaces import WorkspaceError

from .fakes import FakeGcsBucket, FakeStore

OPTS = AgentOptions(project="p")


async def make(store, name="shared", run_options=None, **kwargs):
    return await workspaces.create(
        name,
        run_options or AgentOptions(model="claude-sonnet-5"),
        options=OPTS,
        store=store,
        **kwargs,
    )


async def test_create_stores_serialized_options():
    store = FakeStore()
    workspace = await make(store, description="the shared docs")
    assert workspace["name"] == "shared"
    assert workspace["description"] == "the shared docs"
    stored = store.workspaces["shared"]["options"]
    assert stored["model"] == "claude-sonnet-5"
    assert "project" not in stored  # coordinates stay out


async def test_create_duplicate_and_bad_name_rejected():
    store = FakeStore()
    await make(store)
    with pytest.raises(WorkspaceError):
        await make(store)
    with pytest.raises(Exception):
        await make(store, name="Bad Name")


async def test_update_upserts_a_doc_less_workspace():
    store = FakeStore()
    # no create: the workspace exists only as a GCS directory
    await workspaces.update("bare", AgentOptions(model="m2"), options=OPTS, store=store)
    assert store.workspaces["bare"]["options"]["model"] == "m2"


async def test_delete_requires_existing():
    store = FakeStore()
    await make(store)
    await workspaces.delete("shared", store=store)
    assert await workspaces.get("shared", store=store) is None
    with pytest.raises(WorkspaceError):
        await workspaces.delete("ghost", store=store)


def test_members_derived_from_agent_docs():
    agent_docs = [
        {"name": "writer", "options": {"workspace": "shared"}},
        {"name": "critic", "options": {"workspace": "shared"}},
        {"name": "loner", "options": {}},
        {"name": "other", "options": {"workspace": "elsewhere"}},
    ]
    assert workspaces.members("shared", agent_docs) == ["critic", "writer"]
    assert workspaces.members("empty", agent_docs) == []


def test_claude_md_round_trip(monkeypatch):
    from milos import state

    bucket = FakeGcsBucket()
    monkeypatch.setattr(state, "_bucket", lambda project, bucket_name: bucket)
    assert workspaces.read_claude_md("p", "b", "shared") is None
    workspaces.write_claude_md("p", "b", "shared", "# house rules\n")
    assert bucket.objects["workspaces/shared/ws/CLAUDE.md"]["data"] == b"# house rules\n"
    assert workspaces.read_claude_md("p", "b", "shared") == "# house rules\n"


async def test_resolve_layers_workspace_between_agent_and_settings():
    from milos import agents

    store = FakeStore()
    await make(store, run_options=AgentOptions(system_prompt="ws prompt", model="ws-model"))
    await store.update_settings({"options": {"system_prompt": "house", "max_turns": 9}})
    await agents.create(
        "writer", AgentOptions(model="agent-model", workspace="shared"), options=OPTS, store=store
    )

    # agent layer wins over workspace, workspace over settings, floor last
    resolved = await agents.resolve(store, AgentOptions(agent="writer", project="p"))
    assert resolved.model == "agent-model"  # agent beats workspace
    assert resolved.system_prompt == "ws prompt"  # workspace beats settings
    assert resolved.max_turns == 9  # settings still contribute
    assert resolved.workspace == "shared"  # inherited from the agent

    # a named workspace with no stored doc contributes nothing but the name
    bare = await agents.resolve(store, AgentOptions(workspace="bare", project="p"))
    assert bare.workspace == "bare"
    assert bare.system_prompt == "house"


async def test_attach_session_snapshots_workspace_resolved_options(no_job_trigger):
    from milos.policy import canonical_hash, validate_policy
    from milos.remote import attach_session

    store = FakeStore()
    policy = validate_policy({})
    await store.create_policy(policy, {"hash": canonical_hash(policy)})
    await make(store, run_options=AgentOptions(system_prompt="ws prompt"))

    session_id, _branch, _cursor = await attach_session(
        store, AgentOptions(workspace="shared", project="p")
    )
    options = store.sessions[session_id]["options"]
    assert options["workspace"] == "shared"
    assert options["system_prompt"] == "ws prompt"
