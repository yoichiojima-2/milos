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

The internal connector's BigQuery tools take the `Grant` the check returned:
the API's answer names the session, the tool use and the datasets the
definition lets the agent reach, and `warehouse.py` refuses anything outside.
"""

import asyncio
import inspect
import ipaddress
import json
import logging
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import httpx
from mcp.server.mcpserver import Context, MCPServer

from .errors import Forbidden
from .http import Api, ApiError, GoogleIdentity, Identity
from .models import DataScope, PermissionLookup, sha256_json
from .settings import MCP_PATH, ConnectorSettings
from .warehouse import (
    MAX_ROWS,
    OWNER_LABEL,
    BigQueryWarehouse,
    Plan,
    Table,
    Warehouse,
    check_plan,
    check_sql,
    label,
    owned,
)

MAX_URL_LENGTH = 2048
MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 1_000_000
METADATA_HOSTS = {"metadata.google.internal", "169.254.169.254"}
log = logging.getLogger(__name__)


class PermissionCheck(Protocol):
    async def permitted(self, session_token: str, tool_name: str, args: dict[str, Any]) -> PermissionLookup: ...


@dataclass(frozen=True, slots=True)
class Grant:
    """The permission the API found for one call: whose it is, and what it may reach."""

    session_id: str
    tool_use_id: str
    scope: DataScope | None


class ApiPermissionCheck(Api):
    """Asks the internal API; authenticates as the connector's own service account."""

    def __init__(self, api_url: str, *, identity: Identity | None = None) -> None:
        super().__init__(api_url, prefix="/internal/sessions", identity=identity, timeout=15)

    async def permitted(self, session_token: str, tool_name: str, args: dict[str, Any]) -> PermissionLookup:
        session_id = session_token.rsplit(".", 1)[0]
        try:
            return await self.one(
                PermissionLookup,
                "GET",
                f"/{session_id}/permissions",
                params={"tool_name": tool_name, "args_sha256": sha256_json(args)},
                headers={"X-Milos-Session": session_token},
            )
        except ApiError as error:
            log.warning("permission check for %s in %s failed: %s", tool_name, session_id, error)
            return PermissionLookup(permitted=False)


class Connector:
    def __init__(self, name: str, check: PermissionCheck) -> None:
        self.name = name
        self.check = check
        self.mcp = MCPServer(name)

    def tool(self, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        """Register an async tool; the permission check runs before its body.

        A tool with a keyword-only `grant` parameter receives the permission
        the API found; the parameter is not part of the tool's schema.
        """
        tool_name = f"mcp__{self.name}__{fn.__name__}"
        params = list(inspect.signature(fn).parameters.values())
        wants_grant = any(p.name == "grant" for p in params)

        async def guarded(ctx: Context, **kwargs: Any) -> Any:
            session_token = (ctx.headers or {}).get("x-milos-session", "")
            found = await self.check.permitted(session_token, tool_name, kwargs) if session_token else None
            if not (found and found.permitted):
                raise Forbidden(f"{tool_name} was not permitted for this call")
            if wants_grant:
                session_id = session_token.rsplit(".", 1)[0]
                kwargs["grant"] = Grant(session_id=session_id, tool_use_id=found.tool_use_id or "", scope=found.scope)
            return await fn(**kwargs)

        # The server derives the tool's schema from the signature: expose the
        # real parameters plus the context it injects.
        ctx_param = inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context)
        exposed = [p for p in params if p.name != "grant"]
        cast(Any, guarded).__signature__ = inspect.Signature([*exposed, ctx_param])
        guarded.__annotations__ = {k: v for k, v in fn.__annotations__.items() if k != "grant"} | {"ctx": Context}
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


def internal(check: PermissionCheck, *, data: DataFiles | None = None, warehouse: Warehouse | None = None) -> Connector:
    """Tools over the approved data: read-only files from the bucket, and BigQuery within the definition's scope.

    Without a bucket there are no file tools; without a warehouse no BigQuery tools.
    """
    connector = Connector("internal", check)
    if data is not None:
        _file_tools(connector, data)
    if warehouse is not None:
        _bigquery_tools(connector, warehouse)
    return connector


def _file_tools(connector: Connector, data: DataFiles) -> None:
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


# --- the internal connector's BigQuery tools ------------------------------------------

MAX_RESULT_BYTES = 200_000
MAX_INSERT_ROWS = 1_000
MAX_INSERT_BYTES = 1_000_000
TABLE_ID = re.compile(r"^[A-Za-z0-9_]{1,1024}$")


def _scope(grant: Grant) -> DataScope:
    if grant.scope is None:
        raise Forbidden("the definition names no datasets and no workspace")
    return grant.scope


def _labels(grant: Grant) -> dict[str, str]:
    """Job labels that tie BigQuery's audit log to the journal."""
    scope = _scope(grant)
    return {
        "milos_session": label(grant.session_id),
        "milos_tool_use": label(grant.tool_use_id),
        "milos_agent": label(scope.agent_id),
    }


def bounded_rows(rows: list[dict[str, Any]], limit: int = MAX_RESULT_BYTES) -> tuple[list[dict[str, Any]], bool]:
    """As many leading rows as fit in `limit` bytes of JSON, and whether any were dropped."""
    kept: list[dict[str, Any]] = []
    size = 2
    for row in rows:
        size += len(json.dumps(row, separators=(",", ":"))) + 1
        if size > limit:
            return kept, True
        kept.append(row)
    return kept, False


