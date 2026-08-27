"""AgentOptions — the serializable, sandbox-safe subset of ClaudeAgentOptions.

Only options the GCP sandbox can honour are defined here. Machine-local
ClaudeAgentOptions (cwd, env, hooks, add_dirs, setting_sources, stdio MCP
servers, ...) are deliberately absent: passing them raises TypeError at the
constructor rather than silently doing nothing in the sandbox. Hooks in
particular are the platform's own — a caller-supplied hook could displace the
audit hook, so the field does not exist.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal, cast, get_args

from . import env
from .errors import OptionsError
from .names import validate_name
from .types import CanUseTool

PermissionMode = Literal["default", "acceptEdits", "bypassPermissions", "plan", "dontAsk"]
ModelBackend = Literal["vertex", "anthropic"]

_SERIALIZED_FIELDS = (
    "system_prompt",
    "model",
    "tools",
    "allowed_tools",
    "disallowed_tools",
    "permission_mode",
    "mcp_servers",
    "max_turns",
    "max_budget_usd",
    "workspace",
)

# The built-in floor of the option-resolution chain (agents.resolve): a session
# never records no model, whatever the stored layers say.
DEFAULT_MODEL = "sonnet"

# claude_agent_sdk starts a run with *no* system prompt — a bare assistant. The
# harness's own prompt, the one that makes it the default coding agent, is a
# preset, and this is its name on the wire (claude_agent_sdk's spelling — the
# option milos calls "the default prompt"). It is the one preset milos defines:
# a "file" preset would name a path on a machine the sandbox doesn't have.
DEFAULT_PROMPT_PRESET = "claude_code"
_PRESET_KEYS = ("type", "preset", "append")


def default_prompt(append: str | None = None) -> dict[str, Any]:
    """The `system_prompt` value that runs the harness's default agent.

    An unset `system_prompt` already resolves to this (agents.resolve), so
    naming it explicitly is for the two cases that differ: adding instructions
    after that prompt instead of replacing it (`append`), and pinning it over a
    persona a stored layer would otherwise contribute.
    """
    prompt: dict[str, Any] = {"type": "preset", "preset": DEFAULT_PROMPT_PRESET}
    if append:
        prompt["append"] = append
    return prompt


def append_system_prompt(system_prompt: Any, text: str) -> Any:
    """Add platform-owned instructions to whatever the session configured.

    A preset keeps its shape — the addition rides its `append`, so appending to
    it never quietly turns the default prompt into a plain string that replaces
    it.
    """
    if not text:
        return system_prompt
    if isinstance(system_prompt, dict):
        appended = "\n\n".join(filter(None, (system_prompt.get("append"), text)))
        return {**system_prompt, "append": appended}
    return "\n\n".join(filter(None, (system_prompt, text)))


def _validate_system_prompt(system_prompt: Any) -> None:
    if system_prompt is None or isinstance(system_prompt, str):
        return
    preset = f'{{"type": "preset", "preset": "{DEFAULT_PROMPT_PRESET}"}}'
    if not isinstance(system_prompt, dict):
        raise OptionsError(f"system_prompt must be a plain string or {preset} in milos")
    if (
        system_prompt.get("type") != "preset"
        or system_prompt.get("preset") != DEFAULT_PROMPT_PRESET
    ):
        raise OptionsError(
            f"system_prompt: the only preset milos defines is {preset} —"
            " a file preset would name a path the sandbox doesn't have"
        )
    if unknown := sorted(set(system_prompt) - set(_PRESET_KEYS)):
        raise OptionsError(f"system_prompt preset: unknown key(s): {', '.join(unknown)}")
    if (append := system_prompt.get("append")) is not None and not isinstance(append, str):
        raise OptionsError("system_prompt preset: 'append' must be a string")


@dataclass
class AgentOptions:
    # --- mirrored from claude_agent_sdk; same semantics, run in the sandbox ---
    # A plain string is a persona, replacing the harness's own prompt;
    # default_prompt() keeps that prompt and optionally appends to it. Left
    # unset in every layer, a session resolves to default_prompt() — an
    # explicit "" is what asks for no system prompt at all.
    system_prompt: str | dict[str, Any] | None = None
    model: str | None = None
    tools: list[str] | None = None
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    permission_mode: PermissionMode | None = None
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)  # http/sse only
    max_turns: int | None = None
    max_budget_usd: float | None = None
    resume: str | None = None  # milos session id (sess_...)
    # Rewind: with resume, branch the transcript from this past event (its
    # uuid) instead of continuing the current tip. Conversation-only — the
    # workspace keeps its latest checkpoint — and snapped to the nearest turn
    # boundary at or before the event.
    from_event: str | None = None
    can_use_tool: CanUseTool | None = None

    # --- milos ---
    # Named stored agent (agents/{name} in Firestore): its saved options become
    # the defaults for this run, and any field set explicitly here overrides
    # them. Resolved when the session is created; the session stores the merged
    # result, so later edits to the agent never change a running session.
    agent: str | None = None
    # Named workspace (workspaces/{name} in Firestore): sessions under the same
    # workspace share one working directory (workspaces/{name}/ws/ in the
    # bucket, exclusive lease) and the workspace's skills, and inherit the
    # stored options as defaults (under agent, over global settings). HOME
    # stays per-session, so transcripts and resume are unaffected. Fixed at
    # session creation; on resume the stored options win, like every
    # serialized field.
    workspace: str | None = None
    project: str | None = None  # default: $MILOS_PROJECT or $GOOGLE_CLOUD_PROJECT
    region: str | None = None  # Cloud Run region; default: $MILOS_REGION or asia-northeast1
    vertex_region: str | None = None  # default: $CLOUD_ML_REGION or global
    # Deployment-scoped, so it is not serialized to the runner: the sandbox reads
    # $MILOS_MODEL_BACKEND from its own environment, where the key is mounted.
    # The active policy's model_backends list decides whether it is allowed.
    model_backend: ModelBackend | None = None  # default: $MILOS_MODEL_BACKEND or vertex
    bucket: str | None = None  # default: $MILOS_BUCKET or {project}-milos
    job: str | None = None  # Cloud Run Job name; default: $MILOS_JOB or milos-runner

    def resolved_project(self) -> str:
        project = env.find_project(self.project)
        if not project:
            raise OptionsError(
                "no GCP project configured: set AgentOptions.project or $MILOS_PROJECT"
            )
        return project

    def resolved_region(self) -> str:
        return self.region or os.environ.get("MILOS_REGION") or "asia-northeast1"

    def resolved_vertex_region(self) -> str:
        return self.vertex_region or os.environ.get("CLOUD_ML_REGION") or "global"

    def resolved_model_backend(self) -> ModelBackend:
        backend = self.model_backend or os.environ.get("MILOS_MODEL_BACKEND") or "vertex"
        if backend not in get_args(ModelBackend):
            raise OptionsError(f"unknown model_backend {backend!r}: use 'vertex' or 'anthropic'")
        return cast(ModelBackend, backend)

    def resolved_bucket(self) -> str:
        return env.default_bucket(self.bucket, self.resolved_project())

    def resolved_job(self) -> str:
        return self.job or os.environ.get("MILOS_JOB") or "milos-runner"

    def validate(self) -> None:
        _validate_system_prompt(self.system_prompt)
        if self.agent is not None:
            validate_name("agent", self.agent)
        if self.workspace is not None:
            validate_name("workspace", self.workspace)
        for name, config in self.mcp_servers.items():
            if not isinstance(config, dict):
                raise OptionsError(
                    f"mcp server {name!r}: only dict configs (http/sse) are supported"
                )
            if config.get("type") not in ("http", "sse"):
                raise OptionsError(
                    f"mcp server {name!r}: type must be 'http' or 'sse' — stdio and"
                    " caller-defined in-process servers cannot run in the sandbox"
                )
        self.resolved_project()

    def serialize(self) -> dict[str, Any]:
        """The option subset that travels to the remote runner (JSON/Firestore-safe)."""
        return {name: getattr(self, name) for name in _SERIALIZED_FIELDS}


def options_from_doc(doc: dict[str, Any]) -> AgentOptions:
    """Rebuild AgentOptions from a serialized dict (the inverse of serialize()).

    Unknown keys are an error rather than a silent drop: this is the path
    untrusted input takes when a stored agent is defined, and an option that
    quietly did nothing would be worse than a rejected form.
    """
    unknown = sorted(set(doc) - set(_SERIALIZED_FIELDS))
    if unknown:
        raise OptionsError(f"unknown option(s): {', '.join(unknown)}")
    return AgentOptions(**doc)


def build_sdk_options(
    options: AgentOptions,
    *,
    can_use_tool: CanUseTool | None = None,
    cwd: str | None = None,
    resume: str | None = None,
    fork_session: bool = False,
    env: dict[str, str] | None = None,
    setting_sources: list[str] | None = None,
    hooks: Any = None,
) -> Any:
    """Build a ClaudeAgentOptions from the serializable option subset — the one
    place the milos subset becomes the SDK's own type."""
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        setting_sources=setting_sources,
        fork_session=fork_session,
        system_prompt=options.system_prompt,
        model=options.model,
        tools=options.tools,
        allowed_tools=list(options.allowed_tools),
        disallowed_tools=list(options.disallowed_tools),
        permission_mode=options.permission_mode,
        mcp_servers=dict(options.mcp_servers),
        max_turns=options.max_turns,
        max_budget_usd=options.max_budget_usd,
        can_use_tool=can_use_tool,
        cwd=cwd,
        resume=resume,
        env=dict(env or {}),
        hooks=hooks,
    )


def model_env(options: AgentOptions) -> dict[str, str]:
    """Env vars that route claude_agent_sdk's model calls to a backend.

    Vertex by default, keyed on the GCP project — model traffic stays inside
    GCP. Backend "anthropic" calls the Anthropic API instead; whether a
    deployment permits that is the policy's `model_backends` decision, checked
    at session creation, never a fallback when a key happens to be present.
    """
    if options.resolved_model_backend() == "anthropic":
        if not (key := os.environ.get("ANTHROPIC_API_KEY")):
            raise OptionsError("model_backend='anthropic' requires $ANTHROPIC_API_KEY")
        return {"ANTHROPIC_API_KEY": key}
    return {
        "CLAUDE_CODE_USE_VERTEX": "1",
        "ANTHROPIC_VERTEX_PROJECT_ID": options.resolved_project(),
        "CLOUD_ML_REGION": options.resolved_vertex_region(),
    }
