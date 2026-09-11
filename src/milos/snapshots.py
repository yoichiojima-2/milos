"""Session snapshots: the model transcript and the working directory.

Layout in the snapshot bucket: `sessions/{session_id}/snapshots/{n}/state.tar.gz`
plus `manifest.json`. A runner writes `n+1` and only then asks the API to
advance the pointer, so a failed upload leaves the previous snapshot intact.
The transcript (the SDK's own session files under `~/.claude/projects`) is
kept for the model; the event stream in Firestore is the journal people read.
"""

import asyncio
import io
import json
import tarfile
from pathlib import Path
from typing import Any, Protocol

STATE = "state.tar.gz"
MANIFEST = "manifest.json"


class Blobs(Protocol):
    async def put(self, path: str, data: bytes) -> None: ...

    async def get(self, path: str) -> bytes | None: ...


class GcsBlobs:
    def __init__(self, bucket: str, *, project: str | None = None) -> None:
        # Deferred: google-cloud-storage is only needed on Cloud Run.
        from google.cloud import storage  # type: ignore[attr-defined]

        self._bucket = storage.Client(project=project).bucket(bucket)

    async def put(self, path: str, data: bytes) -> None:
        await asyncio.to_thread(self._bucket.blob(path).upload_from_string, data)

    async def get(self, path: str) -> bytes | None:
        from google.api_core.exceptions import NotFound

        try:
            return await asyncio.to_thread(self._bucket.blob(path).download_as_bytes)
        except NotFound:
            return None


def _path(session_id: str, number: int, name: str) -> str:
    return f"sessions/{session_id}/snapshots/{number}/{name}"


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
    await blobs.put(_path(session_id, number, STATE), buffer.getvalue())
    await blobs.put(_path(session_id, number, MANIFEST), json.dumps(manifest).encode())


async def restore(blobs: Blobs, session_id: str, number: int, *, work_dir: Path, transcripts: Path) -> dict[str, Any] | None:
    """Unpack snapshot `number`; returns its manifest, or None when it does not exist."""
    raw = await blobs.get(_path(session_id, number, MANIFEST))
    archive = await blobs.get(_path(session_id, number, STATE))
    if raw is None or archive is None:
        return None
    targets = {"work": work_dir, "transcripts": transcripts}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            top, _, rest = member.name.partition("/")
            if (target := targets.get(top)) is None or not rest:
                continue
            member.name = rest
            tar.extract(member, path=target, filter="data")
    manifest: dict[str, Any] = json.loads(raw)
    return manifest
