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

from .test_api import FakeIap


@pytest.fixture
def app(service, tokens, directory):
    return create_app(service, role="public", tokens=tokens, iap=FakeIap(), directory=directory)


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


def parked_session(service, agent, **kwargs):
    """A session whose first tool call waits for approval. `cli.main` runs its own loop, so tests stay synchronous."""

    async def make():
        session = await service.create_session("analyst", "hi", operator="alice@example.com", client_request_id="r1", **kwargs)
        await service.permit(
            session.session_id, lease_token=session.lease.token, tool_use_id="toolu_1", tool_name="Bash", args={"command": "ls"}
        )
        return await service.get_session(session.session_id)

    return asyncio.run(make())


def test_agents_list_shows_tools_and_approval(as_user, agent, capsys):
    as_user("alice@example.com")
    assert cli.main(["agents", "list"]) == 0
    out = capsys.readouterr().out
    assert "analyst" in out and "enabled" in out and "needs approval: Bash" in out


def test_pending_shows_the_call_and_the_commands(as_user, service, agent, capsys):
    session = parked_session(service, agent, approvers=["lead@example.com"])
    assert session.pending_tool_use_ids == ["toolu_1"]

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


def test_follow_ends_with_the_next_step(as_user, service, agent, capsys):
    session = parked_session(service, agent)
    assert session.stop_reason == "requires_action"

    as_user("alice@example.com")
    assert cli.main(["events", session.session_id, "--follow"]) == 0
    assert f"waiting for approval: milos allow {session.session_id} toolu_1" in capsys.readouterr().out


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
