"""Session snapshots: the model transcript and the working directory.

Layout in the snapshot bucket: `sessions/{session_id}/snapshots/{n}/state.tar.gz`
plus `manifest.json`. A runner writes `n+1` and only then asks the API to
advance the pointer, so a failed upload leaves the previous snapshot intact.
The transcript (the SDK's own session files under `~/.claude/projects`) is
kept for the model; the event stream in Firestore is the journal people read.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any, Protocol


class Blobs(Protocol):
    async def put(self, path: str, data: bytes) -> None: ...

    async def get(self, path: str) -> bytes | None: ...


class GcsBlobs:
    def __init__(self, bucket: str, *, project: str | None = None) -> None:
        from google.cloud import storage  # type: ignore[attr-defined]

        self._bucket = storage.Client(project=project).bucket(bucket)

    async def put(self, path: str, data: bytes) -> None:
        import asyncio

        await asyncio.to_thread(self._bucket.blob(path).upload_from_string, data)

    async def get(self, path: str) -> bytes | None:
        import asyncio

        from google.api_core.exceptions import NotFound

        try:
            return await asyncio.to_thread(self._bucket.blob(path).download_as_bytes)
        except NotFound:
            return None


def prefix(session_id: str, number: int) -> str:
    return f"sessions/{session_id}/snapshots/{number}/"


async def save(
    blobs: Blobs,
    session_id: str,
    number: int,
    *,
    work_dir: Path,
    transcripts: Path,
    manifest: dict[str, Any],
) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        if work_dir.exists():
            tar.add(work_dir, arcname="work")
        if transcripts.exists():
            tar.add(transcripts, arcname="transcripts")
    await blobs.put(prefix(session_id, number) + "state.tar.gz", buffer.getvalue())
    await blobs.put(prefix(session_id, number) + "manifest.json", json.dumps(manifest).encode())


async def restore(blobs: Blobs, session_id: str, number: int, *, work_dir: Path, transcripts: Path) -> dict[str, Any] | None:
    """Unpack snapshot `number`; returns its manifest, or None when it does not exist."""
    raw = await blobs.get(prefix(session_id, number) + "manifest.json")
    archive = await blobs.get(prefix(session_id, number) + "state.tar.gz")
    if raw is None or archive is None:
        return None
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            top, _, rest = member.name.partition("/")
            target = {"work": work_dir, "transcripts": transcripts}.get(top)
            if target is None or not rest:
                continue
            member.name = rest
            tar.extract(member, path=target, filter="data")
    manifest: dict[str, Any] = json.loads(raw)
    return manifest
