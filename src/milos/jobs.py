"""Starting a runner: one Cloud Run Job execution per session run.

The job's retry count is zero (set in Terraform); a run that dies is restarted
by the inspection pass with a fresh lease, never by Cloud Run itself, so an
execution with side effects is never silently duplicated.
"""

from __future__ import annotations

from typing import Protocol


class JobLauncher(Protocol):
    async def launch(self, session_id: str, *, env: dict[str, str], runner_sa: str) -> str:
        """Start one execution and return its name."""
        ...


class CloudRunJobs:
    def __init__(self, project: str, region: str, job: str) -> None:
        self._name = f"projects/{project}/locations/{region}/jobs/{job}"

    async def launch(self, session_id: str, *, env: dict[str, str], runner_sa: str) -> str:
        from google.cloud import run_v2

        client = run_v2.JobsAsyncClient()
        overrides = run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    env=[run_v2.EnvVar(name=k, value=v) for k, v in env.items()]
                )
            ],
            task_count=1,
        )
        operation = await client.run_job(run_v2.RunJobRequest(name=self._name, overrides=overrides))
        # The operation resolves when the execution finishes; we only need
        # its name, which is available immediately.
        return operation.metadata.name if operation.metadata else session_id


class NoJobs:
    """For the API running locally without Cloud Run: sessions are created but never run."""

    async def launch(self, session_id: str, *, env: dict[str, str], runner_sa: str) -> str:
        return f"local-{session_id}"
