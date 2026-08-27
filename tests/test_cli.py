import json
import time

import pytest

import milos.cli
from milos.cli import main
from milos.policy import canonical_hash, validate_policy

from .fakes import FakeBucket, FakeGcsBucket, FakeStore


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(milos.cli, "Store", lambda project: fake)
    monkeypatch.setenv("MILOS_PROJECT", "proj-1")
    return fake


@pytest.fixture
def bucket(monkeypatch):
    fake = FakeBucket()
    import milos.evidence

    monkeypatch.setattr(milos.evidence, "default_bucket", lambda project: fake)
    return fake


def run_cli(*argv):
    import sys

    old = sys.argv
    sys.argv = ["milos", *argv]
    try:
        main()
    finally:
        sys.argv = old


async def seed_policy(store, doc=None):
    policy = validate_policy(doc or {})
    return await store.create_policy(policy, {"hash": canonical_hash(policy)})


def test_sessions_list(store, capsys):
    import asyncio

    asyncio.run(store.create_session("sess_1", {}, policy_version="v000001"))
    run_cli("sessions")
    out = capsys.readouterr().out
    assert "sess_1" in out and "v000001" in out


def test_sessions_show_and_delete(store, capsys):
    import asyncio

    asyncio.run(store.create_session("sess_1", {"model": "m"}))
    run_cli("sessions", "show", "sess_1")
    assert json.loads(capsys.readouterr().out)["options"] == {"model": "m"}
    run_cli("sessions", "delete", "sess_1")
    assert store.sessions == {}


def test_sessions_purge_respects_age_and_liveness(store, capsys):
    import asyncio

    async def seed():
        await store.create_session("sess_old", {})
        store.sessions["sess_old"]["created_at"] = time.time() - 40 * 86400
        await store.create_session("sess_live", {})
        store.sessions["sess_live"]["created_at"] = time.time() - 40 * 86400
        await store.claim_session("sess_live", "lease", 60)
        await store.create_session("sess_new", {})

    asyncio.run(seed())
    run_cli("sessions", "purge", "--older-than", "30", "--dry-run")
    out = capsys.readouterr().out
    assert "would delete sess_old" in out and "sess_live" not in out
    assert set(store.sessions) == {"sess_old", "sess_live", "sess_new"}

    run_cli("sessions", "purge", "--older-than", "30")
    assert set(store.sessions) == {"sess_live", "sess_new"}


def test_approvals_allow_and_global_list(store, capsys):
    import asyncio

    async def seed():
        await store.create_session("sess_1", {})
        await store.request_approval("sess_1", "h1", "Bash", {"command": "x"}, reason="shell")

    asyncio.run(seed())
    run_cli("approvals")
    out = capsys.readouterr().out
    assert "sess_1" in out and "h1" in out and "(shell)" in out

    run_cli("approvals", "sess_1", "allow", "h1")
    assert store.approvals["sess_1"]["h1"]["status"] == "allow"


def test_kill(store, capsys):
    import asyncio

    asyncio.run(store.create_session("sess_1", {}))
    run_cli("kill", "sess_1")
    assert store.sessions["sess_1"]["disabled"] is True


def test_agents_create_with_risk_and_list_flags_overdue(store, capsys):
    run_cli(
        "agents",
        "create",
        "reviewer",
        "--model",
        "claude-sonnet-5",
        "--risk-purpose",
        "review",
        "--risk-impact",
        "low",
        "--risk-owner",
        "me@x",
        "--risk-review-by",
        "2020-01-01",
    )
    assert store.agents["reviewer"]["risk"]["impact"] == "low"
    run_cli("agents")
    out = capsys.readouterr().out
    assert "REVIEW OVERDUE" in out


def test_agents_revisions(store, capsys):
    run_cli("agents", "create", "a1", "--model", "m1")
    run_cli("agents", "update", "a1", "--model", "m2")
    run_cli("agents", "revisions", "a1")
    out = capsys.readouterr().out
    assert "m1" in out


def test_policies_apply_show_diff_list(store, capsys, tmp_path):
    first = tmp_path / "p1.yaml"
    first.write_text("tools:\n  deny: []\n")
    second = tmp_path / "p2.yaml"
    second.write_text("tools:\n  deny: [WebSearch]\n")
    run_cli("policies", "apply", str(first))
    run_cli("policies", "apply", str(second))
    capsys.readouterr()

    run_cli("policies")
    out = capsys.readouterr().out
    assert "v000001" in out and "v000002  " in out and out.count("* active") == 1

    run_cli("policies", "show")
    assert json.loads(capsys.readouterr().out)["version"] == "v000002"

    run_cli("policies", "diff", "v000001", "v000002")
    out = capsys.readouterr().out
    assert '+      "WebSearch"' in out


