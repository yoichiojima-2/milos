from __future__ import annotations

from typing import Any

import httpx
import pytest

from milos import connector
from milos.errors import Forbidden


class Check:
    def __init__(self, allow: bool) -> None:
        self.allow = allow
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def permitted(self, session_token: str, tool_name: str, args: dict[str, Any]) -> bool:
        self.calls.append((session_token, tool_name, args))
        return self.allow


class Ctx:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


async def test_tool_runs_only_with_a_permission():
    check = Check(allow=False)
    c = connector.Connector("egress", check)

    calls = []

    @c.tool
    async def echo(text: str) -> str:
        """echo"""
        calls.append(text)
        return text

    guarded = c.mcp._tool_manager.get_tool("echo").fn
    with pytest.raises(Forbidden):
        await guarded(Ctx({"x-milos-session": "sess_1.sig"}), text="hi")
    assert check.calls == [("sess_1.sig", "mcp__egress__echo", {"text": "hi"})] and calls == []

    check.allow = True
    assert await guarded(Ctx({"x-milos-session": "sess_1.sig"}), text="hi") == "hi"
    with pytest.raises(Forbidden):
        await guarded(Ctx({}), text="no session header")


def test_check_url_rejects_http_and_private_hosts(monkeypatch):
    with pytest.raises(Forbidden):
        connector.check_url("http://example.com/")
    with pytest.raises(Forbidden):
        connector.check_url("https://metadata.google.internal/computeMetadata/v1/")
    monkeypatch.setattr(
        connector.socket,
        "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("10.0.0.5", 443))],
    )
    with pytest.raises(Forbidden):
        connector.check_url("https://intranet.example.com/")
    with pytest.raises(Forbidden):
        connector.check_url("https://example.com/" + "a" * 3000)


async def test_fetch_follows_checked_redirects_and_truncates(monkeypatch):
    monkeypatch.setattr(
        connector.socket,
        "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(connector, "MAX_RESPONSE_BYTES", 5)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://example.com/end"})
        return httpx.Response(200, text="0123456789")

    body = await connector.fetch("https://example.com/start", transport=httpx.MockTransport(handler))
    assert body == "01234"


async def test_api_permission_check_calls_internal_api():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["session"] = request.headers["x-milos-session"]
        return httpx.Response(200, json={"permitted": True, "tool_use_id": "t1"})

    check = connector.ApiPermissionCheck("http://internal")
    check._http = httpx.AsyncClient(base_url="http://internal", transport=httpx.MockTransport(handler))
    assert await check.permitted("sess_1.sig", "mcp__egress__web_fetch", {"url": "https://x"}) is True
    assert seen["path"] == "/internal/sessions/sess_1/permissions"
    assert seen["params"]["tool_name"] == "mcp__egress__web_fetch" and seen["session"] == "sess_1.sig"
