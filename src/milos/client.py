"""Client for the public API, used by the CLI and by scripts.

Callers authenticate to IAP with a Google identity token
(`gcloud auth print-identity-token --audiences <iap-client-id>`); IAP verifies
it and the API reads the resulting assertion. Pass the token explicitly or set
`MILOS_ID_TOKEN`.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .models import Event, Session


def id_token(audience: str | None = None) -> str:
    token = os.environ.get("MILOS_ID_TOKEN")
    if token:
        return token
    cmd = ["gcloud", "auth", "print-identity-token"]
    if audience:
        cmd.append(f"--audiences={audience}")
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


class Client:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, transport=transport, timeout=30)

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._http.request(method, f"/v1{path}", **kwargs)
        if response.status_code >= 400:
            detail = response.json().get("detail", response.text) if response.content else ""
            raise RuntimeError(f"{response.status_code}: {detail}")
        return response.json()

    async def agents(self) -> list[dict[str, Any]]:
        agents: list[dict[str, Any]] = await self._call("GET", "/agents")
        return agents

    async def create_session(
        self,
        agent_id: str,
        message: str,
        *,
        client_request_id: str | None = None,
        approvers: list[str] | None = None,
        viewers: list[str] | None = None,
    ) -> Session:
        body = {
            "agent_id": agent_id,
            "message": message,
            "client_request_id": client_request_id or secrets.token_hex(8),
            "approvers": approvers or [],
            "viewers": viewers or [],
        }
        return Session(**await self._call("POST", "/sessions", json=body))

    async def sessions(self) -> list[Session]:
        return [Session(**s) for s in await self._call("GET", "/sessions")]

    async def session(self, session_id: str) -> Session:
        return Session(**await self._call("GET", f"/sessions/{session_id}"))

    async def events(self, session_id: str, *, after: int = 0) -> list[Event]:
        data = await self._call("GET", f"/sessions/{session_id}/events", params={"after": after})
        return [Event(**e) for e in data]

    async def send(self, session_id: str, text: str, *, client_request_id: str | None = None) -> Event:
        body = {"text": text, "client_request_id": client_request_id or secrets.token_hex(8)}
        return Event(**await self._call("POST", f"/sessions/{session_id}/messages", json=body))

    async def interrupt(self, session_id: str) -> Event:
        body = {"client_request_id": secrets.token_hex(8)}
        return Event(**await self._call("POST", f"/sessions/{session_id}/interrupt", json=body))

    async def confirm(self, session_id: str, tool_use_id: str, decision: str) -> dict[str, Any]:
        body = {"tool_use_id": tool_use_id, "decision": decision}
        approval: dict[str, Any] = await self._call("POST", f"/sessions/{session_id}/approvals", json=body)
        return approval

    async def terminate(self, session_id: str) -> Session:
        return Session(**await self._call("POST", f"/sessions/{session_id}/terminate"))

    async def follow(self, session_id: str, *, after: int = 0, interval: float = 2.0) -> AsyncIterator[Event]:
        """Yield events as they appear; stops when the session is idle or terminated."""
        while True:
            events = await self.events(session_id, after=after)
            for event in events:
                after = event.seq
                yield event
            if not events:
                session = await self.session(session_id)
                if session.status.value in ("idle", "terminated"):
                    return
                await asyncio.sleep(interval)

    async def close(self) -> None:
        await self._http.aclose()
