from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from milos.auth import SessionTokens
from milos.models import AgentVersion
from milos.service import Service

from .fakes import FakeAuditLog, FakeDirectory, FakeJobs, FakeStore

GCP_ENV = (
    "GOOGLE_CLOUD_PROJECT",
    "FIRESTORE_EMULATOR_HOST",
    "MILOS_PROJECT",
    "MILOS_API_URL",
    "MILOS_SESSION_ID",
    "MILOS_SESSION_TOKEN",
    "MILOS_LEASE_TOKEN",
    "MILOS_TOKEN_KEY",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No test touches real GCP: strip every variable that could point at it."""
    for name in GCP_ENV:
        monkeypatch.delenv(name, raising=False)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def audit() -> FakeAuditLog:
    return FakeAuditLog()


@pytest.fixture
def jobs() -> FakeJobs:
    return FakeJobs()


@pytest.fixture
def directory() -> FakeDirectory:
    return FakeDirectory({"analysts@example.com": ["*@example.com"]})


@pytest.fixture
def tokens() -> SessionTokens:
    return SessionTokens("test-key")


@pytest.fixture
def service(store, audit, jobs, tokens, clock) -> Service:
    return Service(store, audit, jobs, tokens, runner_env={"MILOS_API_URL": "http://api"}, now=clock)


def definition(**overrides) -> AgentVersion:
    base = dict(
        agent_id="analyst",
        version=0,
        definition_sha256="a" * 64,
        purpose="Summarise the weekly numbers",
        owner="owner@example.com",
        allowed_groups=["analysts@example.com"],
        data_classes=["C1"],
        allowed_tools=["Read", "Glob", "Grep", "Bash", "Write", "mcp__egress__*"],
        approval_required=["Bash", "mcp__egress__*"],
        approval_ttl_sec=600,
        max_turns=20,
        max_budget_usd=5.0,
        max_concurrent_sessions=2,
        model="claude-sonnet-5@20260601",
        runner_sa="runner-analyst@runtime.iam.gserviceaccount.com",
        system_prompt="You are a careful analyst.",
        published_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    base.update(overrides)
    return AgentVersion(**base)


@pytest.fixture
async def agent(service):
    return await service.publish(definition())


@pytest.fixture
async def session(service, agent):
    return await service.create_session("analyst", "hello", operator="alice@example.com", client_request_id="req-1")


def emulator_available() -> bool:
    return bool(os.environ.get("FIRESTORE_EMULATOR_HOST"))
