"""state.py GCS sync + prefix file ops, and layout's prefix rules."""

import pytest

from milos import state
from milos.errors import OptionsError
from milos.layout import (
    session_prefix,
    skill_prefix,
    skills_root,
    workspace_prefix,
    workspace_root,
    workspace_skills_root,
)

from .fakes import FakeGcsBucket


def test_session_prefix():
    assert session_prefix("sess_x", "ws") == "sessions/sess_x/state/ws/"
    assert session_prefix("sess_x", "home") == "sessions/sess_x/state/home/"


def test_workspace_owns_one_prefix_holding_its_files_and_its_skills():
    assert workspace_root("data") == "workspaces/data/"
    assert workspace_prefix("data") == "workspaces/data/ws/"
    assert workspace_skills_root("data") == "workspaces/data/skills/"
    assert skill_prefix("pdf", "data") == "workspaces/data/skills/pdf/"
    # a workspace's skills sit beside its files, never inside the agent's cwd
    assert not skills_root("data").startswith(workspace_prefix("data"))


def test_prefix_builders_reject_bad_names():
    for bad in ("/tmp", "a/b", "../x", "", "Upper", ".", "a" * 65):
        for build in (workspace_root, workspace_prefix, workspace_skills_root):
            with pytest.raises(OptionsError):
                build(bad)
        if bad:  # an empty workspace means "global", the same as None
            with pytest.raises(OptionsError):
                skill_prefix("pdf", bad)


# --- blob-level ops against an in-memory bucket ---


@pytest.fixture
def bucket(monkeypatch):
    fake = FakeGcsBucket(
        {
            "workspaces/workspace/ws/a.md": b"aa",
            "workspaces/workspace/ws/sub/b.md": b"bb",
        }
    )
    monkeypatch.setattr(state, "_bucket", lambda project, bucket_name: fake)
    return fake


def test_restore_reads_the_live_object_not_the_listed_generation(bucket, tmp_path):
    """A listing is a snapshot, and its blobs carry the generation they had when
    they were listed. Downloading through one of those pins that generation, so
    anything rewritten under the prefix mid-restore 404s — which at session
    start would take the whole run down (a skills re-push rewrites many blobs)."""
    listed = bucket.list_blobs

    def list_then_rewrite(prefix=""):
        blobs = listed(prefix=prefix)
        bucket.objects["workspaces/workspace/ws/a.md"]["data"] = b"rewritten"
        bucket.bump("workspaces/workspace/ws/a.md")
        return blobs

    bucket.list_blobs = list_then_rewrite

    count = state.restore("proj", "bkt", "workspaces/workspace/ws/", tmp_path)

    assert count == 2
    assert (tmp_path / "a.md").read_bytes() == b"rewritten"


def test_restore_skips_a_file_deleted_mid_restore(bucket, tmp_path):
    listed = bucket.list_blobs

    def list_then_delete(prefix=""):
        blobs = listed(prefix=prefix)
        del bucket.objects["workspaces/workspace/ws/a.md"]
        del bucket.generations["workspaces/workspace/ws/a.md"]
        return blobs

    bucket.list_blobs = list_then_delete

    count = state.restore("proj", "bkt", "workspaces/workspace/ws/", tmp_path)

    assert count == 1
    assert (tmp_path / "sub" / "b.md").read_bytes() == b"bb"
    # and no empty stub where the vanished file would have been
    assert not (tmp_path / "a.md").exists()


def test_checkpoint_skips_excluded_prefixes(bucket, tmp_path):
    (tmp_path / "a.md").write_bytes(b"rewritten")
    (tmp_path / ".claude" / "skills" / "pdf").mkdir(parents=True)
    (tmp_path / ".claude" / "skills" / "pdf" / "SKILL.md").write_bytes(b"# pdf")

    count = state.checkpoint(
        "proj", "bkt", "workspaces/workspace/ws/", tmp_path, (".claude/skills/",)
    )

    assert count == 1
    assert bucket.objects["workspaces/workspace/ws/a.md"]["data"] == b"rewritten"
    assert not any(".claude" in name for name in bucket.objects)


def test_read_write_delete_in_prefix(bucket):
    state.write_in_prefix("proj", "bkt", "workspaces/workspace/ws/", "file", "new.md", b"n")
    data, content_type = state.read_in_prefix(
        "proj", "bkt", "workspaces/workspace/ws/", "file", "new.md", max_bytes=10
    )
    assert (data, content_type) == (b"n", "text/markdown")
    with pytest.raises(ValueError, match="limit"):
        state.read_in_prefix("proj", "bkt", "workspaces/workspace/ws/", "file", "a.md", max_bytes=1)
    with pytest.raises(FileNotFoundError):
        state.read_in_prefix(
            "proj", "bkt", "workspaces/workspace/ws/", "file", "gone.md", max_bytes=10
        )
    with pytest.raises(OptionsError):
        state.read_in_prefix(
            "proj", "bkt", "workspaces/workspace/ws/", "file", "../escape", max_bytes=10
        )
    state.delete_in_prefix("proj", "bkt", "workspaces/workspace/ws/", "file", "new.md")
    assert "workspaces/workspace/ws/new.md" not in bucket.objects
    with pytest.raises(FileNotFoundError):
        state.delete_in_prefix("proj", "bkt", "workspaces/workspace/ws/", "file", "new.md")
