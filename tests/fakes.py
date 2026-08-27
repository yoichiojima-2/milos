"""In-memory fakes for the two backends: Firestore (FakeStore) and GCS
(FakeBucket). Both mirror the protocols in milos.store / milos.evidence, which
is what lets the whole suite run without touching GCP."""

from __future__ import annotations

import secrets
import time
from typing import Any

from milos.errors import SessionExists
from milos.store import RUNTIME_FIELDS, format_policy_version, lease_active


def _set_path(doc: dict[str, Any], key: str, value: Any) -> None:
    """Apply one dotted-path update the way Firestore's update() does."""
    parts = key.split(".")
    for part in parts[:-1]:
        doc = doc.setdefault(part, {})
    doc[parts[-1]] = value


async def append_message(store, session_id, seq, doc, *, branch="main", parent_uuid=None):
    """Test shorthand: journal one message document at a given seq."""
    from milos.journal import make_event

    event = make_event("message", doc, parent_uuid=parent_uuid, branch=branch, seq=seq)
    await store.append_event(session_id, event)
    return event


class FakeStore:
    """Implements milos.store.StoreProtocol (asserted in tests/test_store.py)."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.events: dict[str, dict[str, dict[str, Any]]] = {}  # sid -> uuid -> event
        self.inbox: dict[str, list[dict[str, Any]]] = {}
        self.approvals: dict[str, dict[str, dict[str, Any]]] = {}
        self.workspaces: dict[str, dict[str, Any]] = {}
        self.settings: dict[str, Any] | None = None
        self.agents: dict[str, dict[str, Any]] = {}
        self.agent_revisions: dict[str, list[dict[str, Any]]] = {}
        self.policies: dict[str, dict[str, Any]] = {}
        self.incidents: dict[str, dict[str, Any]] = {}

    async def create_session(
        self,
        session_id,
        options,
        created_by=None,
        trigger="api",
        agent=None,
        policy_version=None,
        policy_hash=None,
    ):
        if session_id in self.sessions:
            raise SessionExists(f"session {session_id} exists")
        self.sessions[session_id] = {
            "options": options,
            "disabled": False,
            "cost_usd": 0.0,
            "claude_session_id": None,
            "branches": {
                "main": {
                    "created_at": time.time(),
                    "base_uuid": None,
                    "base_seq": 0,
                    "claude_session_id": None,
                }
            },
            "active_branch": "main",
            "tip_uuid": None,
            "seq_head": 0,
            "runtime": {
                "status": "queued",
                "stop_reason": None,
                "lease_id": None,
                "lease_expires": 0.0,
                "heartbeat_at": 0.0,
                "triggered_at": 0.0,
            },
            "created_by": created_by,
            "trigger": trigger,
            "agent": agent,
            "policy_version": policy_version,
            "policy_hash": policy_hash,
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    async def get_session(self, session_id):
        session = self.sessions.get(session_id)
        return dict(session) if session else None

    async def update_session(self, session_id, **fields):
        session = self.sessions[session_id]
        for key, value in fields.items():
            if key in RUNTIME_FIELDS:
                key = f"runtime.{key}"
            _set_path(session, key, value)
        session["updated_at"] = time.time()

    async def list_sessions(self, limit=20):
        rows = [{"id": k, **v} for k, v in self.sessions.items()]
        return rows if limit is None else rows[:limit]

    async def delete_session(self, session_id):
        self.sessions.pop(session_id, None)
        self.events.pop(session_id, None)
        self.inbox.pop(session_id, None)
        self.approvals.pop(session_id, None)

    async def mark_starting(self, session_id):
        session = self.sessions.get(session_id)
        if not session or session.get("disabled"):
            return
        status = session["runtime"]["status"]
        if status == "terminated" or (status == "running" and lease_active(session)):
            return
        session["runtime"].update(status="starting", triggered_at=time.time())
        session["updated_at"] = time.time()

    async def claim_session(self, session_id, lease_id, ttl_seconds):
        session = self.sessions.get(session_id)
        if not session or session["runtime"]["status"] == "terminated" or session.get("disabled"):
            return None
        runtime = session["runtime"]
        now = time.time()
        if float(runtime.get("lease_expires") or 0) > now and runtime.get("lease_id") != lease_id:
            return None
        runtime.update(
            status="running",
            lease_id=lease_id,
            lease_expires=now + ttl_seconds,
            heartbeat_at=now,
        )
        return dict(session)

    async def renew_lease(self, session_id, lease_id, ttl_seconds):
        session = self.sessions.get(session_id)
        if not session or session["runtime"]["status"] == "terminated" or session.get("disabled"):
            return False
        if session["runtime"].get("lease_id") != lease_id:
            return False
        now = time.time()
        session["runtime"].update(lease_expires=now + ttl_seconds, heartbeat_at=now)
        return True

    async def release_session(self, session_id, *, status, stop_reason, **fields):
        await self.update_session(
            session_id,
            status=status,
            stop_reason=stop_reason,
            lease_id=None,
            lease_expires=0.0,
            **fields,
        )

    async def create_branch(self, session_id, branch_id, *, base_uuid, base_seq, claude_session_id):
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(f"session {session_id} not found")
        if float(session["runtime"].get("lease_expires") or 0) > time.time():
            raise RuntimeError(f"session {session_id} is running — interrupt it first")
        if branch_id in session["branches"]:
            raise ValueError(f"branch {branch_id} already exists")
        session["branches"][branch_id] = {
            "created_at": time.time(),
            "base_uuid": base_uuid,
            "base_seq": base_seq,
            "claude_session_id": claude_session_id,
        }
        session.update(active_branch=branch_id, tip_uuid=base_uuid, seq_head=base_seq)
        session["updated_at"] = time.time()

    async def append_event(self, session_id, event):
        # Keyed by uuid, like the real store's doc id: idempotent on retry,
        # and a stale-seq rewrite lands as a second record, never an overwrite.
        self.events.setdefault(session_id, {})[event["uuid"]] = {**event, "ts": time.time()}

    async def list_events(self, session_id, branch, after, limit=200):
        rows = [
            e
            for e in self.events.get(session_id, {}).values()
            if e.get("branch") == branch and e["seq"] > after
        ]
        return sorted(rows, key=lambda e: e["seq"])[:limit]

    async def get_event(self, session_id, uuid):
        event = self.events.get(session_id, {}).get(uuid)
        return dict(event) if event else None

    async def recover_head(self, session_id, branch):
        rows = [e for e in self.events.get(session_id, {}).values() if e.get("branch") == branch]
        if not rows:
            return 0, None
        head = max(rows, key=lambda e: e["seq"])
        return int(head["seq"]), head.get("uuid")

    async def push_inbox(self, session_id, kind, text=None):
        message_id = secrets.token_hex(10)
        self.inbox.setdefault(session_id, []).append(
            {"id": message_id, "kind": kind, "text": text, "ts": time.time(), "consumed": False}
        )
        return message_id

    async def peek_messages(self, session_id):
        return [
            {"id": item["id"], "text": item["text"] or "", "ts": item["ts"]}
            for item in self.inbox.get(session_id, [])
            if item["kind"] == "message" and not item["consumed"]
        ]

    async def consume_message(self, session_id, message_id):
        for item in self.inbox.get(session_id, []):
            if item["id"] == message_id:
                item["consumed"] = True

    async def take_interrupt(self, session_id):
        taken = False
        for item in self.inbox.get(session_id, []):
            if item["kind"] == "interrupt" and not item["consumed"]:
                item["consumed"] = True
                taken = True
        return taken

    async def request_approval(
        self, session_id, call_hash, tool_name, tool_input, tool_use_id=None, reason=None
    ):
        self.approvals.setdefault(session_id, {})[call_hash] = {
            "call_hash": call_hash,
            "tool_name": tool_name,
            "input": tool_input,
            "tool_use_id": tool_use_id,
            "reason": reason,
            "status": "pending",
            "deny_message": None,
            "decided_by": None,
            "requested_at": time.time(),
            "decided_at": None,
        }

    async def get_approval(self, session_id, call_hash):
        approval = self.approvals.get(session_id, {}).get(call_hash)
        return dict(approval) if approval else None

    async def decide_approval(self, session_id, call_hash, *, allow, decided_by, deny_message=None):
        self.approvals[session_id][call_hash].update(
            status="allow" if allow else "deny",
            decided_by=decided_by,
            deny_message=deny_message,
            decided_at=time.time(),
        )

    async def list_pending_approvals(self, session_id):
        return [
            dict(a) for a in self.approvals.get(session_id, {}).values() if a["status"] == "pending"
        ]

    async def list_approvals(self, session_id):
        return [dict(a) for a in self.approvals.get(session_id, {}).values()]

    async def list_all_pending_approvals(self):
        return [
            {"session_id": sid, **a}
            for sid, rows in self.approvals.items()
            for a in rows.values()
            if a["status"] == "pending"
        ]

    async def list_tool_calls(self, session_id):
        from milos.store import _tool_call_row

        rows = [e for e in self.events.get(session_id, {}).values() if e.get("type") == "tool_call"]
        return [_tool_call_row(e) for e in sorted(rows, key=lambda e: e["ts"])]

    async def claim_workspace(self, name, session_id, ttl_seconds):
        doc = self.workspaces.get(name)
        if (
            doc
            and float(doc.get("lease_expires") or 0) > time.time()
            and doc.get("lease_session_id") != session_id
        ):
            return False
        self.workspaces.setdefault(name, {}).update(
            lease_session_id=session_id,
            lease_expires=time.time() + ttl_seconds,
        )
        return True

    async def release_workspace(self, name, session_id):
        doc = self.workspaces.get(name)
        if doc and doc.get("lease_session_id") == session_id:
            doc.update(lease_session_id=None, lease_expires=0.0)

    async def create_workspace(self, name, doc):
        if name in self.workspaces:
            raise ValueError(f"workspace {name} exists")
        self.workspaces[name] = {**doc, "created_at": time.time(), "updated_at": time.time()}

    async def get_workspace(self, name):
        doc = self.workspaces.get(name)
        return {"name": name, **doc} if doc else None

    async def update_workspace(self, name, **fields):
        self.workspaces[name].update(fields, updated_at=time.time())

    async def list_workspaces(self):
        return [{"name": k, **v} for k, v in self.workspaces.items()]

    async def delete_workspace(self, name):
        self.workspaces.pop(name, None)

    async def get_settings(self):
        return self.settings

    async def update_settings(self, doc):
        self.settings = dict(doc)

    async def create_agent(self, name, doc):
        if name in self.agents:
            raise ValueError(f"agent {name} exists")
        self.agents[name] = {**doc, "created_at": time.time(), "updated_at": time.time()}

    async def get_agent(self, name):
        agent = self.agents.get(name)
        return {"name": name, **agent} if agent else None

    async def update_agent(self, name, **fields):
        self.agents[name].update(fields, updated_at=time.time())

    async def list_agents(self):
        return [{"name": k, **v} for k, v in self.agents.items()]

    async def delete_agent(self, name):
        self.agents.pop(name, None)
        self.agent_revisions.pop(name, None)

    async def create_agent_revision(self, name, doc):
        revisions = self.agent_revisions.setdefault(name, [])
        revision = len(revisions) + 1
        revisions.append({**doc, "revision": revision, "archived_at": time.time()})
        return revision

    async def list_agent_revisions(self, name):
        return [dict(r) for r in self.agent_revisions.get(name, [])]

    async def create_policy(self, policy, meta):
        settings = self.settings or {}
        seq = int(settings.get("policy_seq") or 0) + 1
        version = format_policy_version(seq)
        self.policies[version] = {
            **meta,
            "version": version,
            "policy": policy,
            "created_at": time.time(),
        }
        self.settings = {**settings, "policy_seq": seq, "policy_version": version}
        return version

    async def get_policy(self, version):
        policy = self.policies.get(version)
        return dict(policy) if policy else None

    async def get_active_policy(self):
        version = (self.settings or {}).get("policy_version")
        return await self.get_policy(version) if version else None

    async def list_policies(self):
        return [dict(self.policies[v]) for v in sorted(self.policies)]

    async def create_incident(self, incident_id, doc):
        if incident_id in self.incidents:
            raise ValueError(f"incident {incident_id} exists")
        self.incidents[incident_id] = {**doc, "created_at": time.time(), "updated_at": time.time()}

    async def get_incident(self, incident_id):
        incident = self.incidents.get(incident_id)
        return {"id": incident_id, **incident} if incident else None

    async def update_incident(self, incident_id, **fields):
        self.incidents[incident_id].update(fields, updated_at=time.time())

    async def list_incidents(self):
        return [{"id": k, **v} for k, v in self.incidents.items()]


class FakeBucket:
    """A name->bytes dict standing in for the evidence bucket: upload and
    download by blob name, which is all evidence.py needs."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def upload(self, name: str, data: bytes) -> None:
        self.blobs[name] = data

    def download(self, name: str) -> bytes:
        return self.blobs[name]

    def list(self, prefix: str) -> list[str]:
        return sorted(name for name in self.blobs if name.startswith(prefix))


