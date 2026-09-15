"""Client for the public API, used by the CLI and by scripts.

Callers authenticate to IAP with a Google identity token
(`gcloud auth print-identity-token --audiences <iap-client-id>`); IAP verifies
it and the API reads the resulting assertion. Pass the token explicitly or set
`MILOS_ID_TOKEN`.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Literal, Self

import httpx

from .http import Api, ApiError, id_token
from .models import (
    Agent,
    AgentPatch,
    AgentVersion,
    Approval,
    Event,
    NewApproval,
    NewMessage,
    NewSession,
    Published,
    SessionStatus,
    SessionView,
    StopReason,
    Verdict,
)

__all__ = ["ApiError", "Client", "SessionRole", "id_token"]

type SessionRole = Literal["operator", "approver"]


def _request_id(client_request_id: str | None) -> dict[str, str]:
    """Passing an id makes the request retry-safe; otherwise the model's fresh default stands."""
    return {"client_request_id": client_request_id} if client_request_id else {}


class Client(Api):
    def __init__(self, base_url: str, *, token: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        super().__init__(base_url, prefix="/v1", headers=headers, transport=transport)

    @classmethod
    def from_env(cls) -> Self:
        """Build a client from `MILOS_API_URL` and, for the token, `MILOS_ID_TOKEN` or `MILOS_IAP_CLIENT_ID`.

        Raises `KeyError` naming the variable when `MILOS_API_URL` is missing.
        """
        url = os.environ.get("MILOS_API_URL")
        if not url:
            raise KeyError("MILOS_API_URL")
        return cls(url, token=id_token(os.environ.get("MILOS_IAP_CLIENT_ID")))

    async def agents(self) -> list[Published]:
        return await self.many(Published, "GET", "/agents")

    async def publish(self, version: AgentVersion) -> AgentVersion:
        """Publish a validated definition as the agent's next version (admin group only)."""
        return await self.one(AgentVersion, "POST", "/agents", json=version.model_dump(mode="json"))

    async def set_enabled(self, agent_id: str, enabled: bool) -> Agent:
        """Enable or disable an agent (admin group only); disabling stops every session at its next tool request."""
        return await self.one(Agent, "PATCH", f"/agents/{agent_id}", json=AgentPatch(enabled=enabled).model_dump())

    async def create_session(
        self,
        agent_id: str,
        message: str,
        *,
        client_request_id: str | None = None,
        approvers: list[str] | None = None,
        viewers: list[str] | None = None,
    ) -> SessionView:
        new = NewSession(agent_id=agent_id, message=message, approvers=approvers or [], viewers=viewers or [])
        return await self.one(SessionView, "POST", "/sessions", json=new.model_dump() | _request_id(client_request_id))

    async def sessions(self, *, role: SessionRole = "operator") -> list[SessionView]:
        """Sessions you started, or with `role="approver"` those that name you as an approver."""
        return await self.many(SessionView, "GET", "/sessions", params={"role": role})

    async def session(self, session_id: str) -> SessionView:
        return await self.one(SessionView, "GET", f"/sessions/{session_id}")

    async def events(self, session_id: str, *, after: int = 0) -> list[Event]:
        return await self.many(Event, "GET", f"/sessions/{session_id}/events", params={"after": after})

    async def send(self, session_id: str, text: str, *, client_request_id: str | None = None) -> Event:
        body = NewMessage(text=text).model_dump() | _request_id(client_request_id)
        return await self.one(Event, "POST", f"/sessions/{session_id}/messages", json=body)

    async def interrupt(self, session_id: str) -> Event:
        return await self.one(Event, "POST", f"/sessions/{session_id}/interrupt", json={})

    async def decide(self, session_id: str, tool_use_id: str, verdict: Verdict) -> Approval:
        """Allow or deny a parked tool call, as someone other than the operator."""
        body = NewApproval(tool_use_id=tool_use_id, verdict=verdict).model_dump(mode="json")
        return await self.one(Approval, "POST", f"/sessions/{session_id}/approvals", json=body)

    async def terminate(self, session_id: str) -> SessionView:
        return await self.one(SessionView, "POST", f"/sessions/{session_id}/terminate")

    async def follow(
        self, session_id: str, *, after: int = 0, interval: float = 2.0, through_approvals: bool = True
    ) -> AsyncIterator[Event]:
        """Yield events as they appear until the session is terminated or idle.

        A session waiting for a person to allow or deny a tool call is not finished: the
        decision restarts it, so by default the iterator keeps polling and resumes with the
        events that follow. A caller that decides those calls itself passes
        `through_approvals=False` to get control back at the pause.
        """
        while True:
            events = await self.events(session_id, after=after)
            for event in events:
                after = event.seq
                yield event
            if not events:
                session = await self.session(session_id)
                if session.status == SessionStatus.TERMINATED:
                    return
                waiting = through_approvals and session.stop_reason == StopReason.REQUIRES_ACTION
                if session.status == SessionStatus.IDLE and not waiting:
                    return
                await asyncio.sleep(interval)
