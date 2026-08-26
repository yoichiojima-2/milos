"""The GCP transport: sessions in Firestore, execution on the Cloud Run Job.

Full implementation lands with the client/runner port; the module exists first
so the test fixture that stubs _trigger_job has something to patch.
"""

from __future__ import annotations


async def _trigger_job(project: str, region: str, job: str, session_id: str) -> None:
    raise NotImplementedError
