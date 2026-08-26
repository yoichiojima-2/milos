import pytest

from milos.errors import PolicyError
from milos.options import AgentOptions
from milos.policy import (
    approval_rule,
    canonical_hash,
    check_session,
    policy_from_yaml,
    tool_denied,
    validate_policy,
    validate_risk,
)

PERMISSIVE = validate_policy({})


def test_empty_policy_normalizes_to_full_shape():
    assert PERMISSIVE["models"] == {"allow": ["*"]}
    assert PERMISSIVE["model_backends"] == ["vertex"]
    assert PERMISSIVE["require_approval"] == []
    assert PERMISSIVE["oversight"] == {"require_risk_block": False}


def test_unknown_keys_rejected():
    with pytest.raises(PolicyError, match="unknown policy key"):
        validate_policy({"modelz": {}})
    with pytest.raises(PolicyError, match="budgets"):
        validate_policy({"budgets": {"max_cost": 1}})
    with pytest.raises(PolicyError, match="require_approval"):
        validate_policy({"require_approval": [{"tool": ["Bash"]}]})


def test_equivalent_policies_hash_equal():
    assert canonical_hash(validate_policy({})) == canonical_hash(
        validate_policy({"models": {"allow": ["*"]}})
    )
    assert canonical_hash(validate_policy({"tools": {"deny": ["Bash"]}})) != canonical_hash(
        PERMISSIVE
    )


def test_policy_from_yaml():
    policy = policy_from_yaml("tools:\n  deny: [WebSearch]\nmodel_backends: [vertex]\n")
    assert policy["tools"]["deny"] == ["WebSearch"]
    with pytest.raises(PolicyError, match="YAML"):
        policy_from_yaml("a: [unclosed")


def test_tool_denied_globs():
    policy = validate_policy({"tools": {"deny": ["Bash", "mcp__*"]}})
    assert tool_denied(policy, "Bash")
    assert tool_denied(policy, "mcp__slack__post")
    assert not tool_denied(policy, "Read")


def test_approval_rule_matches_first():
    policy = validate_policy(
        {"require_approval": [{"tools": ["Bash"], "reason": "shell"}, {"tools": ["*"]}]}
    )
    assert approval_rule(policy, "Bash")["reason"] == "shell"
    assert approval_rule(policy, "Read")["reason"] == "required by policy"
    assert approval_rule(PERMISSIVE, "Bash") is None


def test_check_session_model_allowlist(monkeypatch):
    monkeypatch.setenv("MILOS_PROJECT", "p1")
    policy = validate_policy({"models": {"allow": ["sonnet", "claude-sonnet-*"]}})
    check_session(AgentOptions(model="claude-sonnet-5"), policy)
    with pytest.raises(PolicyError, match="denies model"):
        check_session(AgentOptions(model="claude-opus-5"), policy)


def test_check_session_backend(monkeypatch):
    monkeypatch.setenv("MILOS_PROJECT", "p1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    with pytest.raises(PolicyError, match="backend"):
        check_session(AgentOptions(model_backend="anthropic"), PERMISSIVE)
    both = validate_policy({"model_backends": ["vertex", "anthropic"]})
    check_session(AgentOptions(model_backend="anthropic"), both)


def test_check_session_permission_mode(monkeypatch):
    monkeypatch.setenv("MILOS_PROJECT", "p1")
    policy = validate_policy({"permission_modes": {"deny": ["bypassPermissions"]}})
    with pytest.raises(PolicyError, match="permission_mode"):
        check_session(AgentOptions(permission_mode="bypassPermissions"), policy)
    check_session(AgentOptions(permission_mode="default"), policy)


def test_check_session_denied_tool_in_allowlist(monkeypatch):
    monkeypatch.setenv("MILOS_PROJECT", "p1")
    policy = validate_policy({"tools": {"deny": ["WebSearch"]}})
    with pytest.raises(PolicyError, match="denies tool"):
        check_session(AgentOptions(allowed_tools=["WebSearch"]), policy)


def test_check_session_clamps_budgets(monkeypatch):
    monkeypatch.setenv("MILOS_PROJECT", "p1")
    policy = validate_policy({"budgets": {"max_budget_usd": 5.0, "max_turns": 10}})
    options = AgentOptions(max_budget_usd=50.0)  # over the ceiling -> clamped
    check_session(options, policy)
    assert options.max_budget_usd == 5.0
    assert options.max_turns == 10  # absent -> injected
    under = AgentOptions(max_budget_usd=1.0, max_turns=3)  # under -> untouched
    check_session(under, policy)
    assert under.max_budget_usd == 1.0
    assert under.max_turns == 3


def test_validate_risk():
    risk = validate_risk(
        {"purpose": "triage", "impact": "low", "owner": "me@x", "review_by": "2027-01-01"}
    )
    assert risk["impact"] == "low"
    with pytest.raises(PolicyError, match="missing"):
        validate_risk({"purpose": "x"})
    with pytest.raises(PolicyError, match="impact"):
        validate_risk({"purpose": "x", "impact": "huge", "owner": "o", "review_by": "2027-01-01"})
    with pytest.raises(PolicyError, match="unknown"):
        validate_risk({"purpose": "x", "impact": "low", "owner": "o", "review_by": "d", "extra": 1})
