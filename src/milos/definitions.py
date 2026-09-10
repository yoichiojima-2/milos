"""Agent definitions: YAML in Git, validated by CI, published as immutable versions.

A definition is the unit of control. It names the purpose, the owner, who may
start it, which data classes it touches, which tools it may use and which of
those need a human, its limits, its model, and its own runner service
account. Nothing runs without one, and the registry (`registry()`) is
generated from what is published rather than maintained by hand.
"""

from __future__ import annotations

import fnmatch
import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .errors import Invalid
from .models import Agent, AgentVersion, utcnow

# Tools the SDK ships that must never be granted directly: the web goes
# through a connector, where the URL is logged and the host is checked.
FORBIDDEN_TOOLS = ("WebFetch", "WebSearch")
# The SDK's own tools an agent may be granted; anything else must be an MCP
# tool exposed by a connector (`mcp__<connector>__<tool>`).
SDK_TOOLS = ("Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "NotebookEdit", "Task")


def load(path: str | Path) -> AgentVersion:
    """Parse and validate one definition file. Raises `Invalid` with every problem found."""
    raw = Path(path).read_bytes()
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise Invalid(f"{path}: definition must be a mapping")
    return build(data, definition_sha256=hashlib.sha256(raw).hexdigest())


def build(data: dict[str, Any], *, definition_sha256: str) -> AgentVersion:
    fields = {
        **data,
        "version": 0,
        "definition_sha256": definition_sha256,
        "published_at": utcnow(),
    }
    try:
        version = AgentVersion(**fields)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors()
        )
        raise Invalid(f"invalid definition: {problems}") from error
    problems = check(version)
    if problems:
        raise Invalid("invalid definition: " + "; ".join(problems))
    return version


def check(version: AgentVersion) -> list[str]:
    """Rules pydantic cannot express. Empty list means valid."""
    problems = []
    if version.max_turns <= 0:
        problems.append("max_turns must be positive")
    if version.max_budget_usd <= 0:
        problems.append("max_budget_usd must be positive")
    if version.max_concurrent_sessions <= 0:
        problems.append("max_concurrent_sessions must be positive")
    if version.approval_ttl_sec <= 0:
        problems.append("approval_ttl_sec must be positive")
    if not version.allowed_groups:
        problems.append("allowed_groups must name at least one group")
    if "@" not in version.owner:
        problems.append("owner must be an email address")
    if not version.purpose.strip():
        problems.append("purpose must not be empty")
    if not version.runner_sa.endswith(".iam.gserviceaccount.com"):
        problems.append("runner_sa must be a service account email")
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
            connector = tool.split("__")[1] if tool.count("__") >= 2 else ""
            if connector not in version.connectors:
                problems.append(f"{tool} needs connector {connector!r} in connectors")
    return problems


def registry(rows: list[tuple[Agent, AgentVersion]]) -> str:
    """The AI usage register as Markdown, generated from published definitions."""
    lines = [
        "| Agent | Version | Enabled | Purpose | Owner | Data classes | Tools | Needs approval | Runner SA |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for agent, version in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    agent.agent_id,
                    str(version.version),
                    "yes" if agent.enabled else "no",
                    version.purpose,
                    version.owner,
                    ", ".join(version.data_classes),
                    ", ".join(version.allowed_tools),
                    ", ".join(version.approval_required) or "—",
                    version.runner_sa,
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"