class FakeBlob:
    """One blob handle over a FakeGcsBucket record ({"data", and "content_type"
    for string uploads}) — the subset of google.cloud.storage the milos GCS
    modules (state.py, skills.py) touch."""

    def __init__(self, bucket: "FakeGcsBucket", name: str) -> None:
        self._bucket = bucket
        self.name = name
        self.updated = None
        # None until the handle is reloaded (which is what a listing does). A
        # handle that knows a generation reads *that* one, like GCS: the object
        # being rewritten underneath it is a 404, not a fresh download.
        self.generation: int | None = None

    @property
    def size(self) -> int | None:
        record = self._bucket.objects.get(self.name)
        return len(record["data"]) if record else None

    def exists(self) -> bool:
        return self.name in self._bucket.objects

    def reload(self) -> None:
        self.generation = self._bucket.generations.get(self.name)

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        self._bucket.objects[self.name] = {"data": data, "content_type": content_type}
        self._bucket.bump(self.name)

    def upload_from_filename(self, path) -> None:
        from pathlib import Path

        self._bucket.objects[self.name] = {"data": Path(path).read_bytes()}
        self._bucket.bump(self.name)

    def download_as_bytes(self, start: int | None = None, end: int | None = None) -> bytes:
        data = self._bucket.objects[self.name]["data"]
        if start is not None or end is not None:
            # GCS ranges are inclusive of `end`
            return data[start or 0 : None if end is None else end + 1]
        return data

    def download_to_filename(self, path) -> None:
        from pathlib import Path

        from google.api_core.exceptions import NotFound

        current = self._bucket.generations.get(self.name)
        # GCS opens the destination before it can know the read will fail, so a
        # 404 leaves an empty file behind. Mirror it: callers have to clean up.
        Path(path).write_bytes(b"")
        if current is None or (self.generation is not None and self.generation != current):
            raise NotFound(f"404 no such object: {self.name}")
        Path(path).write_bytes(self._bucket.objects[self.name]["data"])

    def delete(self) -> None:
        del self._bucket.objects[self.name]


class FakeListing(list):
    def __init__(self, blobs, prefixes):
        super().__init__(blobs)
        self.prefixes = prefixes


class FakeGcsBucket:
    """In-memory GCS bucket, seedable with {name: data}."""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects: dict[str, dict[str, Any]] = {
            name: {"data": data} for name, data in (objects or {}).items()
        }
        # Generations live beside the objects rather than in them, so a rewrite
        # is visible to a stale handle without changing the record shape tests
        # compare against.
        self.generations: dict[str, int] = dict.fromkeys(self.objects, 1)

    def bump(self, name: str) -> None:
        self.generations[name] = self.generations.get(name, 0) + 1

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)

    def list_blobs(self, prefix: str = "") -> FakeListing:
        blobs = []
        for name in sorted(n for n in self.objects if n.startswith(prefix)):
            blob = FakeBlob(self, name)
            blob.reload()
            blobs.append(blob)
        return FakeListing(blobs, set())