class Workspace:
    """Session ownership of workspace tables: a session lists, reads and writes only the tables it created."""

    def __init__(self, warehouse: Warehouse) -> None:
        self._warehouse = warehouse

    async def check(self, plan: Plan, scope: DataScope, grant: Grant) -> Plan:
        """Refuse a statement that reads another session's table, or writes over one; a new table is fine."""
        session = label(grant.session_id)
        for table in plan.reads:
            if table.dataset != scope.workspace or table in plan.writes:
                continue
            if not owned(await self._warehouse.labels(scope.agent_id, table), session):
                raise Forbidden(f"{table} is not a table of this session")
        for table in plan.writes:
            labels = await self._warehouse.labels(scope.agent_id, table)
            if labels is not None and not owned(labels, session):
                raise Forbidden(f"{table} exists and is not this session's; choose another name")
        return plan

    async def claim(self, tables: tuple[Table, ...], scope: DataScope, grant: Grant) -> None:
        """Label the tables a statement created with this session, so later calls recognise them."""
        for table in tables:
            labels = await self._warehouse.labels(scope.agent_id, table)
            if labels is not None and OWNER_LABEL not in labels:
                await self._warehouse.claim(scope.agent_id, table, {OWNER_LABEL: label(grant.session_id)})


def _bigquery_tools(connector: Connector, warehouse: Warehouse) -> None:
    project = warehouse.project
    workspace = Workspace(warehouse)

    @connector.tool
    async def bq_tables(dataset: str, *, grant: Grant) -> list[dict[str, Any]]:
        """List the tables of one BigQuery dataset the agent may reach, with columns and row counts. In the workspace, only this session's tables."""
        scope = _scope(grant)
        if dataset not in scope.readable():
            raise Forbidden(f"{dataset} is outside the agent's datasets")
        found = await warehouse.tables(scope.agent_id, dataset)
        if dataset == scope.workspace:
            found = [t for t in found if owned(t.labels, label(grant.session_id))]
        return [{k: v for k, v in asdict(t).items() if k != "labels"} for t in found]

    @connector.tool
    async def bq_query(sql: str, *, grant: Grant) -> dict[str, Any]:
        """Run one SELECT over the agent's datasets and this session's workspace tables. Name tables as dataset.table; at most 1000 rows / 200 kB come back."""
        scope = _scope(grant)
        plan = check_plan(await warehouse.plan(scope.agent_id, check_sql(sql)), scope, project, write=False)
        await workspace.check(plan, scope, grant)
        result = await warehouse.run(scope.agent_id, sql, labels=_labels(grant), max_rows=MAX_ROWS)
        rows, cut = bounded_rows(result.rows)
        return {"rows": rows, "row_count": len(rows), "truncated": result.truncated or cut, "bytes_processed": result.bytes}

    @connector.tool
    async def bq_write(sql: str, *, grant: Grant) -> dict[str, Any]:
        """Run one statement that creates, fills, changes or drops one of this session's tables in the workspace dataset (CREATE TABLE AS SELECT, INSERT, MERGE, DELETE, DROP TABLE, ...)."""
        scope = _scope(grant)
        plan = check_plan(await warehouse.plan(scope.agent_id, check_sql(sql, write=True)), scope, project, write=True)
        await workspace.check(plan, scope, grant)
        result = await warehouse.run(scope.agent_id, sql, labels=_labels(grant), max_rows=0)
        await workspace.claim(plan.writes, scope, grant)
        return {"statement": plan.statement_type, "affected_rows": result.affected, "bytes_processed": result.bytes}

    @connector.tool
    async def bq_insert_rows(table: str, rows: list[dict[str, Any]], *, grant: Grant) -> dict[str, Any]:
        """Append rows (JSON objects, at most 1000 per call) to one of this session's workspace tables, creating it from their shape when it does not exist."""
        scope = _scope(grant)
        if not scope.workspace:
            raise Forbidden("the definition has no workspace")
        if not TABLE_ID.match(table):
            raise Forbidden("table is a plain table name inside the workspace")
        if not rows or len(rows) > MAX_INSERT_ROWS:
            raise Forbidden(f"between 1 and {MAX_INSERT_ROWS} rows per call")
        if len(json.dumps(rows, separators=(",", ":"))) > MAX_INSERT_BYTES:
            raise Forbidden(f"rows exceed {MAX_INSERT_BYTES} bytes")
        target = Table(project=project, dataset=scope.workspace, table=table)
        await workspace.check(Plan(statement_type="INSERT", reads=(), writes=(target,), bytes=0), scope, grant)
        loaded = await warehouse.load(scope.agent_id, target, rows, labels=_labels(grant))
        await workspace.claim((target,), scope, grant)
        return {"table": str(target), "inserted_rows": loaded}


def build_from_env(name: str) -> Any:
    settings = ConnectorSettings.from_env()
    check = ApiPermissionCheck(settings.api_url, identity=GoogleIdentity())
    if name == "egress":
        return egress(check).app()
    data = GcsDataFiles(settings.data_bucket) if settings.data_bucket else None
    warehouse = BigQueryWarehouse(settings.data_project, settings.workspace_service_accounts) if settings.data_project else None
    return internal(check, data=data, warehouse=warehouse).app()
