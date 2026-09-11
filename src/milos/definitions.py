"""Agent definitions: YAML in Git, validated by CI, published as immutable versions.

A definition is the unit of control. It names the purpose, the owner, who may
start it, which data classes it touches, which tools it may use and which of
those need a human, its limits, its model, and its own runner service
account. Nothing runs without one, and the registry (`registry()`) is
generated from what is published rather than maintained by hand.
"""

import fnmatch
import hashlib
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .errors import Invalid
from .models import DATA_CLASS, Agent, AgentVersion, utcnow

# Tools the SDK ships that must never be granted directly: the web goes
# through a connector, where the URL is logged and the host is checked.
FORBIDDEN_TOOLS = ("WebFetch", "WebSearch")
# The SDK's own tools an agent may be granted; anything else must be an MCP
# tool exposed by a connector (`mcp__<connector>__<tool>`).
SDK_TOOLS = ("Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "NotebookEdit", "Task")
# Limits a definition must set to a positive value.
LIMITS = ("max_turns", "max_budget_usd", "max_concurrent_sessions", "approval_ttl_sec")


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
        version = AgentVersion.model_validate(fields)
    except ValidationError as error:
        detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors())
        raise Invalid(f"invalid definition: {detail}") from error
    if problems := check(version):
        raise Invalid("invalid definition: " + "; ".join(problems))
    return version


def check(version: AgentVersion) -> list[str]:
    """Rules pydantic cannot express. Empty list means valid."""
    problems = [f"{limit} must be positive" for limit in LIMITS if getattr(version, limit) <= 0]
    if not version.allowed_groups:
        problems.append("allowed_groups must name at least one group")
    if "@" not in version.owner:
        problems.append("owner must be an email address")
    if not version.purpose.strip():
        problems.append("purpose must not be empty")
    if not version.runner_sa.endswith(".iam.gserviceaccount.com"):
        problems.append("runner_sa must be a service account email")
    for label in version.data_classes:
        if not DATA_CLASS.fullmatch(label):
            problems.append(f"data class {label!r} must be C followed by a number")
    for tool in version.allowed_tools:
        if tool in FORBIDDEN_TOOLS:
            problems.append(f"{tool} may not be granted directly; use a connector")
        elif not (tool in SDK_TOOLS or tool.startswith("mcp__")):
            problems.append(f"unknown tool {tool}")
    for tool in version.approval_required:
        if not any(fnmatch.fnmatchcase(tool, p) or tool == p for p in version.allowed_tools):
            problems.append(f"approval_required entry {tool} is not in allowed_tools")
    for tool in version.allowed_tools:
        if tool.startswith("mcp__"):
            name, separator, _ = tool.removeprefix("mcp__").partition("__")
            connector = name if separator else ""
            if connector not in version.connectors:
                problems.append(f"{tool} needs connector {connector!r} in connectors")
    return problems


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
