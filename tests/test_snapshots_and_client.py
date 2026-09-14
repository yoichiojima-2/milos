from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from milos import snapshots
from milos.api import create_app
from milos.auth import IAP_HEADER
from milos.client import ApiError, Client

from .fakes import FakeBlobs
from .test_api import FakeIap


async def test_snapshot_round_trip(tmp_path: Path):
    blobs = FakeBlobs()
    work = tmp_path / "work"
    transcripts = tmp_path / "home" / ".claude" / "projects"
    (work / "sub").mkdir(parents=True)
    (work / "sub" / "report.md").write_text("hello")
    transcripts.mkdir(parents=True)
    (transcripts / "session.jsonl").write_text("{}")

    await snapshots.save(blobs, "sess_1", 1, work_dir=work, transcripts=transcripts, manifest={"sdk_session_id": "x"})
    assert set(blobs.objects) == {
        "sessions/sess_1/snapshots/1/state.tar.gz",
        "sessions/sess_1/snapshots/1/manifest.json",
    }

    other = tmp_path / "restore"
    manifest = await snapshots.restore(blobs, "sess_1", 1, work_dir=other / "work", transcripts=other / "transcripts")
    assert manifest == {"sdk_session_id": "x"}
    assert (other / "work" / "sub" / "report.md").read_text() == "hello"
    assert (other / "transcripts" / "session.jsonl").exists()
    assert await snapshots.restore(blobs, "sess_1", 2, work_dir=other, transcripts=other) is None


@pytest.fixture
def client(service, tokens, access):
    app = create_app(service, role="public", tokens=tokens, verifier=FakeIap(), access=access)
    c = Client("http://public", transport=ASGITransport(app=app))
    c._http.headers[IAP_HEADER] = "alice@example.com"
    return c


async def test_client_drives_a_session(client, agent, service):
    session = await client.create_session("analyst", "hi")
    assert session.operator == "alice@example.com"
    await client.send(session.session_id, "and this")
    events = await client.events(session.session_id)
    assert [e.type.value for e in events][-1] == "user.message"
    assert [s.session_id for s in await client.sessions()] == [session.session_id]
    with pytest.raises(ApiError, match="403"):
        await client.decide(session.session_id, "nope", "allow")
    assert (await client.terminate(session.session_id)).status.value == "terminated"
    seen = [e.seq async for e in client.follow(session.session_id)]
    assert seen == [e.seq for e in await client.events(session.session_id)]


async def test_client_reports_non_json_errors():
    def edge(request):
        return httpx.Response(401, headers={"content-type": "text/html"}, text="<html>Unauthorized</html>")

    async with Client("http://public", transport=httpx.MockTransport(edge)) as c:
        with pytest.raises(ApiError, match="401: not authorised at the edge"):
            await c.agents()