def test_policies_apply_rejects_bad_yaml(store, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("modelz: {}\n")
    with pytest.raises(SystemExit, match="unknown policy key"):
        run_cli("policies", "apply", str(bad))


def test_incidents_open_close_list(store, capsys):
    import asyncio

    asyncio.run(store.create_session("sess_1", {}))
    run_cli("incidents", "open", "sess_1", "--reason", "weird", "--severity", "high")
    (incident_id,) = store.incidents
    run_cli("incidents")
    out = capsys.readouterr().out
    assert "open" in out and "weird" in out
    run_cli("incidents", "close", incident_id, "--resolution", "fine")
    assert store.incidents[incident_id]["status"] == "closed"


def test_evidence_export_and_verify(store, bucket, capsys):
    import asyncio

    async def seed():
        await seed_policy(store)
        await store.create_session("sess_1", {})

    asyncio.run(seed())
    run_cli("evidence", "export", "--from", "2020-01-01", "--to", "2099-01-01")
    out = capsys.readouterr().out
    assert "exported gs://proj-1-milos-evidence/exports/" in out
    (export_id,) = {name.split("/")[1] for name in bucket.blobs}
    run_cli("evidence", "verify", export_id)
    assert "verified" in capsys.readouterr().out


def test_settings_show_reports_active_policy(store, capsys):
    import asyncio

    asyncio.run(seed_policy(store))
    run_cli("settings", "update", "--model", "claude-sonnet-5")
    capsys.readouterr()
    run_cli("settings")
    out = capsys.readouterr().out
    assert "claude-sonnet-5" in out
    assert "active policy: v000001" in out


@pytest.fixture
def gcs(monkeypatch):
    from milos import skills, state

    fake = FakeGcsBucket()
    # both namespaces: write/read paths resolve _bucket inside state, while
    # skills.stats/files/push-prune use the from-imported copy in skills
    monkeypatch.setattr(state, "_bucket", lambda project, bucket_name: fake)
    monkeypatch.setattr(skills, "_bucket", lambda project, bucket_name: fake)
    return fake


def test_workspaces_create_list_show_delete(store, capsys):
    run_cli("workspaces", "create", "shared", "--model", "claude-sonnet-5")
    run_cli("agents", "create", "writer", "--workspace", "shared")
    capsys.readouterr()

    run_cli("workspaces")
    out = capsys.readouterr().out
    assert "shared" in out and "free" in out and "writer" in out

    run_cli("workspaces", "show", "shared")
    shown = json.loads(capsys.readouterr().out)
    assert shown["members"] == ["writer"]

    run_cli("workspaces", "delete", "shared")
    assert store.workspaces == {}


def test_workspaces_list_shows_busy_holder(store, capsys):
    import asyncio

    run_cli("workspaces", "create", "shared")
    asyncio.run(store.claim_workspace("shared", "sess_1", 60))
    run_cli("workspaces")
    out = capsys.readouterr().out
    assert "busy" in out and "sess_1" in out


def test_workspaces_claude_md_round_trip(store, gcs, capsys, tmp_path):
    source = tmp_path / "CLAUDE.md"
    source.write_text("# rules\n")
    run_cli("workspaces", "claude-md", "shared", "--file", str(source))
    capsys.readouterr()
    run_cli("workspaces", "claude-md", "shared")
    assert capsys.readouterr().out == "# rules\n"


def test_skills_push_list_files_cat(store, gcs, capsys, tmp_path):
    path = tmp_path / "pdf"
    path.mkdir()
    (path / "SKILL.md").write_bytes(b"---\ndescription: Merge PDFs\n---\n# pdf")
    (path / "notes.md").write_bytes(b"hi")

    run_cli("skills", "push", str(path))
    assert "pushed 2 file(s)" in capsys.readouterr().out
    assert "skills/pdf/SKILL.md" in gcs.objects

    run_cli("skills")
    out = capsys.readouterr().out
    assert "pdf" in out and "Merge PDFs" in out

    run_cli("skills", "files", "pdf")
    out = capsys.readouterr().out
    assert "SKILL.md" in out and "notes.md" in out

    run_cli("skills", "cat", "pdf", "notes.md")
    assert capsys.readouterr().out == "hi"


def test_skills_sync_rejects_workspace(store):
    with pytest.raises(SystemExit, match="workspace"):
        run_cli("skills", "sync", "--workspace", "ws")


def test_evidence_includes_workspaces(store, bucket, capsys):
    import asyncio

    async def seed():
        await seed_policy(store)
        await store.create_workspace("shared", {"options": {}})

    asyncio.run(seed())
    run_cli("evidence", "export", "--from", "2020-01-01", "--to", "2099-01-01")
    out = capsys.readouterr().out
    assert "workspaces.jsonl" in out
    name = next(n for n in bucket.blobs if n.endswith("workspaces.jsonl"))
    assert b"shared" in bucket.blobs[name]
