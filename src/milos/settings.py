"""Process configuration, read once from the environment.

The API, the runner and the connector share this module; each reads only the
fields it needs. Terraform sets these on the Cloud Run resources, so the
variable names below are the contract with `infra/`.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Self

MCP_PATH = "/mcp"  # where a connector mounts its MCP transport; the runner appends it to the service URL


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"{name} is not set")
    return value


def _connector_urls() -> dict[str, str]:
    urls: dict[str, str] = json.loads(os.environ.get("MILOS_CONNECTOR_URLS", "{}"))
    return urls


@dataclass(frozen=True, slots=True, kw_only=True)
class ApiSettings:
    project: str
    token_key: str
    region: str = "asia-northeast1"
    api_role: str = "public"  # "public" (behind IAP) or "internal" (runner, scheduler, connector)
    iap_audience: str | None = None
    runner_job_prefix: str = "milos-runner"  # jobs are named <prefix>-<agent_id>
    internal_url: str = ""  # what runners are told to call
    snapshot_bucket: str = ""
    connector_urls: dict[str, str] = field(default_factory=dict)
    vertex_region: str = "us-east5"
    admin_group: str | None = None  # members may publish, enable and disable definitions
    scheduler_sa: str | None = None  # the only identity that may create scheduled sessions and run inspection
    dev_user: str | None = None  # local development only: trust this email without IAP

    @classmethod
    def from_env(cls) -> Self:
        return cls(
            project=_env("MILOS_PROJECT"),
            token_key=_env("MILOS_TOKEN_KEY"),
            region=_env("MILOS_REGION", "asia-northeast1"),
            api_role=_env("MILOS_API_ROLE", "public"),
            iap_audience=os.environ.get("MILOS_IAP_AUDIENCE"),
            runner_job_prefix=_env("MILOS_RUNNER_JOB_PREFIX", "milos-runner"),
            internal_url=_env("MILOS_INTERNAL_URL", ""),
            snapshot_bucket=_env("MILOS_SNAPSHOT_BUCKET", ""),
            connector_urls=_connector_urls(),
            vertex_region=_env("MILOS_VERTEX_REGION", "us-east5"),
            admin_group=os.environ.get("MILOS_ADMIN_GROUP"),
            scheduler_sa=os.environ.get("MILOS_SCHEDULER_SA"),
            dev_user=os.environ.get("MILOS_DEV_USER"),
        )

    def runner_env(self) -> dict[str, str]:
        """Environment every runner execution receives, on top of the per-session tokens."""
        return {
            "MILOS_INTERNAL_API_URL": self.internal_url,
            "MILOS_PROJECT": self.project,
            "MILOS_SNAPSHOT_BUCKET": self.snapshot_bucket,
            "MILOS_CONNECTOR_URLS": json.dumps(self.connector_urls),
            "MILOS_VERTEX_REGION": self.vertex_region,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RunnerSettings:
    session_id: str
    session_token: str
    lease_token: str
    api_url: str
    project: str
    runner_id: str = "local"
    snapshot_bucket: str = ""
    connector_urls: dict[str, str] = field(default_factory=dict)
    vertex_region: str = "us-east5"
    # Development only: call the Anthropic API directly instead of Vertex AI. The
    # sandbox network must then allow HTTPS egress (infra: direct_anthropic_api).
    anthropic_api_key: str | None = None
    work_dir: str = "/work"
    idle_seconds: float = 30  # how long to wait for more input before exiting
    poll_seconds: float = 2

    @classmethod
    def from_env(cls) -> Self:
        return cls(
            session_id=_env("MILOS_SESSION_ID"),
            session_token=_env("MILOS_SESSION_TOKEN"),
            lease_token=_env("MILOS_LEASE_TOKEN"),
            api_url=_env("MILOS_INTERNAL_API_URL"),
            project=_env("MILOS_PROJECT"),
            runner_id=_env("MILOS_RUNNER_ID", "local"),
            snapshot_bucket=_env("MILOS_SNAPSHOT_BUCKET", ""),
            connector_urls=_connector_urls(),
            vertex_region=_env("MILOS_VERTEX_REGION", "us-east5"),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
            work_dir=_env("MILOS_WORK_DIR", "/work"),
            idle_seconds=float(_env("MILOS_IDLE_SECONDS", "30")),
            poll_seconds=float(_env("MILOS_POLL_SECONDS", "2")),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectorSettings:
    api_url: str  # the internal API, for permission checks
    data_bucket: str | None = None  # internal connector only: the approved data bucket
    data_project: str | None = None  # internal connector only: the data project whose BigQuery datasets it reaches
    workspace_service_accounts: dict[str, str] = field(default_factory=dict)  # agent id -> identity impersonated for BigQuery

    @classmethod
    def from_env(cls) -> Self:
        accounts: dict[str, str] = json.loads(os.environ.get("MILOS_WORKSPACE_SAS", "{}"))
        return cls(
            api_url=_env("MILOS_INTERNAL_API_URL"),
            data_bucket=os.environ.get("MILOS_DATA_BUCKET"),
            data_project=os.environ.get("MILOS_DATA_PROJECT"),
            workspace_service_accounts=accounts,
        )
