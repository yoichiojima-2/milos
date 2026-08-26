"""Session-state persistence: GCS <-> the sandbox's local state directory.

The sandbox work dir contains ws/ (the agent's working directory) and home/
(HOME for the harness, so claude_agent_sdk transcripts checkpoint too,
enabling resume). Both sync to per-session prefixes from `layout.py`.
Synchronous on purpose — callers wrap in asyncio.to_thread.
"""

from __future__ import annotations

from pathlib import Path


def _bucket(project: str, bucket_name: str):
    from google.cloud import storage

    return storage.Client(project=project).bucket(bucket_name)


def restore(project: str, bucket_name: str, prefix: str, root: Path) -> int:
    """Download everything under the prefix into root; returns the file count.

    A listing is a snapshot, and the blobs it yields carry the generation they
    had when they were listed — downloading one of *those* pins that generation.
    Anything rewritten under the prefix while the restore runs would then 404
    and, at session start, take the whole run down with it. So the download goes
    through a fresh, generation-less handle, and a file that disappears
    mid-restore is skipped instead of fatal.
    """
    from google.api_core.exceptions import NotFound

    bucket = _bucket(project, bucket_name)
    count = 0
    for blob in bucket.list_blobs(prefix=prefix):
        relative = blob.name[len(prefix) :]
        if not relative:
            continue
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            bucket.blob(blob.name).download_to_filename(target)
        except NotFound:
            # Deleted between the listing and now. A failed download can leave
            # the opened file behind, so don't hand the sandbox an empty stub.
            target.unlink(missing_ok=True)
            continue
        count += 1
    return count


def checkpoint(project: str, bucket_name: str, prefix: str, root: Path) -> int:
    """Upload root to the prefix; returns the file count."""
    bucket = _bucket(project, bucket_name)
    count = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        bucket.blob(prefix + str(path.relative_to(root))).upload_from_filename(path)
        count += 1
    return count
