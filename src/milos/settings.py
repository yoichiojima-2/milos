"""Process configuration, read once from the environment.

The API, the runner and the connector share this module; each reads only the
fields it needs. Terraform sets these on the Cloud Run resources.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"{name} is not set")
    return value


@dataclass(frozen=True)
class ApiSettings:
    project: str
    region: str
    role: str  # "public" (behind IAP) or "internal" (runner, scheduler, connector)
    token_key: str
    iap_audience: str | None
    runner_job_prefix: str  # jobs are named <prefix>-<agent_id>
    internal_url: str  # what runners are told to call
    snapshot_bucket: str
    connector_urls: dict[str, str] = field(default_factory=dict)
    vertex_region: str = "us-east5"
    dev_user: str | None = None  # local development only: trust this email without IAP

    @classmethod
    def from_env(cls) -> ApiSettings:
        return cls(
            project=_env("MILOS_PROJECT"),
            region=_env("MILOS_REGION", "asia-northeast1"),
            role=_env("MILOS_API_ROLE", "public"),
            token_key=_env("MILOS_TOKEN_KEY"),
            iap_audience=os.environ.get("MILOS_IAP_AUDIENCE"),
            runner_job_prefix=_env("MILOS_RUNNER_JOB_PREFIX", "milos-runner"),
            internal_url=_env("MILOS_INTERNAL_URL", ""),
            snapshot_bucket=_env("MILOS_SNAPSHOT_BUCKET", ""),
            connector_urls=json.loads(os.environ.get("MILOS_CONNECTOR_URLS", "{}")),
            vertex_region=_env("MILOS_VERTEX_REGION", "us-east5"),
            dev_user=os.environ.get("MILOS_DEV_USER"),
        )

    def runner_env(self) -> dict[str, str]:
        """Environment every runner execution receives, on top of the per-session tokens."""
        return {
            "MILOS_API_URL": self.internal_url,
            "MILOS_PROJECT": self.project,
            "MILOS_SNAPSHOT_BUCKET": self.snapshot_bucket,
            "MILOS_CONNECTOR_URLS": json.dumps(self.connector_urls),
            "MILOS_VERTEX_REGION": self.vertex_region,
        }


@dataclass(frozen=True)
class RunnerSettings:
    session_id: str
    session_token: str
    lease_token: str
    runner_id: str
    api_url: str
    project: str
    snapshot_bucket: str
    connector_urls: dict[str, str]
    vertex_region: str
    work_dir: str
    idle_seconds: float  # how long to wait for more input before exiting
    poll_seconds: float

    @classmethod
    def from_env(cls) -> RunnerSettings:
        return cls(
            session_id=_env("MILOS_SESSION_ID"),
            session_token=_env("MILOS_SESSION_TOKEN"),
            lease_token=_env("MILOS_LEASE_TOKEN"),
            runner_id=_env("MILOS_RUNNER_ID", "local"),
            api_url=_env("MILOS_API_URL"),
            project=_env("MILOS_PROJECT"),
            snapshot_bucket=_env("MILOS_SNAPSHOT_BUCKET", ""),
            connector_urls=json.loads(os.environ.get("MILOS_CONNECTOR_URLS", "{}")),
            vertex_region=_env("MILOS_VERTEX_REGION", "us-east5"),
            work_dir=_env("MILOS_WORK_DIR", "/work"),
            idle_seconds=float(_env("MILOS_IDLE_SECONDS", "30")),
            poll_seconds=float(_env("MILOS_POLL_SECONDS", "2")),
        )
