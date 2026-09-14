"""Agent definitions: YAML in Git, validated by CI, published as immutable versions.

A definition is the unit of control. It names the purpose, the owner, who may
start it, which data classes it touches, which tools it may use and which of
those need a human, its limits, its model, and its own runner service
account. The rules are the validators on `models.AgentVersion`. Nothing runs
without a definition, and the registry (`registry()`) is generated from what
is published rather than maintained by hand.
"""

import hashlib
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .errors import Invalid
from .models import Agent, AgentVersion, utcnow


def load(path: str | Path) -> AgentVersion:
    """Parse and validate one definition file. Raises `Invalid` with every problem found."""
    raw = Path(path).read_bytes()
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise Invalid(f"{path}: definition must be a mapping")
    return build(data, definition_sha256=hashlib.sha256(raw).hexdigest())


def build(data: dict[str, Any], *, definition_sha256: str) -> AgentVersion:
    fields = {**data, "version": 0, "definition_sha256": definition_sha256, "published_at": utcnow()}
    try:
        return AgentVersion.model_validate(fields)
    except ValidationError as error:
        raise Invalid("invalid definition: " + "; ".join(_problem(e) for e in error.errors())) from error


def _problem(error: Any) -> str:
    """`field: message`, without pydantic's "Value error, " prefix on our own messages."""
    message = str(error["msg"]).removeprefix("Value error, ")
    location = ".".join(str(part) for part in error["loc"])
    return f"{location}: {message}" if location else message


# --- the AI usage register --------------------------------------------------------

COLUMNS: tuple[tuple[str, Callable[[Agent, AgentVersion], str]], ...] = (
    ("Agent", lambda agent, _: agent.agent_id),
    ("Version", lambda _, version: str(version.version)),
    ("Enabled", lambda agent, _: "yes" if agent.enabled else "no"),
    ("Purpose", lambda _, version: version.purpose),
    ("Owner", lambda _, version: version.owner),
    ("Data classes", lambda _, version: ", ".join(version.data_classes)),
    ("Tools", lambda _, version: ", ".join(version.allowed_tools)),
    ("Needs approval", lambda _, version: ", ".join(version.approval_required) or "—"),
    ("Runner SA", lambda _, version: version.runner_sa),
)


def registry(rows: list[tuple[Agent, AgentVersion]]) -> str:
    """The AI usage register as Markdown, generated from published definitions."""

    def line(cells: Iterable[str]) -> str:
        return "| " + " | ".join(cells) + " |"

    header = line(heading for heading, _ in COLUMNS)
    rule = line("---" for _ in COLUMNS)
    body = [line(cell(agent, version) for _, cell in COLUMNS) for agent, version in rows]
    return "\n".join([header, rule, *body]) + "\n"
