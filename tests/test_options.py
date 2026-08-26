import pytest

from milos.errors import OptionsError
from milos.options import (
    AgentOptions,
    _SERIALIZED_FIELDS,
    append_system_prompt,
    default_prompt,
    model_env,
    options_from_doc,
)


def test_serialize_round_trip():
    options = AgentOptions(
        system_prompt="be careful",
        model="claude-sonnet-5",
        allowed_tools=["Read"],
        max_turns=5,
    )
    doc = options.serialize()
    assert set(doc) == set(_SERIALIZED_FIELDS)
    rebuilt = options_from_doc(doc)
    assert rebuilt.system_prompt == "be careful"
    assert rebuilt.model == "claude-sonnet-5"
    assert rebuilt.allowed_tools == ["Read"]
    assert rebuilt.max_turns == 5


def test_options_from_doc_rejects_unknown_keys():
    with pytest.raises(OptionsError, match="unknown option"):
        options_from_doc({"model": "sonnet", "workspace": "dev"})


def test_local_only_sdk_fields_raise():
    with pytest.raises(TypeError):
        AgentOptions(hooks={})
    with pytest.raises(TypeError):
        AgentOptions(env={"A": "b"})
    with pytest.raises(TypeError):
        AgentOptions(setting_sources=["user"])


def test_validate_requires_project():
    with pytest.raises(OptionsError, match="no GCP project"):
        AgentOptions().validate()


def test_validate_with_project(monkeypatch):
    monkeypatch.setenv("MILOS_PROJECT", "p1")
    AgentOptions().validate()


def test_validate_rejects_stdio_mcp(monkeypatch):
    monkeypatch.setenv("MILOS_PROJECT", "p1")
    with pytest.raises(OptionsError, match="stdio"):
        AgentOptions(mcp_servers={"local": {"type": "stdio", "command": "x"}}).validate()
    AgentOptions(mcp_servers={"ok": {"type": "http", "url": "https://x"}}).validate()


def test_validate_rejects_bad_agent_name(monkeypatch):
    monkeypatch.setenv("MILOS_PROJECT", "p1")
    with pytest.raises(OptionsError, match="agent"):
        AgentOptions(agent="Bad/Name").validate()


def test_system_prompt_preset():
    assert default_prompt() == {"type": "preset", "preset": "claude_code"}
    assert default_prompt("terse")["append"] == "terse"
    with pytest.raises(OptionsError, match="preset"):
        AgentOptions(system_prompt={"type": "preset", "preset": "file"}).validate()
    with pytest.raises(OptionsError, match="unknown key"):
        AgentOptions(system_prompt={"type": "preset", "preset": "claude_code", "x": 1}).validate()


def test_append_system_prompt_keeps_preset_shape():
    appended = append_system_prompt(default_prompt("a"), "b")
    assert appended["preset"] == "claude_code"
    assert appended["append"] == "a\n\nb"
    assert append_system_prompt("persona", "extra") == "persona\n\nextra"
    assert append_system_prompt("persona", "") == "persona"


def test_model_env_vertex_default(monkeypatch):
    monkeypatch.setenv("MILOS_PROJECT", "p1")
    env = model_env(AgentOptions())
    assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
    assert env["ANTHROPIC_VERTEX_PROJECT_ID"] == "p1"


def test_model_env_anthropic_needs_key(monkeypatch):
    monkeypatch.setenv("MILOS_PROJECT", "p1")
    with pytest.raises(OptionsError, match="ANTHROPIC_API_KEY"):
        model_env(AgentOptions(model_backend="anthropic"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert model_env(AgentOptions(model_backend="anthropic")) == {"ANTHROPIC_API_KEY": "k"}


def test_resolved_defaults(monkeypatch):
    monkeypatch.setenv("MILOS_PROJECT", "p1")
    options = AgentOptions()
    assert options.resolved_bucket() == "p1-milos"
    assert options.resolved_job() == "milos-runner"
    assert options.resolved_region() == "asia-northeast1"
