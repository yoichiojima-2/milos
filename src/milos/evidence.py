"""Evidence export — the bundle an ISO auditor asks for, as one command.

`milos evidence export` snapshots the control plane for a time range into the
evidence bucket:

    exports/{export_id}/
        manifest.json     export id, range, who generated it, milos version,
                          per-file {sha256, bytes, records}, bundle_hash
        sessions.jsonl    session docs in range (options snapshot, policy stamp)
        events.jsonl      the full journal per session — messages, the
                          tool-call audit trail, approvals, lifecycle
        approvals.jsonl   the operational approval queue (who decided, when)
        policies.jsonl    every policy version, full content + hash
        agents.jsonl      agent docs with risk blocks + revision history
        workspaces.jsonl  workspace docs (stored defaults + lease state)
        incidents.jsonl   AI incident records

Integrity is hashes, not signatures (signing is deliberately deferred):
`bundle_hash` is a sha256 over the per-file sha256s, and `milos evidence
verify` recomputes everything — an auditor can re-run it. Immutability is the
bucket's, not Firestore's: the evidence bucket is versioned,
and carries a lockable retention policy (infra/evidence.tf), so a bundle, once
written, cannot be altered or deleted until retention expires. Firestore is
the working copy; the locked bucket is the record.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import secrets
import time
from typing import Any, Protocol

from . import env, layout
from .errors import MilosError
from .store import StoreProtocol

FILES = (
    "sessions.jsonl",
    "events.jsonl",
    "approvals.jsonl",
    "policies.jsonl",
    "agents.jsonl",
    "workspaces.jsonl",
    "incidents.jsonl",
)


class BucketProtocol(Protocol):
    """The two operations evidence needs from a bucket; tests use a dict fake."""

    def upload(self, name: str, data: bytes) -> None: ...
    def download(self, name: str) -> bytes: ...


class Bucket:
    """GCS-backed BucketProtocol."""

    def __init__(self, project: str, bucket_name: str) -> None:
        from google.cloud import storage

        self._bucket = storage.Client(project=project).bucket(bucket_name)

    def upload(self, name: str, data: bytes) -> None:
        self._bucket.blob(name).upload_from_string(data, content_type="application/json")

    def download(self, name: str) -> bytes:
        return self._bucket.blob(name).download_as_bytes()


def new_export_id(now: float | None = None) -> str:
    day = datetime.datetime.fromtimestamp(now or time.time(), tz=datetime.UTC)
    return f"exp_{day:%Y%m%d}_{secrets.token_hex(4)}"


def _epoch(value: Any) -> float:
    """Timestamps come back as floats from the fake and datetimes from
    Firestore; comparisons happen in epoch seconds either way."""
    if isinstance(value, datetime.datetime):
        return value.timestamp()
    return float(value or 0)


def _in_range(doc: dict[str, Any], start: float, end: float) -> bool:
    return start <= _epoch(doc.get("created_at")) < end


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(row, sort_keys=True, default=str) + "\n" for row in rows).encode()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bundle_hash(file_hashes: dict[str, str]) -> str:
    """One hash over the sorted per-file hashes — what verify() recomputes."""
    canonical = json.dumps(dict(sorted(file_hashes.items())), separators=(",", ":"))
    return _sha256(canonical.encode())


async def collect(
    store: StoreProtocol,
    *,
    start: float,
    end: float,
    session_id: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Gather every collection for the range as JSON-safe rows.

    Sessions filter by creation time (or one explicit id); their journals and
    approval queues ride along whole — a partial transcript is worse evidence
    than a long one. Policies, agents, and revisions are small and export in
    full: the auditor needs the versions that were active *around* the range,
    and shipping all of them is simpler than proving which those were.
    """
    if session_id:
        session = await store.get_session(session_id)
        if session is None:
            raise MilosError(f"session {session_id} not found")
        sessions = [{"id": session_id, **session}]
    else:
        sessions = [s for s in await store.list_sessions(limit=None) if _in_range(s, start, end)]
    events: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []
    for session in sessions:
        sid = session["id"]
        for branch in session.get("branches") or {"main": {}}:
            events.extend(
                {"session_id": sid, **e}
                for e in await store.list_events(sid, branch, after=0, limit=1_000_000)
            )
        approvals.extend({"session_id": sid, **a} for a in await store.list_approvals(sid))
    agents = []
    for agent in await store.list_agents():
        revisions = await store.list_agent_revisions(agent["name"])
        agents.append({**agent, "revisions": revisions})
    incidents = [i for i in await store.list_incidents() if _in_range(i, start, end)]
    return {
        "sessions.jsonl": sessions,
        "events.jsonl": events,
        "approvals.jsonl": approvals,
        "policies.jsonl": await store.list_policies(),
        "agents.jsonl": agents,
        "workspaces.jsonl": await store.list_workspaces(),
        "incidents.jsonl": incidents,
    }


async def export(
    store: StoreProtocol,
    bucket: BucketProtocol,
    *,
    start: float,
    end: float,
    session_id: str | None = None,
    generated_by: str | None = None,
) -> dict[str, Any]:
    """Write one evidence bundle; returns the manifest (already uploaded)."""
    export_id = new_export_id()
    prefix = layout.export_prefix(export_id)
    rows = await collect(store, start=start, end=end, session_id=session_id)
    files: dict[str, dict[str, Any]] = {}
    for name in FILES:
        data = _jsonl(rows[name])
        bucket.upload(prefix + name, data)
        files[name] = {"sha256": _sha256(data), "bytes": len(data), "records": len(rows[name])}
    manifest = {
        "export_id": export_id,
        "range": {"start": start, "end": end},
        "session_id": session_id,
        "generated_by": generated_by,
        "generated_at": time.time(),
        "milos_version": _version(),
        "files": files,
        "bundle_hash": bundle_hash({name: meta["sha256"] for name, meta in files.items()}),
    }
    bucket.upload(prefix + "manifest.json", json.dumps(manifest, indent=2).encode())
    return manifest


def verify(bucket: BucketProtocol, export_id: str) -> dict[str, Any]:
    """Re-hash a bundle against its manifest; raises MilosError on any
    mismatch. Auditor-runnable: needs only read access on the bucket."""
    prefix = layout.export_prefix(export_id)
    manifest = json.loads(bucket.download(prefix + "manifest.json"))
    failures = []
    hashes: dict[str, str] = {}
    for name, meta in manifest.get("files", {}).items():
        data = bucket.download(prefix + name)
        actual = _sha256(data)
        hashes[name] = actual
        if actual != meta.get("sha256"):
            failures.append(f"{name}: sha256 {actual} != manifest {meta.get('sha256')}")
        if len(data) != meta.get("bytes"):
            failures.append(f"{name}: {len(data)} bytes != manifest {meta.get('bytes')}")
    if bundle_hash(hashes) != manifest.get("bundle_hash"):
        failures.append("bundle_hash mismatch")
    if failures:
        raise MilosError(f"evidence bundle {export_id} failed verification: " + "; ".join(failures))
    return manifest


def default_bucket(project: str) -> Bucket:
    return Bucket(project, env.default_evidence_bucket(None, project))


def _version() -> str | None:
    from .journal import milos_version

    return milos_version()
