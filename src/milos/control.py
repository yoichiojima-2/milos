"""The runner's client for the internal API.

Every call carries three credentials: the Google identity token of the runner
service account (`Authorization`, checked by Cloud Run IAM), the session token
the API issued at start (`X-Milos-Session`), and the lease token of this
particular execution (`X-Milos-Lease`). The connector reuses the same headers
for its permission checks.
"""

import asyncio
import base64
import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol, Self

import httpx

from .models import AgentVersion, Event, Session, StopReason
from .service import Poll, RunnerEvent


class Identity(Protocol):
    async def token(self, audience: str) -> str | None: ...


class GoogleIdentity:
    """ID tokens from the metadata server, cached until shortly before expiry."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, float]] = {}

    async def token(self, audience: str) -> str | None:
        cached = self._cache.get(audience)
        if cached and cached[1] - time.time() > 120:
            return cached[0]
        # Deferred: importing google-auth is only needed on Cloud Run.
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token

        token: str = await asyncio.to_thread(id_token.fetch_id_token, Request(), audience)
        self._cache[audience] = (token, _expiry(token))
        return token


def _expiry(jwt: str) -> float:
    payload = jwt.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))


async def auth_headers(identity: Identity | None, audience: str, headers: dict[str, str]) -> dict[str, str]:
    """`headers` plus a bearer token for `audience` when the caller has a Google identity."""
    if identity and (token := await identity.token(audience)):
        return {**headers, "Authorization": f"Bearer {token}"}
    return headers


@dataclass(frozen=True, slots=True)
class Context:
    session: Session
    version: AgentVersion


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
        self._identity = identity
        self._http = httpx.AsyncClient(base_url=self.api_url, transport=transport, timeout=30)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def headers(self) -> dict[str, str]:
        session = {"X-Milos-Session": self.session_token, "X-Milos-Lease": self.lease_token}
        return await auth_headers(self._identity, self.api_url, session)

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._http.request(
            method, f"/internal/sessions/{self.session_id}{path}", headers=await self.headers(), **kwargs
        )
        response.raise_for_status()
        return response.json()

    async def context(self) -> Context:
        data = await self._call("GET", "")
        return Context(session=Session.model_validate(data["session"]), version=AgentVersion.model_validate(data["version"]))

    async def permit(self, tool_use_id: str, tool_name: str, args: dict[str, Any]) -> tuple[str, str]:
        body = {"tool_use_id": tool_use_id, "tool_name": tool_name, "args": args}
        data = await self._call("POST", "/permit", json=body)
        return data["decision"], data["reason"]

    async def poll(self) -> Poll:
        data = await self._call("POST", "/poll")
        return Poll(stop=data["stop"], events=[Event.model_validate(e) for e in data["events"]])

    async def ack(self, seq: int) -> None:
        await self._call("POST", "/ack", json={"seq": seq})

    async def report(self, events: list[RunnerEvent]) -> None:
        if events:
            await self._call("POST", "/events", json=[asdict(e) for e in events])

    async def advance_snapshot(self, number: int) -> None:
        await self._call("POST", "/snapshot", json={"number": number})

    async def finish(self, stop_reason: StopReason) -> None:
        await self._call("POST", "/finish", json={"stop_reason": stop_reason.value})

    async def close(self) -> None:
        await self._http.aclose()
