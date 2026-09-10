"""Who is calling.

Public API: the caller reached us through IAP, so the request carries a signed
JWT (`x-goog-iap-jwt-assertion`). We verify it and take the email from it; the
request body is never trusted for identity.

Internal API: Cloud Run IAM already restricts invokers to the runner and
scheduler service accounts. On top of that, a runner presents the session
token the API issued at start (`Authorization: Bearer <token>`) so a runner can
only ever speak about its own session. The token is an HMAC over the session
id; it is verified statelessly and never stored in Firestore.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .errors import Unauthorized

IAP_HEADER = "x-goog-iap-jwt-assertion"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"


@dataclass(frozen=True)
class Principal:
    email: str
    kind: Literal["user", "scheduler", "runner"] = "user"


class Directory(Protocol):
    async def is_member(self, email: str, group: str) -> bool: ...


class CloudIdentityDirectory:
    """Transitive group membership via the Cloud Identity Groups API."""

    def __init__(self) -> None:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-identity.groups.readonly"]
        )
        self._session = AuthorizedSession(credentials)

    async def is_member(self, email: str, group: str) -> bool:
        import asyncio

        def check() -> bool:
            lookup = self._session.get(
                "https://cloudidentity.googleapis.com/v1/groups:lookup",
                params={"groupKey.id": group},
            )
            if lookup.status_code != 200:
                return False
            name = lookup.json()["name"]
            response = self._session.get(
                f"https://cloudidentity.googleapis.com/v1/{name}/memberships:checkTransitiveMembership",
                params={"query": f"member_key_id == '{email}'"},
            )
            return response.status_code == 200 and bool(response.json().get("hasMembership"))

        return await asyncio.to_thread(check)


class IapVerifier:
    def __init__(self, audience: str) -> None:
        self._audience = audience

    def verify(self, token: str | None) -> Principal:
        if not token:
            raise Unauthorized("missing IAP assertion")
        from google.auth.transport import requests
        from google.oauth2 import id_token

        try:
            claims: dict[str, Any] = id_token.verify_token(
                token, requests.Request(), audience=self._audience, certs_url=IAP_CERTS_URL
            )
        except Exception as error:  # google-auth raises ValueError subclasses
            raise Unauthorized(f"invalid IAP assertion: {error}") from error
        return Principal(email=claims["email"])


class SessionTokens:
    """Stateless session tokens: `<session_id>.<hmac_sha256(key, session_id)>`."""

    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("session token key must not be empty")
        self._key = key.encode()

    def issue(self, session_id: str) -> str:
        return f"{session_id}.{self._sign(session_id)}"

    def verify(self, token: str | None) -> str:
        """Return the session id the token was issued for."""
        if not token or "." not in token:
            raise Unauthorized("missing session token")
        session_id, signature = token.rsplit(".", 1)
        if not hmac.compare_digest(signature, self._sign(session_id)):
            raise Unauthorized("invalid session token")
        return session_id

    def _sign(self, session_id: str) -> str:
        return hmac.new(self._key, session_id.encode(), hashlib.sha256).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)
