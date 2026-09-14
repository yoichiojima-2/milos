"""HTTP plumbing shared by every client of the API: the CLI and scripts (`client`),
the runner (`control`) and the connectors' permission check.

`Api` wraps one `httpx.AsyncClient` with a path prefix, fixed headers, an optional
Google identity that adds a bearer token per request (Cloud Run IAM checks it),
and one error translation: any 4xx/5xx becomes `ApiError` carrying the API's
`detail`.
"""

import asyncio
import base64
import json
import os
import subprocess
import time
from typing import Any, Protocol, Self

import httpx
from pydantic import BaseModel


class ApiError(RuntimeError):
    """The API answered with an error status; `status` and `detail` carry the response."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


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


def id_token(audience: str | None = None) -> str:
    """A person's identity token: `MILOS_ID_TOKEN`, or one minted by gcloud.

    Raises `RuntimeError` with the remedy when neither works.
    """
    if token := os.environ.get("MILOS_ID_TOKEN"):
        return token
    cmd = ["gcloud", "auth", "print-identity-token", *([f"--audiences={audience}"] if audience else [])]
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()
    except FileNotFoundError:
        raise RuntimeError("gcloud is not installed; install it or set MILOS_ID_TOKEN") from None
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"gcloud could not mint an identity token: {error.stderr.strip()}") from None


def _detail(response: httpx.Response) -> str:
    """The API's `detail`, or a short description when the body is not the API's (an IAP or Cloud Run error page)."""
    if response.status_code in (401, 403) and "text/html" in response.headers.get("content-type", ""):
        return "not authorised at the edge; set MILOS_IAP_CLIENT_ID (or MILOS_ID_TOKEN) so the client sends an identity token"
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:200] or response.reason_phrase
    return str(body.get("detail", body)) if isinstance(body, dict) else str(body)


class Api:
    def __init__(
        self,
        base_url: str,
        *,
        prefix: str = "",
        headers: dict[str, str] | None = None,
        identity: Identity | None = None,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._prefix = prefix
        self._identity = identity
        self._http = httpx.AsyncClient(base_url=self.base_url, headers=headers or {}, transport=transport, timeout=timeout)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    async def request(self, method: str, path: str, *, headers: dict[str, str] | None = None, **kwargs: Any) -> Any:
        """One call under the prefix; the identity's bearer token is added per request."""
        sent = await auth_headers(self._identity, self.base_url, headers or {})
        response = await self._http.request(method, f"{self._prefix}{path}", headers=sent, **kwargs)
        if response.status_code >= 400:
            raise ApiError(response.status_code, _detail(response))
        return response.json()

    async def one[M: BaseModel](self, model: type[M], method: str, path: str, **kwargs: Any) -> M:
        return model.model_validate(await self.request(method, path, **kwargs))

    async def many[M: BaseModel](self, model: type[M], method: str, path: str, **kwargs: Any) -> list[M]:
        return [model.model_validate(item) for item in await self.request(method, path, **kwargs)]
