"""Environment resolution — the single home for milos env-var defaults.

Every MILOS_* lookup lives here so a default is defined exactly once. Callers
keep their own failure modes: find_project returns None and each caller decides
whether that is an OptionsError (SDK), a SystemExit (CLI), or "no Vertex
routing" (model_env).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_APPROVAL_TIMEOUT = 300.0


def find_project(explicit: str | None = None) -> str | None:
    return explicit or os.environ.get("MILOS_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")


def default_bucket(explicit: str | None, project: str) -> str:
    return explicit or os.environ.get("MILOS_BUCKET") or f"{project}-milos"


def default_evidence_bucket(explicit: str | None, project: str) -> str:
    """The evidence bucket is separate from the working bucket on purpose: it
    carries the (lockable) retention policy, and locking retention on the
    bucket that also holds mutable session state would brick checkpoints."""
    return explicit or os.environ.get("MILOS_EVIDENCE_BUCKET") or f"{project}-milos-evidence"


def approval_timeout() -> float:
    return float(os.environ.get("MILOS_APPROVAL_TIMEOUT") or DEFAULT_APPROVAL_TIMEOUT)


@dataclass(frozen=True)
class RunnerEnv:
    """The sandbox runner's deployment-scoped configuration."""

    project: str
    bucket: str
    stay_alive: float
    lease_ttl: float
    heartbeat: float
    work_dir: Path

    @classmethod
    def from_env(cls) -> RunnerEnv:
        project = find_project()
        if not project:
            raise SystemExit("runner requires $MILOS_PROJECT")
        # The lease is short and heartbeat-renewed (default every ttl/3), so a
        # dead runner is detected within minutes instead of masquerading as
        # "running" for the rest of a long ttl.
        lease_ttl = float(os.environ.get("MILOS_LEASE_TTL") or 180)
        return cls(
            project=project,
            bucket=default_bucket(None, project),
            stay_alive=float(os.environ.get("MILOS_STAY_ALIVE") or 60),
            lease_ttl=lease_ttl,
            heartbeat=float(os.environ.get("MILOS_HEARTBEAT") or lease_ttl / 3),
            work_dir=Path(os.environ.get("MILOS_WORK_DIR") or "/work"),
        )
