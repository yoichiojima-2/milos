"""Agents: named, stored run configurations — the persona a session runs as.

An agent is one Firestore document holding the same serialized `AgentOptions`
subset a session stores, plus its AI risk block. Referencing it
(`AgentOptions(agent=...)`) resolves the stored options as defaults, with any
explicitly-set option on the caller's side overriding them. Resolution happens
when a session is created and the merged result is snapshotted onto the
session, so editing an agent changes future runs only.

The compliance half (ISO 42001):
  - `risk` — {purpose, impact, owner, review_by} — is required by
    agents.create/update when the active policy sets
    oversight.require_risk_block. The agents collection *is* the AI risk
    register; `milos agents` flags entries past their review date.
  - every update first snapshots the current doc into agents/{name}/revisions,
    so the collection doubles as the prompt/config version history.

    agents/{name}
        options       the serialized AgentOptions persona subset
        risk          the AI risk block, or None
        description   free text, for humans
        created_by
    agents/{name}/revisions/{n}   the doc before each update
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .errors import MilosError
from .names import validate_name
from .options import (
    _SERIALIZED_FIELDS,
    DEFAULT_MODEL,
    AgentOptions,
    default_prompt,
    options_from_doc,
)
from .policy import validate_risk
from .store import StoreProtocol, store_or_default as _store


class AgentError(MilosError):
    """An agent definition is invalid, missing, or already exists."""


async def _check_risk(store: StoreProtocol, risk: Any) -> dict[str, Any] | None:
    """Validate the risk block, and require one when the active policy says so.

    A policy-less install accepts agents without risk blocks — the same
    fail-open posture is *not* extended to sessions (remote.attach_session
    refuses to run without a policy), because an unreviewed definition is
    recoverable and an ungoverned run is not.
    """
    if risk is not None:
        return validate_risk(risk)
    active = await store.get_active_policy()
    if active and active["policy"]["oversight"]["require_risk_block"]:
        raise AgentError(
            "the active policy requires a risk block on every agent —"
            " pass risk={purpose, impact, owner, review_by}"
        )
    return None


def build(
    name: str,
    run_options: AgentOptions | None = None,
    *,
    risk: dict[str, Any] | None = None,
    description: str | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    """The Firestore document for an agent."""
    validate_name("agent", name)
    return {
        "options": (run_options or AgentOptions()).serialize(),
        "risk": risk,
        "description": description,
        "created_by": created_by,
    }


async def create(
    name: str,
    run_options: AgentOptions | None = None,
    *,
    options: AgentOptions | None = None,
    risk: dict[str, Any] | None = None,
    description: str | None = None,
    created_by: str | None = None,
    store: StoreProtocol | None = None,
) -> dict[str, Any]:
    """Define an agent. `options` is the installation coordinates (project);
    `run_options` is the persona every run referencing this agent is given."""
    options = options or AgentOptions()
    run_options = run_options or AgentOptions()
    run_options.project = run_options.project or options.project
    run_options.validate()
    store = _store(options, store)
    risk = await _check_risk(store, risk)
    doc = build(name, run_options, risk=risk, description=description, created_by=created_by)
    if await store.get_agent(name) is not None:
        raise AgentError(f"agent {name!r} already exists")
    await store.create_agent(name, doc)
    return {"name": name, **doc}


async def get(
    name: str, *, options: AgentOptions | None = None, store: StoreProtocol | None = None
) -> dict[str, Any] | None:
    return await _store(options or AgentOptions(), store).get_agent(name)


async def list_all(
    *, options: AgentOptions | None = None, store: StoreProtocol | None = None
) -> list[dict[str, Any]]:
    agents = await _store(options or AgentOptions(), store).list_agents()
    return sorted(agents, key=lambda a: a.get("name") or "")


async def update(
    name: str,
    run_options: AgentOptions,
    *,
    options: AgentOptions | None = None,
    risk: dict[str, Any] | None = None,
    description: str | None = None,
    store: StoreProtocol | None = None,
) -> dict[str, Any]:
    """Replace an agent's stored options. Running sessions keep the options
    they were created with; the next run picks up the new configuration.

    The doc as it stands is archived to revisions/ first — the version history
    exists because updates go through here, so nothing else may skip it."""
    options = options or AgentOptions()
    run_options.project = run_options.project or options.project
    run_options.validate()
    store = _store(options, store)
    agent = await require_agent(store, name)
    # An update that doesn't mention risk keeps the existing block — dropping
    # the register entry should be deliberate, not a forgotten flag.
    risk = await _check_risk(store, risk if risk is not None else agent.get("risk"))
    await store.create_agent_revision(name, {k: v for k, v in agent.items() if k != "name"})
    fields: dict[str, Any] = {"options": run_options.serialize(), "risk": risk}
    if description is not None:
        fields["description"] = description
    await store.update_agent(name, **fields)
    return {**agent, **fields}


async def revisions(
    name: str, *, options: AgentOptions | None = None, store: StoreProtocol | None = None
) -> list[dict[str, Any]]:
    store = _store(options or AgentOptions(), store)
    await require_agent(store, name)
    return await store.list_agent_revisions(name)


async def delete(
    name: str, *, options: AgentOptions | None = None, store: StoreProtocol | None = None
) -> None:
    """Remove the agent. Sessions that ran as it stored their own options and
    stay — the evidence record survives its definition."""
    store = _store(options or AgentOptions(), store)
    await require_agent(store, name)
    await store.delete_agent(name)


async def require_agent(store: StoreProtocol, name: str) -> dict[str, Any]:
    agent = await store.get_agent(name)
    if agent is None:
        raise AgentError(f"no such agent: {name}")
    return agent


def merge(base: AgentOptions, overrides: AgentOptions) -> AgentOptions:
    """Overlay `overrides` on `base`, field by field, for the serialized subset.

    A field counts as overridden when it differs from a fresh AgentOptions()
    default (defaults are all None/[]/{} — so there is no way, and no need, to
    explicitly ask for the default back). Installation coordinates (project,
    region, job, ...) and callback fields always come from `overrides`.
    """
    defaults = AgentOptions()
    inherited = {
        field: getattr(base, field)
        for field in _SERIALIZED_FIELDS
        if getattr(overrides, field) == getattr(defaults, field)
    }
    return replace(overrides, **inherited)


async def resolve(store: StoreProtocol, options: AgentOptions) -> AgentOptions:
    """Expand references into concrete options, layered as

        explicit options  <-  agent  <-  settings/global

    Explicitly-set fields always win over any stored layer. Only the top-level
    options may name an agent — a stored layer naming one is ignored (merge
    never overrides a set field, and nesting would recurse). The model lands
    on "sonnet" when no layer names one, so a session never records no model,
    and the system prompt lands on the harness's own the same way. (An
    explicit "" is how you ask for no system prompt at all.)"""
    merged = options
    if options.agent:
        agent = await require_agent(store, options.agent)
        merged = merge(options_from_doc(dict(agent.get("options") or {})), merged)
    settings = await store.get_settings()
    if settings and settings.get("options"):
        merged = merge(options_from_doc(dict(settings.get("options") or {})), merged)
    return replace(
        merged,
        model=merged.model or DEFAULT_MODEL,
        system_prompt=(default_prompt() if merged.system_prompt is None else merged.system_prompt),
    )
