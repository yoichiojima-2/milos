"""Synchronous audit entries.

The API writes one entry per tool request with `entries.write` and waits for
the call to return before any permission is created. Entries carry hashes and
short, masked summaries only; raw arguments and tool output never reach the
audit log. The log sink routes them to the locked bucket in the logging
project (see `infra/modules/logging`).
"""

from __future__ import annotations

from typing import Any, Protocol

LOG_NAME = "milos-audit"


class AuditLog(Protocol):
    def write(self, entry: dict[str, Any]) -> None: ...


class CloudAuditLog:
    """`google-cloud-logging` writes `log_struct` synchronously unless batched."""

    def __init__(self, project: str, *, log_name: str = LOG_NAME) -> None:
        from google.cloud import logging as cloud_logging

        self._logger = cloud_logging.Client(project=project).logger(log_name)  # type: ignore[no-untyped-call]

    def write(self, entry: dict[str, Any]) -> None:
        labels = {
            "session_id": str(entry.get("session_id", "")),
            "agent_id": str(entry.get("agent_id", "")),
        }
        self._logger.log_struct(entry, severity="NOTICE", labels=labels)


class StderrAuditLog:
    """Local development sink."""

    def write(self, entry: dict[str, Any]) -> None:
        import json
        import sys

        print(json.dumps(entry, default=str), file=sys.stderr)
