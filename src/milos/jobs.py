"""Starting a runner: one Cloud Run Job execution per session run.

Every agent has its own job (`<prefix>-<agent_id>`) because a job's service
account is fixed in its template, and the design gives each agent a dedicated
runner identity. The job's retry count is zero (set in Terraform); a run that
dies is restarted by the inspection pass with a fresh lease, never by Cloud
Run itself, so an execution with side effects is never silently duplicated.
"""

from __future__ import annotations

from typing import Protocol


class JobLauncher(Protocol):
    async def launch(self, session_id: str, *, agent_id: str, env: dict[str, str]) -> str:
        """Start one execution of the agent's job and return its name."""
        ...


class CloudRunJobs:
    def __init__(self, project: str, region: str, prefix: str = "milos-runner") -> None:
        self._parent = f"projects/{project}/locations/{region}/jobs"
        self._prefix = prefix

    def job_name(self, agent_id: str) -> str:
        return f"{self._parent}/{self._prefix}-{agent_id}"

    async def launch(self, session_id: str, *, agent_id: str, env: dict[str, str]) -> str:
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
        operation = await client.run_job(
            run_v2.RunJobRequest(name=self.job_name(agent_id), overrides=overrides)
        )
        # The operation resolves when the execution finishes; we only need
        # its name, which is available immediately.
        return operation.metadata.name if operation.metadata else session_id


class NoJobs:
    """For the API running locally without Cloud Run: sessions are created but never run."""

    async def launch(self, session_id: str, *, agent_id: str, env: dict[str, str]) -> str:
        return f"local-{session_id}"
