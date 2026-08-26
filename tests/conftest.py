import pytest

ENV_VARS = (
    "MILOS_PROJECT",
    "MILOS_REGION",
    "MILOS_BUCKET",
    "MILOS_EVIDENCE_BUCKET",
    "MILOS_JOB",
    "GOOGLE_CLOUD_PROJECT",
    "CLOUD_ML_REGION",
    "MILOS_MODEL_BACKEND",
    "ANTHROPIC_API_KEY",
    "MILOS_APPROVAL_TIMEOUT",
    "MILOS_STAY_ALIVE",
    "MILOS_LEASE_TTL",
    "MILOS_HEARTBEAT",
    "MILOS_WORK_DIR",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class TriggeredJobs(list):
    """The (project, region, job, session_id) tuples fake-triggered in a test."""

    @property
    def session_ids(self) -> list[str]:
        return [session_id for _, _, _, session_id in self]


@pytest.fixture(autouse=True)
def no_job_trigger(monkeypatch):
    """No test may launch a real Cloud Run job; record the attempts instead."""
    import milos.remote

    triggered = TriggeredJobs()

    async def fake_trigger(project, region, job, session_id):
        triggered.append((project, region, job, session_id))

    monkeypatch.setattr(milos.remote, "_trigger_job", fake_trigger)
    return triggered
