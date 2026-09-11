"""The runner's client for the internal API.

Every call carries three credentials: the Google identity token of the runner
service account (`Authorization`, checked by Cloud Run IAM), the session token
the API issued at start (`X-Milos-Session`), and the lease token of this
particular execution (`X-Milos-Lease`). The connector reuses the same headers
for its permission checks.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .models import Event, Session, StopReason
from .service import RunnerEvent


class Identity(Protocol):
    async def token(self, audience: str) -> str | None: ...


class NoIdentity:
    async def token(self, audience: str) -> str | None:
        return None


class GoogleIdentity:
    """ID tokens from the metadata server, cached until shortly before expiry."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, float]] = {}

    async def token(self, audience: str) -> str | None:
        cached = self._cache.get(audience)
        if cached and cached[1] - time.time() > 120:
            return cached[0]
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token

        token: str = await asyncio.to_thread(id_token.fetch_id_token, Request(), audience)
        self._cache[audience] = (token, _expiry(token))
        return token


def _expiry(jwt: str) -> float:
    payload = jwt.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))


@dataclass(frozen=True)
class Context:
    session: Session
    version: dict[str, Any]


@dataclass(frozen=True)
class Polled:
    stop: bool
    events: list[Event]


class Control:
    def __init__(
        self,
        api_url: str,
        *,
        session_id: str,
        session_token: str,
        lease_token: str,
        identity: Identity | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.session_id = session_id
        self.session_token = session_token
        self.lease_token = lease_token
        self._identity = identity or NoIdentity()
        self._http = httpx.AsyncClient(base_url=self.api_url, transport=transport, timeout=30)

    async def headers(self) -> dict[str, str]:
        headers = {"X-Milos-Session": self.session_token, "X-Milos-Lease": self.lease_token}
        token = await self._identity.token(self.api_url)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._http.request(
            method,
            f"/internal/sessions/{self.session_id}{path}",
            headers=await self.headers(),
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

    async def context(self) -> Context:
        data = await self._call("GET", "")
        return Context(session=Session(**data["session"]), version=data["version"])

    async def permit(
        self, tool_use_id: str, tool_name: str, args: dict[str, Any]
    ) -> tuple[str, str]:
        data = await self._call(
            "POST",
            "/permit",
            json={"tool_use_id": tool_use_id, "tool_name": tool_name, "args": args},
        )
        return data["decision"], data["reason"]

    async def poll(self) -> Polled:
        data = await self._call("POST", "/poll")
        return Polled(stop=data["stop"], events=[Event(**e) for e in data["events"]])

    async def ack(self, seq: int) -> None:
        await self._call("POST", "/ack", json={"seq": seq})

    async def report(self, events: list[RunnerEvent]) -> None:
        if not events:
            return
        body = [
            {"type": e.type.value, "payload": e.payload, "tool_use_id": e.tool_use_id}
            for e in events
        ]
        await self._call("POST", "/events", json=body)

    async def advance_snapshot(self, number: int) -> None:
        await self._call("POST", "/snapshot", json={"number": number})

    async def finish(self, stop_reason: StopReason) -> None:
        await self._call("POST", "/finish", json={"stop_reason": stop_reason.value})

    async def close(self) -> None:
        await self._http.aclose()
