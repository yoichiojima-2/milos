"""The command line: what an operator and an approver see, and that failures are one line."""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest
from httpx import ASGITransport

from milos import cli
from milos.api import create_app
from milos.auth import IAP_HEADER
from milos.client import Client
from milos.models import StopReason, Verdict

from .test_api import FakeIap
from .test_definitions import REPO


@pytest.fixture
def app(service, tokens, access):
    return create_app(service, role="public", tokens=tokens, verifier=FakeIap(), access=access)


@pytest.fixture
def as_user(app, monkeypatch):
    """Point the CLI at the in-memory API as the given user."""

    def use(email: str) -> None:
        def make() -> Client:
            c = Client("http://public", transport=ASGITransport(app=app))
            c._http.headers[IAP_HEADER] = email
            return c

        monkeypatch.setattr(cli, "_client", make)

    return use


async def park(service, **kwargs):
    """A session whose first tool call waits for approval."""
    session = await service.create_session("analyst", "hi", operator="alice@example.com", client_request_id="r1", **kwargs)
    await service.permit(
        session.session_id, lease_token=session.lease.token, tool_use_id="toolu_1", tool_name="Bash", args={"command": "ls"}
    )
    return await service.get_session(session.session_id)


def parked_session(service, agent, **kwargs):
    """`park` for synchronous tests: `cli.main` runs its own event loop."""
    return asyncio.run(park(service, **kwargs))


def follow_as_alice(app, session_id: str) -> asyncio.Task:
    client = Client("http://public", transport=ASGITransport(app=app))
    client._http.headers[IAP_HEADER] = "alice@example.com"
    return asyncio.create_task(cli._follow(client, session_id, interval=0.01))


def test_agents_list_shows_tools_and_approval(as_user, agent, capsys):
    as_user("alice@example.com")
    assert cli.main(["agents", "list"]) == 0
    out = capsys.readouterr().out
    assert "analyst" in out and "enabled" in out and "needs approval: Bash" in out


def test_pending_shows_the_call_and_the_commands(as_user, service, agent, capsys):
    session = parked_session(service, agent, approvers=["lead@example.com"])
    assert [c.tool_use_id for c in session.pending] == ["toolu_1"]

    as_user("lead@example.com")
    assert cli.main(["pending"]) == 0
    out = capsys.readouterr().out
    assert 'Bash {"command": "ls"}' in out
    assert f"milos allow {session.session_id} toolu_1" in out

    assert cli.main(["sessions", "--approving"]) == 0
    assert session.session_id in capsys.readouterr().out

    as_user("alice@example.com")
    assert cli.main(["pending"]) == 0
    assert capsys.readouterr().out == ""


async def test_follow_waits_through_the_approval(app, service, agent, capsys):
    session = await park(service, approvers=["lead@example.com"])
    task = follow_as_alice(app, session.session_id)
    await asyncio.sleep(0.05)
    assert not task.done()  # parked for approval, still following
    out = capsys.readouterr().out
    assert 'Bash {"command": "ls"} → require_approval' in out
    assert "waiting for lead@example.com to decide: Bash" in out and f"milos allow {session.session_id} toolu_1" in out

    await service.decide(session.session_id, "toolu_1", verdict=Verdict.ALLOW, by="lead@example.com")
    resumed = await service.get_session(session.session_id)
    assert resumed.status == "running"
    await service.ack(resumed.session_id, lease_token=resumed.lease.token, seq=1)  # the runner consumed "hi"
    await service.finish(resumed.session_id, lease_token=resumed.lease.token, stop_reason=StopReason.END_TURN)
    await asyncio.wait_for(task, 2)
    out = capsys.readouterr().out
    assert "user.approval" in out and out.rstrip().endswith("milos send " + session.session_id + ' "..."')


async def test_follow_says_how_to_resume_when_interrupted(app, service, agent, capsys):
    session = await park(service)
    task = follow_as_alice(app, session.session_id)
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert f"detached; the session continues: milos events {session.session_id} --follow" in capsys.readouterr().out


def test_allow_resolves_the_only_waiting_call(as_user, service, agent, capsys):
    parked_session(service, agent, approvers=["lead@example.com"])
    as_user("lead@example.com")
    assert cli.main(["deny"]) == 0
    out = capsys.readouterr().out
    assert out.startswith('deny: Bash {"command": "ls"}') and "deny by lead@example.com" in out
    assert cli.main(["allow"]) == 1
    assert "nothing is waiting" in capsys.readouterr().err


def test_allow_refuses_to_guess_between_calls(as_user, service, agent, capsys):
    session = parked_session(service, agent, approvers=["lead@example.com"])
    asyncio.run(
        service.permit(
            session.session_id, lease_token=session.lease.token, tool_use_id="toolu_2", tool_name="Bash", args={"command": "rm"}
        )
    )
    as_user("lead@example.com")
    assert cli.main(["allow", session.session_id]) == 1
    captured = capsys.readouterr()
    assert "2 calls are waiting" in captured.err and "toolu_2" in captured.out
    assert cli.main(["allow", session.session_id, "toolu_2"]) == 0


def test_dotenv_is_read_but_never_overrides(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("# operator\nMILOS_API_URL=http://from-file\nMILOS_ID_TOKEN='t'\n")
    monkeypatch.setenv("MILOS_ID_TOKEN", "from-env")
    cli._load_dotenv()
    assert os.environ["MILOS_API_URL"] == "http://from-file" and os.environ["MILOS_ID_TOKEN"] == "from-env"


def test_api_errors_are_one_line(as_user, agent, capsys):
    as_user("x@other.org")
    assert cli.main(["run", "analyst", "hi", "--detach"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: 403") and "Traceback" not in err


def test_unreachable_api_is_one_line(monkeypatch, capsys):
    def unreachable(*_: object, **__: object) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(cli, "_client", lambda: Client("http://nowhere", transport=httpx.MockTransport(unreachable)))
    assert cli.main(["sessions"]) == 1
    err = capsys.readouterr().err
    assert "cannot reach the API" in err and "MILOS_API_URL" in err


def test_missing_gcloud_is_one_line(monkeypatch, capsys):
    monkeypatch.setenv("MILOS_API_URL", "http://nowhere")
    monkeypatch.delenv("MILOS_ID_TOKEN", raising=False)
    monkeypatch.setenv("PATH", "")
    assert cli.main(["sessions"]) == 1
    assert "gcloud is not installed" in capsys.readouterr().err


def test_admin_commands_publish_enable_and_list_the_registry(as_user, service, capsys):
    definition = str(REPO / "agents" / "analyst.yaml")
    as_user("alice@example.com")
    assert cli.main(["agents", "publish", definition]) == 1
    assert "403" in capsys.readouterr().err
    as_user("admin@example.com")
    assert cli.main(["agents", "publish", definition]) == 0
    assert capsys.readouterr().out.strip() == "published analyst v1"
    assert cli.main(["agents", "disable", "analyst"]) == 0
    assert "enabled=False" in capsys.readouterr().out
    assert cli.main(["agents", "registry"]) == 0
    table = capsys.readouterr().out
    assert "| analyst | 1 | no |" in table and "owner@example.com" in table


def test_registry_is_generated_from_published_versions(as_user, service, agent, capsys):
    as_user("alice@example.com")
    assert cli.main(["agents", "registry"]) == 0
    table = capsys.readouterr().out
    assert "| analyst | 1 | yes |" in table and "owner@example.com" in table
