"""AI incident records (ISO 42001) — flag a session, track it to resolution.

An incident is one Firestore document naming the session whose behavior is in
question, why, and how it ended. Opening one also appends a lifecycle record
to the session's journal, so the transcript itself shows the flag inline — and
the evidence export carries both.

    incidents/{inc_...}
        session_id, reason, severity, status (open|closed),
        opened_by, resolution, closed_by
"""

from __future__ import annotations

from typing import Any

from . import journal
from .errors import MilosError
from .options import AgentOptions
from .store import StoreProtocol, new_incident_id, store_or_default as _store

SEVERITIES = ("low", "medium", "high")


async def open_incident(
    session_id: str,
    reason: str,
    *,
    severity: str = "medium",
    opened_by: str | None = None,
    options: AgentOptions | None = None,
    store: StoreProtocol | None = None,
) -> dict[str, Any]:
    if severity not in SEVERITIES:
        raise MilosError(f"severity must be one of {', '.join(SEVERITIES)}")
    if not reason or not reason.strip():
        raise MilosError("an incident needs a reason")
    store = _store(options or AgentOptions(), store)
    session = await store.get_session(session_id)
    if session is None:
        raise MilosError(f"session {session_id} not found")
    incident_id = new_incident_id()
    doc = {
        "session_id": session_id,
        "reason": reason,
        "severity": severity,
        "status": "open",
        "opened_by": opened_by,
        "resolution": None,
        "closed_by": None,
    }
    await store.create_incident(incident_id, doc)
    # The transcript shows the flag inline. Appended at the journal head of
    # the active branch — an incident is operator action, not runner state, so
    # it does not go through a JournalWriter (no run owns the session now).
    branch = journal.active_branch(session)
    seq, tip = await store.recover_head(session_id, branch)
    await store.append_event(
        session_id,
        journal.make_event(
            "lifecycle",
            {"event": "incident_opened", "incident": incident_id, "reason": reason},
            parent_uuid=tip,
            branch=branch,
            seq=seq + 1,
        ),
    )
    return {"id": incident_id, **doc}


async def close_incident(
    incident_id: str,
    resolution: str,
    *,
    closed_by: str | None = None,
    options: AgentOptions | None = None,
    store: StoreProtocol | None = None,
) -> dict[str, Any]:
    if not resolution or not resolution.strip():
        raise MilosError("closing an incident needs a resolution")
    store = _store(options or AgentOptions(), store)
    incident = await store.get_incident(incident_id)
    if incident is None:
        raise MilosError(f"incident {incident_id} not found")
    await store.update_incident(
        incident_id, status="closed", resolution=resolution, closed_by=closed_by
    )
    return {**incident, "status": "closed", "resolution": resolution, "closed_by": closed_by}


async def list_all(
    *, options: AgentOptions | None = None, store: StoreProtocol | None = None
) -> list[dict[str, Any]]:
    incidents = await _store(options or AgentOptions(), store).list_incidents()
    return sorted(incidents, key=lambda i: i.get("id") or "")
