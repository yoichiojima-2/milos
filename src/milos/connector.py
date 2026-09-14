"""Connectors: MCP servers that run outside the sandbox.

One implementation, deployed twice. `internal` holds read access to approved
data and has no NAT and no secrets; `egress` holds the SaaS credentials and
the only path to the internet. Before executing any call a connector asks the
API whether this exact call (tool name + argument hash) was permitted for the
session named by the `X-Milos-Session` header the runner sent along; the
permission the API created in `PreToolUse` is the only thing that lets a
connector act.

`web_fetch` is the reference tool: GET only, https only, public addresses
only, a bounded number of redirects, a bounded response, every URL journaled
through the permission it consumed.
"""

import asyncio
import inspect
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import httpx
from mcp.server.mcpserver import Context, MCPServer

from .errors import Forbidden
from .http import Api, ApiError, GoogleIdentity, Identity
from .models import PermissionLookup, sha256_json
from .settings import MCP_PATH, ConnectorSettings

MAX_URL_LENGTH = 2048
MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 1_000_000
METADATA_HOSTS = {"metadata.google.internal", "169.254.169.254"}
log = logging.getLogger(__name__)


class PermissionCheck(Protocol):
    async def permitted(self, session_token: str, tool_name: str, args: dict[str, Any]) -> bool: ...


class ApiPermissionCheck(Api):
    """Asks the internal API; authenticates as the connector's own service account."""

    def __init__(self, api_url: str, *, identity: Identity | None = None) -> None:
        super().__init__(api_url, prefix="/internal/sessions", identity=identity, timeout=15)

    async def permitted(self, session_token: str, tool_name: str, args: dict[str, Any]) -> bool:
        session_id = session_token.rsplit(".", 1)[0]
        try:
            found = await self.one(
                PermissionLookup,
                "GET",
                f"/{session_id}/permissions",
                params={"tool_name": tool_name, "args_sha256": sha256_json(args)},
                headers={"X-Milos-Session": session_token},
            )
        except ApiError as error:
            log.warning("permission check for %s in %s failed: %s", tool_name, session_id, error)
            return False
        return found.permitted


class Connector:
    def __init__(self, name: str, check: PermissionCheck) -> None:
        self.name = name
        self.check = check
        self.mcp = MCPServer(name)

    def tool(self, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        """Register an async tool; the permission check runs before its body."""
        tool_name = f"mcp__{self.name}__{fn.__name__}"

        async def guarded(ctx: Context, **kwargs: Any) -> Any:
            session_token = (ctx.headers or {}).get("x-milos-session", "")
            if not session_token or not await self.check.permitted(session_token, tool_name, kwargs):
                raise Forbidden(f"{tool_name} was not permitted for this call")
            return await fn(**kwargs)

        # The server derives the tool's schema from the signature: expose the
        # real parameters plus the context it injects.
        ctx_param = inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context)
        params = list(inspect.signature(fn).parameters.values())
        cast(Any, guarded).__signature__ = inspect.Signature([*params, ctx_param])
        guarded.__annotations__ = {**fn.__annotations__, "ctx": Context}
        guarded.__name__ = fn.__name__
        guarded.__doc__ = fn.__doc__
        self.mcp.tool(name=fn.__name__, description=fn.__doc__ or fn.__name__)(guarded)
        return fn

    def app(self) -> Any:
        return self.mcp.streamable_http_app(stateless_http=True, host="0.0.0.0", streamable_http_path=MCP_PATH)


# --- the egress connector's reference tool ---------------------------------------


def _public_host(host: str) -> None:
    if host in METADATA_HOSTS:
        raise Forbidden("metadata endpoints are not reachable")
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise Forbidden(f"cannot resolve {host}") from error
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise Forbidden(f"{host} resolves to a non-public address")


def check_url(url: str) -> str:
    if len(url) > MAX_URL_LENGTH:
        raise Forbidden("URL too long")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise Forbidden("only https URLs are fetched")
    _public_host(parts.hostname)
    return url


async def fetch(url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """GET a public https URL, following at most MAX_REDIRECTS checked hops."""
    async with httpx.AsyncClient(transport=transport, follow_redirects=False, timeout=20) as http:
        for _ in range(MAX_REDIRECTS + 1):
            response = await http.get(check_url(url), headers={"Range": f"bytes=0-{MAX_RESPONSE_BYTES}"})
            if response.is_redirect:
                url = str(response.next_request.url) if response.next_request else ""
                continue
            body = response.content[:MAX_RESPONSE_BYTES]
            return body.decode(response.encoding or "utf-8", errors="replace")
    raise Forbidden("too many redirects")


def egress(check: PermissionCheck) -> Connector:
    connector = Connector("egress", check)

    @connector.tool
    async def web_fetch(url: str) -> str:
        """Fetch a public https URL with GET and return its body as text."""
        return await fetch(url)

    return connector


# --- the internal connector's data tools ----------------------------------------------

MAX_LIST = 200
MAX_FILE_BYTES = 200_000


class DataFiles(Protocol):
    """Read-only view of the approved data bucket."""

    async def list(self, prefix: str) -> list[str]: ...

    async def read(self, path: str) -> bytes | None: ...


class GcsDataFiles:
    def __init__(self, bucket: str, *, project: str | None = None) -> None:
        # Deferred: google-cloud-storage is only needed on Cloud Run.
        from google.cloud import storage  # type: ignore[attr-defined]

        self._bucket = storage.Client(project=project).bucket(bucket)

    async def list(self, prefix: str) -> list[str]:
        def run() -> list[str]:
            return [blob.name for blob in self._bucket.list_blobs(prefix=prefix, max_results=MAX_LIST)]

        return await asyncio.to_thread(run)

    async def read(self, path: str) -> bytes | None:
        def run() -> bytes | None:
            blob = self._bucket.blob(path)
            if not blob.exists():
                return None
            return cast(bytes, blob.download_as_bytes(start=0, end=MAX_FILE_BYTES - 1))

        return await asyncio.to_thread(run)


def check_path(path: str) -> str:
    if not path or path.startswith("/") or ".." in path.split("/"):
        raise Forbidden("paths are relative and stay inside the data bucket")
    return path


def internal(check: PermissionCheck, *, data: DataFiles | None = None) -> Connector:
    """Read-only tools over the approved data bucket. Without a bucket the connector has no tools."""
    connector = Connector("internal", check)
    if data is None:
        return connector

    @connector.tool
    async def list_files(prefix: str = "") -> list[str]:
        """List files in the shared data project under a prefix (at most 200)."""
        return await data.list(check_path(prefix) if prefix else "")

    @connector.tool
    async def read_file(path: str) -> str:
        """Read one text file from the shared data project (at most 200 kB)."""
        body = await data.read(check_path(path))
        if body is None:
            raise Forbidden(f"no such file: {path}")
        return body.decode("utf-8", errors="replace")

    return connector


def build_from_env(name: str) -> Any:
    settings = ConnectorSettings.from_env()
    check = ApiPermissionCheck(settings.api_url, identity=GoogleIdentity())
    if name == "egress":
        return egress(check).app()
    data = GcsDataFiles(settings.data_bucket) if settings.data_bucket else None
    return internal(check, data=data).app()
