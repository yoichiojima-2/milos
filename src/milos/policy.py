"""The policy engine — org-level rules every session runs under, as pure functions.

A policy is one YAML document, applied via `milos policies apply`, stored as an
immutable version in `policies/{vNNNNNN}` with `settings/global.policy_version`
naming the active one. Enforcement happens at exactly two points, both in code:

  - session creation: `check_session` rejects disallowed model/backend/
    permission-mode/tool choices and clamps budgets, then the winning
    `policy_version` + `policy_hash` are stamped onto the session doc;
  - the gate: the runner loads the policy pinned at the session's version and
    the PreToolUse hook consults `tool_denied` / `approval_rule` on every call.

Nothing here does I/O — the store owns versions and pointers, this module owns
what a policy means. That split is what keeps the engine testable without GCP
and the store free of rule semantics.

ISO mapping: policy-as-code is control MC-02 in docs/compliance/controls.md
(ISO 27001 A.5.1 policies / A.8.2 privileged access; ISO 42001 6.1.2, 8.2).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from typing import Any

from .errors import PolicyError

# Every key a policy document may carry, with its default. One table drives
# validation, normalization, and documentation — an unknown key is an error,
# never a silent drop, because this is the path operator YAML takes.
_SECTIONS: dict[str, Any] = {
    "models": {"allow": ["*"]},
    "model_backends": ["vertex"],
    "permission_modes": {"deny": []},
    "tools": {"deny": []},
    "require_approval": [],
    "budgets": {},
    "oversight": {"require_risk_block": False},
    "retention": {},
}
_BUDGET_KEYS = ("max_budget_usd", "max_turns")
_RETENTION_KEYS = ("session_state_days", "journal_days")
_RULE_KEYS = ("tools", "reason")

RISK_IMPACTS = ("low", "medium", "high")
RISK_KEYS = ("purpose", "impact", "owner", "review_by")


def canonical_hash(policy: dict[str, Any]) -> str:
    """Deterministic content hash of a normalized policy — the value stamped
    into every journal record's context, so evidence can prove which rules
    were in force. Same canonicalization as gate.call_hash."""
    canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _string_list(where: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise PolicyError(f"{where} must be a list of strings")
    return list(value)


def validate_policy(doc: Any) -> dict[str, Any]:
    """Normalize an operator-supplied policy document, filling defaults.

    Returns the full normalized shape (every section present), which is what
    gets hashed and stored — so two YAMLs that mean the same thing get the
    same hash, and a stored policy never needs defaulting again at read time.
    """
    if not isinstance(doc, dict):
        raise PolicyError("policy must be a mapping")
    if unknown := sorted(set(doc) - set(_SECTIONS)):
        raise PolicyError(f"unknown policy key(s): {', '.join(unknown)}")

    policy: dict[str, Any] = {}

    models = doc.get("models", _SECTIONS["models"])
    if not isinstance(models, dict) or set(models) - {"allow"}:
        raise PolicyError("models must be a mapping with an 'allow' list")
    policy["models"] = {"allow": _string_list("models.allow", models.get("allow", ["*"]))}

    backends = _string_list("model_backends", doc.get("model_backends", ["vertex"]))
    if invalid := sorted(set(backends) - {"vertex", "anthropic"}):
        raise PolicyError(f"model_backends: unknown backend(s): {', '.join(invalid)}")
    if not backends:
        raise PolicyError("model_backends must allow at least one backend")
    policy["model_backends"] = backends

    modes = doc.get("permission_modes", _SECTIONS["permission_modes"])
    if not isinstance(modes, dict) or set(modes) - {"deny"}:
        raise PolicyError("permission_modes must be a mapping with a 'deny' list")
    policy["permission_modes"] = {"deny": _string_list("permission_modes.deny", modes.get("deny", []))}

    tools = doc.get("tools", _SECTIONS["tools"])
    if not isinstance(tools, dict) or set(tools) - {"deny"}:
        raise PolicyError("tools must be a mapping with a 'deny' list")
    policy["tools"] = {"deny": _string_list("tools.deny", tools.get("deny", []))}

    rules = doc.get("require_approval", [])
    if not isinstance(rules, list):
        raise PolicyError("require_approval must be a list of rules")
    normalized_rules = []
    for i, rule in enumerate(rules):
        where = f"require_approval[{i}]"
        if not isinstance(rule, dict):
            raise PolicyError(f"{where} must be a mapping")
        if unknown := sorted(set(rule) - set(_RULE_KEYS)):
            raise PolicyError(f"{where}: unknown key(s): {', '.join(unknown)}")
        patterns = _string_list(f"{where}.tools", rule.get("tools", []))
        if not patterns:
            raise PolicyError(f"{where}: 'tools' must name at least one pattern")
        reason = rule.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise PolicyError(f"{where}: 'reason' must be a string")
        normalized_rules.append({"tools": patterns, "reason": reason or "required by policy"})
    policy["require_approval"] = normalized_rules

    budgets = doc.get("budgets", {})
    if not isinstance(budgets, dict):
        raise PolicyError("budgets must be a mapping")
    if unknown := sorted(set(budgets) - set(_BUDGET_KEYS)):
        raise PolicyError(f"budgets: unknown key(s): {', '.join(unknown)}")
    for key in _BUDGET_KEYS:
        if key in budgets and not isinstance(budgets[key], (int, float)):
            raise PolicyError(f"budgets.{key} must be a number")
        if key in budgets and budgets[key] <= 0:
            raise PolicyError(f"budgets.{key} must be positive")
    policy["budgets"] = dict(budgets)

    oversight = doc.get("oversight", _SECTIONS["oversight"])
    if not isinstance(oversight, dict) or set(oversight) - {"require_risk_block"}:
        raise PolicyError("oversight must be a mapping with 'require_risk_block'")
    require = oversight.get("require_risk_block", False)
    if not isinstance(require, bool):
        raise PolicyError("oversight.require_risk_block must be true or false")
    policy["oversight"] = {"require_risk_block": require}

    retention = doc.get("retention", {})
    if not isinstance(retention, dict):
        raise PolicyError("retention must be a mapping")
    if unknown := sorted(set(retention) - set(_RETENTION_KEYS)):
        raise PolicyError(f"retention: unknown key(s): {', '.join(unknown)}")
    for key in _RETENTION_KEYS:
        if key in retention and (not isinstance(retention[key], int) or retention[key] <= 0):
            raise PolicyError(f"retention.{key} must be a positive integer (days)")
    policy["retention"] = dict(retention)

    return policy


def policy_from_yaml(text: str) -> dict[str, Any]:
    """Parse and normalize an operator's policy YAML."""
    import yaml

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy is not valid YAML: {exc}") from exc
    return validate_policy(doc if doc is not None else {})


