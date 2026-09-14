"""Who is calling.

Public API: the caller reached us through IAP, so the request carries a signed
JWT (`x-goog-iap-jwt-assertion`). We verify it and take the email from it; the
request body is never trusted for identity.

Internal API: Cloud Run IAM already restricts invokers to the runner,
connector and scheduler service accounts. On top of that, a runner presents
the session token the API issued at start (`X-Milos-Session`) so a runner can
only ever speak about its own session, and the scheduler's Google identity
token is verified so only it may create sessions or run inspection. The
session token is an HMAC over the session id; it is verified statelessly and
never stored in Firestore.
"""

import asyncio
import hashlib
import hmac
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import Unauthorized

IAP_HEADER = "x-goog-iap-jwt-assertion"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"


@dataclass(frozen=True, slots=True)
class Principal:
    email: str


class Directory(Protocol):
    async def is_member(self, email: str, group: str) -> bool: ...


class CloudIdentityDirectory:
    """Transitive group membership via the Cloud Identity Groups API."""

    def __init__(self) -> None:
        # Deferred: google-auth is only needed on Cloud Run.
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-identity.groups.readonly"])
        self._session = AuthorizedSession(credentials)  # type: ignore[no-untyped-call]

    async def is_member(self, email: str, group: str) -> bool:
        """Raises when the directory cannot answer: a misconfigured API must not look like a policy decision."""

        def check() -> bool:
            lookup = self._session.get(
                "https://cloudidentity.googleapis.com/v1/groups:lookup",
                params={"groupKey.id": group},
            )
            if lookup.status_code != 200:
                raise RuntimeError(f"group {group} cannot be looked up ({lookup.status_code}): {lookup.text[:200]}")
            name = lookup.json()["name"]
            response = self._session.get(
                f"https://cloudidentity.googleapis.com/v1/{name}/memberships:checkTransitiveMembership",
                params={"query": f"member_key_id == '{email}'"},
            )
            if response.status_code != 200:
                raise RuntimeError(f"membership of {group} cannot be checked ({response.status_code}): {response.text[:200]}")
            return bool(response.json().get("hasMembership"))

        return await asyncio.to_thread(check)


class TokenVerifier:
    """Verifies a signed identity token for one audience: an IAP assertion, or a Google ID token."""

    def __init__(self, audience: str, *, certs_url: str | None = None) -> None:
        self._audience = audience
        self._certs_url = certs_url

    @classmethod
    def for_iap(cls, audience: str) -> "TokenVerifier":
        return cls(audience, certs_url=IAP_CERTS_URL)

    def verify(self, token: str | None) -> Principal:
        if not token:
            raise Unauthorized("missing identity token")
        # Deferred: google-auth is only needed on Cloud Run.
        from google.auth.transport import requests
        from google.oauth2 import id_token

        try:
            request = requests.Request()
            claims: Mapping[str, Any] = (
                id_token.verify_token(token, request, audience=self._audience, certs_url=self._certs_url)
                if self._certs_url
                else id_token.verify_token(token, request, audience=self._audience)
            )
        except Exception as error:  # google-auth raises ValueError subclasses
            raise Unauthorized(f"invalid identity token: {error}") from error
        if not (email := claims.get("email")):
            raise Unauthorized("identity token carries no email")
        return Principal(email=str(email))


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
