"""The bucket layout: every GCS prefix milos writes, in one place.

State bucket ({project}-milos):

    sessions/{sid}/state/ws/     a session's working directory
    sessions/{sid}/state/home/   HOME for the harness (transcripts, resume)

Evidence bucket ({project}-milos-evidence — versioned, CMEK, lockable
retention; see infra/evidence.tf):

    exports/{export_id}/         one evidence bundle (manifest.json + *.jsonl)

The prefix builders live here rather than beside their readers so the layout
is one file to read and one file to change.
"""

from __future__ import annotations

SESSIONS = "sessions/"
EXPORTS = "exports/"


def session_prefix(session_id: str, subdir: str) -> str:
    """A session's checkpointed state: subdir is "ws" or "home"."""
    return f"{SESSIONS}{session_id}/state/{subdir}/"


def export_prefix(export_id: str) -> str:
    """One evidence bundle's directory in the evidence bucket."""
    return f"{EXPORTS}{export_id}/"
