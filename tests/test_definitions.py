from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from milos.errors import Invalid
from milos.models import AgentVersion, DataScope

REPO = Path(__file__).resolve().parents[1]


def test_example_definition_is_valid():
    version = AgentVersion.from_yaml(REPO / "agents" / "general.yaml")
    assert version.agent_id == "general" and len(version.definition_sha256) == 64
    assert DataScope.of(version) == DataScope(agent_id="general", datasets=["weekly_numbers"], workspace="agent_general")


def write(tmp_path: Path, **overrides) -> Path:
    data = yaml.safe_load((REPO / "agents" / "general.yaml").read_text())
    for key, value in overrides.items():
        if value is ...:
            data.pop(key, None)
        else:
            data[key] = value
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"max_turns": None}, "max_turns"),
        ({"max_turns": 0}, "max_turns: Input should be greater than 0"),
        ({"max_budget_usd": 0}, "greater than 0"),
        ({"allowed_groups": []}, "at least one group"),
        ({"allowed_tools": ["WebFetch"]}, "connector"),
        ({"approval_required": ["NotebookEdit"]}, "not in allowed_tools"),
        ({"connectors": []}, "needs connector"),
        ({"owner": "nobody"}, "email"),
        ({"unexpected": 1}, "unexpected"),
        ({"workspace": False, "datasets": []}, "needs a workspace or datasets"),
        ({"workspace": False}, "mcp__internal__bq_write writes; it needs workspace: true"),
        ({"datasets": ["weekly-numbers"]}, "plain dataset ids"),
    ],
)
def test_invalid_definitions_are_rejected(tmp_path, overrides, message):
    with pytest.raises(Invalid) as error:
        AgentVersion.from_yaml(write(tmp_path, **overrides))
    assert message in str(error.value)


def test_system_prompt_is_optional(tmp_path):
    version = AgentVersion.from_yaml(write(tmp_path, system_prompt=...))
    assert version.system_prompt is None


def test_all_problems_are_reported_at_once(tmp_path):
    with pytest.raises(Invalid) as error:
        AgentVersion.from_yaml(write(tmp_path, max_turns=0, owner="nobody", allowed_tools=["Foo"], purpose=" "))
    message = str(error.value)
    assert all(word in message for word in ("max_turns", "owner", "unknown tool Foo", "purpose"))
