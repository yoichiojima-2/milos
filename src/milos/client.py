"""Client for the public API, used by the CLI and by scripts.

Callers authenticate to IAP with a Google identity token
(`gcloud auth print-identity-token --audiences <iap-client-id>`); IAP verifies
it and the API reads the resulting assertion. Pass the token explicitly or set
`MILOS_ID_TOKEN`.
"""

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator
from typing import Any, Self

import httpx
from pydantic import BaseModel

from .models import Approval, Event, Published, Session, SessionStatus, ToolDecision


def id_token(audience: str | None = None) -> str:
    if token := os.environ.get("MILOS_ID_TOKEN"):
        return token
    cmd = ["gcloud", "auth", "print-identity-token", *([f"--audiences={audience}"] if audience else [])]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


class ApiError(RuntimeError):
    """The API answered with an error status; `status` and `detail` carry the response."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


def _detail(response: httpx.Response) -> str:
    """The API's `detail`, or a short description when the body is not the API's (an IAP or Cloud Run error page)."""
    if response.status_code in (401, 403) and "text/html" in response.headers.get("content-type", ""):
        return "not authorised at the edge; set MILOS_IAP_CLIENT_ID (or MILOS_ID_TOKEN) so the client sends an identity token"
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:200] or response.reason_phrase
    return str(body.get("detail", body)) if isinstance(body, dict) else str(body)


def _request_id(client_request_id: str | None) -> dict[str, str]:
    """Omitted keys get a fresh id from the API; passing one makes the request retry-safe."""
    return {"client_request_id": client_request_id} if client_request_id else {}


class Client:
    def __init__(self, base_url: str, *, token: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers=headers, transport=transport, timeout=30)

    @classmethod
    def from_env(cls) -> Self:
        """Build a client from `MILOS_API_URL` and, for the token, `MILOS_ID_TOKEN` or `MILOS_IAP_CLIENT_ID`.

        Raises `KeyError` naming the variable when `MILOS_API_URL` is missing.
        """
        url = os.environ.get("MILOS_API_URL")
        if not url:
            raise KeyError("MILOS_API_URL")
        return cls(url, token=id_token(os.environ.get("MILOS_IAP_CLIENT_ID")))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._http.request(method, f"/v1{path}", **kwargs)
        if response.status_code >= 400:
            raise ApiError(response.status_code, _detail(response))
        return response.json()

    async def _one[M: BaseModel](self, model: type[M], method: str, path: str, **kwargs: Any) -> M:
        return model.model_validate(await self._call(method, path, **kwargs))

    async def _many[M: BaseModel](self, model: type[M], method: str, path: str, **kwargs: Any) -> list[M]:
        return [model.model_validate(item) for item in await self._call(method, path, **kwargs)]

    async def agents(self) -> list[Published]:
        return await self._many(Published, "GET", "/agents")

    async def create_session(
        self,
        agent_id: str,
        message: str,
        *,
        client_request_id: str | None = None,
        approvers: list[str] | None = None,
        viewers: list[str] | None = None,
    ) -> Session:
        body = {"agent_id": agent_id, "message": message, "approvers": approvers or [], "viewers": viewers or []}
        return await self._one(Session, "POST", "/sessions", json=body | _request_id(client_request_id))

    async def sessions(self) -> list[Session]:
        return await self._many(Session, "GET", "/sessions")

    async def session(self, session_id: str) -> Session:
        return await self._one(Session, "GET", f"/sessions/{session_id}")

    async def events(self, session_id: str, *, after: int = 0) -> list[Event]:
        return await self._many(Event, "GET", f"/sessions/{session_id}/events", params={"after": after})

    async def send(self, session_id: str, text: str, *, client_request_id: str | None = None) -> Event:
        body = {"text": text} | _request_id(client_request_id)
        return await self._one(Event, "POST", f"/sessions/{session_id}/messages", json=body)

    async def interrupt(self, session_id: str) -> Event:
        return await self._one(Event, "POST", f"/sessions/{session_id}/interrupt", json={})

    async def confirm(self, session_id: str, tool_use_id: str, decision: ToolDecision) -> Approval:
        body = {"tool_use_id": tool_use_id, "decision": decision}
        return await self._one(Approval, "POST", f"/sessions/{session_id}/approvals", json=body)

    async def terminate(self, session_id: str) -> Session:
        return await self._one(Session, "POST", f"/sessions/{session_id}/terminate")

    async def follow(self, session_id: str, *, after: int = 0, interval: float = 2.0) -> AsyncIterator[Event]:
        """Yield events as they appear; stops when the session is idle or terminated."""
        while True:
            events = await self.events(session_id, after=after)
            for event in events:
                after = event.seq
                yield event
            if not events:
                session = await self.session(session_id)
                if session.status in (SessionStatus.IDLE, SessionStatus.TERMINATED):
                    return
                await asyncio.sleep(interval)

    async def close(self) -> None:
        await self._http.aclose()
