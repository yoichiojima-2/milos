from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from milos.errors import Invalid
from milos.models import AgentVersion

REPO = Path(__file__).resolve().parents[1]


def test_example_definition_is_valid():
    version = AgentVersion.from_yaml(REPO / "agents" / "analyst.yaml")
    assert version.agent_id == "analyst" and len(version.definition_sha256) == 64


def write(tmp_path: Path, **overrides) -> Path:
    data = yaml.safe_load((REPO / "agents" / "analyst.yaml").read_text())
    data.update(overrides)
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
        ({"approval_required": ["Edit"]}, "not in allowed_tools"),
        ({"connectors": []}, "needs connector"),
        ({"owner": "nobody"}, "email"),
        ({"unexpected": 1}, "unexpected"),
    ],
)
def test_invalid_definitions_are_rejected(tmp_path, overrides, message):
    with pytest.raises(Invalid) as error:
        AgentVersion.from_yaml(write(tmp_path, **overrides))
    assert message in str(error.value)


def test_all_problems_are_reported_at_once(tmp_path):
    with pytest.raises(Invalid) as error:
        AgentVersion.from_yaml(write(tmp_path, max_turns=0, owner="nobody", allowed_tools=["Foo"], purpose=" "))
    message = str(error.value)
    assert all(word in message for word in ("max_turns", "owner", "unknown tool Foo", "purpose"))
