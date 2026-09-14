"""The runner's client for the internal API.

Every call carries three credentials: the Google identity token of the runner
service account (`Authorization`, checked by Cloud Run IAM), the session token
the API issued at start (`X-Milos-Session`), and the lease token of this
particular execution (`X-Milos-Lease`).
"""

import httpx

from .http import Api, Identity
from .models import (
    Ack,
    Finish,
    PermissionAnswer,
    PermissionRequest,
    Polled,
    RunnerContext,
    RunnerEvent,
    Session,
    SnapshotPointer,
    StopReason,
)


class Control(Api):
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
        super().__init__(
            api_url,
            prefix=f"/internal/sessions/{session_id}",
            headers={"X-Milos-Session": session_token, "X-Milos-Lease": lease_token},
            identity=identity,
            transport=transport,
        )
        self.session_id = session_id

    async def context(self) -> RunnerContext:
        return await self.one(RunnerContext, "GET", "")

    async def permit(self, request: PermissionRequest) -> PermissionAnswer:
        return await self.one(PermissionAnswer, "POST", "/permissions", json=request.model_dump())

    async def poll(self) -> Polled:
        return await self.one(Polled, "POST", "/poll")

    async def ack(self, seq: int) -> Session:
        return await self.one(Session, "POST", "/ack", json=Ack(seq=seq).model_dump())

    async def report(self, events: list[RunnerEvent]) -> None:
        if events:
            await self.request("POST", "/events", json=[e.model_dump(mode="json") for e in events])

    async def advance_snapshot(self, number: int) -> Session:
        return await self.one(Session, "POST", "/snapshot", json=SnapshotPointer(number=number).model_dump())

    async def finish(self, stop_reason: StopReason) -> Session:
        return await self.one(Session, "POST", "/finish", json=Finish(stop_reason=stop_reason).model_dump(mode="json"))