def _matches(patterns: list[str], name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def tool_denied(policy: dict[str, Any], tool_name: str) -> bool:
    """Whether the policy bans this tool outright (gate: audit row records
    decision "policy_denied" and the call never runs)."""
    return _matches(policy["tools"]["deny"], tool_name)


def approval_rule(policy: dict[str, Any], tool_name: str) -> dict[str, Any] | None:
    """The first require_approval rule matching this tool, or None. A match
    forces the call through the human approval queue even when the session's
    permission_mode would otherwise let it through."""
    for rule in policy["require_approval"]:
        if _matches(rule["tools"], tool_name):
            return rule
    return None


def check_session(options: Any, policy: dict[str, Any]) -> None:
    """Reject or adjust a session's resolved options against the policy.

    Called at session creation, after the option-resolution chain has merged
    every layer — so the policy judges what the run would actually get, not
    one layer of it. Rejections raise PolicyError; budget ceilings mutate the
    options in place (clamped when set higher, injected when absent), which the
    caller then snapshots onto the session, so the stored options already obey
    the policy and the runner needs no second clamp.
    """
    allowed_models = policy["models"]["allow"]
    if options.model is not None and not _matches(allowed_models, options.model):
        raise PolicyError(
            f"policy denies model {options.model!r} — allowed: {', '.join(allowed_models)}"
        )
    backend = options.resolved_model_backend()
    if backend not in policy["model_backends"]:
        raise PolicyError(
            f"policy denies model backend {backend!r} —"
            f" allowed: {', '.join(policy['model_backends'])}"
        )
    if options.permission_mode is not None and _matches(
        policy["permission_modes"]["deny"], options.permission_mode
    ):
        raise PolicyError(f"policy denies permission_mode {options.permission_mode!r}")
    for tool in list(options.allowed_tools) + list(options.tools or []):
        if tool_denied(policy, tool):
            raise PolicyError(f"policy denies tool {tool!r}")
    budgets = policy["budgets"]
    if (ceiling := budgets.get("max_budget_usd")) is not None:
        if options.max_budget_usd is None or options.max_budget_usd > ceiling:
            options.max_budget_usd = float(ceiling)
    if (ceiling := budgets.get("max_turns")) is not None:
        if options.max_turns is None or options.max_turns > ceiling:
            options.max_turns = int(ceiling)


def validate_risk(risk: Any) -> dict[str, Any]:
    """Check an agent's AI risk block (ISO 42001 6.1.2): purpose, impact
    level, accountable owner, and a review-by date. Enforced by agents.create/
    update when the active policy sets oversight.require_risk_block."""
    if not isinstance(risk, dict):
        raise PolicyError("risk must be a mapping with purpose, impact, owner, review_by")
    if unknown := sorted(set(risk) - set(RISK_KEYS)):
        raise PolicyError(f"risk: unknown key(s): {', '.join(unknown)}")
    if missing := sorted(set(RISK_KEYS) - set(risk)):
        raise PolicyError(f"risk: missing key(s): {', '.join(missing)}")
    for key in ("purpose", "owner", "review_by"):
        if not isinstance(risk[key], str) or not risk[key].strip():
            raise PolicyError(f"risk.{key} must be a non-empty string")
    if risk["impact"] not in RISK_IMPACTS:
        raise PolicyError(f"risk.impact must be one of {', '.join(RISK_IMPACTS)}")
    return {key: risk[key] for key in RISK_KEYS}
