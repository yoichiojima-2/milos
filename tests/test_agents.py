import pytest

from milos import agents
from milos.agents import AgentError
from milos.errors import PolicyError
from milos.options import AgentOptions
from milos.policy import validate_policy

from .fakes import FakeStore

OPTS = AgentOptions(project="p")
RISK = {"purpose": "review code", "impact": "low", "owner": "me@x", "review_by": "2027-01-01"}


async def make(store, name="reviewer", run_options=None, **kwargs):
    return await agents.create(
        name,
        run_options or AgentOptions(system_prompt="be careful", model="claude-sonnet-5"),
        options=OPTS,
        store=store,
        **kwargs,
    )


async def require_risk_policy(store):
    await store.create_policy(
        validate_policy({"oversight": {"require_risk_block": True}}), {"hash": "h"}
    )


# --- CRUD ---


async def test_create_stores_serialized_options():
    store = FakeStore()
    agent = await make(store, description="code reviews")
    assert agent["name"] == "reviewer"
    assert agent["description"] == "code reviews"
    stored = store.agents["reviewer"]["options"]
    assert stored["system_prompt"] == "be careful"
    assert stored["model"] == "claude-sonnet-5"
    # only the serialized subset travels; coordinates stay out
    assert "project" not in stored


async def test_create_duplicate_rejected():
    store = FakeStore()
    await make(store)
    with pytest.raises(AgentError):
        await make(store)


async def test_get_and_list():
    store = FakeStore()
    await make(store, name="b")
    await make(store, name="a")
    assert (await agents.get("a", store=store))["name"] == "a"
    assert await agents.get("missing", store=store) is None
    assert [a["name"] for a in await agents.list_all(store=store)] == ["a", "b"]


async def test_update_replaces_options_and_archives_revision():
    store = FakeStore()
    await make(store)
    await agents.update(
        "reviewer", AgentOptions(model="claude-haiku-4-5"), options=OPTS, store=store
    )
    stored = store.agents["reviewer"]["options"]
    assert stored["model"] == "claude-haiku-4-5"
    assert stored["system_prompt"] is None  # replaced wholesale, not merged
    # the pre-update doc is the version history (ISO 42001 versioning)
    (revision,) = await agents.revisions("reviewer", store=store)
    assert revision["revision"] == 1
    assert revision["options"]["model"] == "claude-sonnet-5"


async def test_update_and_delete_missing_raise():
    store = FakeStore()
    with pytest.raises(AgentError):
        await agents.update("ghost", AgentOptions(), options=OPTS, store=store)
    with pytest.raises(AgentError):
        await agents.delete("ghost", store=store)


async def test_delete():
    store = FakeStore()
    await make(store)
    await agents.delete("reviewer", store=store)
    assert await agents.get("reviewer", store=store) is None


# --- the AI risk register (ISO 42001) ---


async def test_risk_block_optional_without_policy():
    store = FakeStore()
    agent = await make(store)
    assert agent["risk"] is None


async def test_policy_requires_risk_block():
    store = FakeStore()
    await require_risk_policy(store)
    with pytest.raises(AgentError, match="risk block"):
        await make(store)
    agent = await make(store, name="risky", risk=RISK)
    assert agent["risk"]["impact"] == "low"


async def test_invalid_risk_block_rejected():
    store = FakeStore()
    with pytest.raises(PolicyError, match="impact"):
        await make(store, risk={**RISK, "impact": "huge"})


async def test_update_keeps_existing_risk_block():
    store = FakeStore()
    await require_risk_policy(store)
    await make(store, risk=RISK)
    # an update that doesn't mention risk keeps the register entry
    await agents.update("reviewer", AgentOptions(model="m2"), options=OPTS, store=store)
    assert store.agents["reviewer"]["risk"] == RISK


# --- merge / resolve ---


def test_merge_stored_defaults_win_when_not_overridden():
    base = AgentOptions(system_prompt="stored", model="m1", allowed_tools=["Read"])
    merged = agents.merge(base, AgentOptions(project="p"))
    assert merged.system_prompt == "stored"
    assert merged.model == "m1"
    assert merged.allowed_tools == ["Read"]
    assert merged.project == "p"  # coordinates come from the overrides side


def test_merge_explicit_fields_override():
    base = AgentOptions(system_prompt="stored", model="m1", max_turns=3)
    merged = agents.merge(base, AgentOptions(model="m2", allowed_tools=["Bash"]))
    assert merged.model == "m2"
    assert merged.allowed_tools == ["Bash"]
    assert merged.system_prompt == "stored"  # untouched default inherits
    assert merged.max_turns == 3


async def test_resolve_missing_agent_raises():
    store = FakeStore()
    with pytest.raises(AgentError):
        await agents.resolve(store, AgentOptions(agent="ghost", project="p"))


async def test_resolve_without_agent_floors_model_and_prompt():
    store = FakeStore()
    resolved = await agents.resolve(store, AgentOptions(project="p"))
    assert resolved.model == "sonnet"
    assert resolved.system_prompt == {"type": "preset", "preset": "claude_code"}
    assert resolved.project == "p"


async def test_resolve_layers_agent_then_settings():
    store = FakeStore()
    await make(store)  # agent "reviewer", system_prompt="be careful"
    from_agent = await agents.resolve(store, AgentOptions(agent="reviewer", project="p"))
    assert from_agent.system_prompt == "be careful"

    explicit = await agents.resolve(store, AgentOptions(system_prompt="be terse", project="p"))
    assert explicit.system_prompt == "be terse"

    await store.update_settings({"options": {"system_prompt": "house style"}})
    from_settings = await agents.resolve(store, AgentOptions(project="p"))
    assert from_settings.system_prompt == "house style"


async def test_resolve_keeps_an_explicit_empty_prompt():
    """An empty string is the escape hatch: no system prompt at all."""
    store = FakeStore()
    resolved = await agents.resolve(store, AgentOptions(system_prompt="", project="p"))
    assert resolved.system_prompt == ""


async def test_resolve_merges_stored_options():
    store = FakeStore()
    await make(store)
    merged = await agents.resolve(
        store, AgentOptions(agent="reviewer", model="override", project="p")
    )
    assert merged.system_prompt == "be careful"
    assert merged.model == "override"
    assert merged.agent == "reviewer"
