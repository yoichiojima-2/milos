from __future__ import annotations

from typing import Any

import httpx
import pytest

from milos import connector
from milos.errors import Forbidden
from milos.models import DataScope, PermissionLookup
from milos.warehouse import Plan, Table, TableInfo

from .fakes import FakeWarehouse

SCOPE = DataScope(agent_id="analyst", datasets=["weekly_numbers"], workspace="agent_analyst")


class Check:
    def __init__(self, allow: bool, scope: DataScope | None = None) -> None:
        self.allow = allow
        self.scope = scope
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def permitted(self, session_token: str, tool_name: str, args: dict[str, Any]) -> PermissionLookup:
        self.calls.append((session_token, tool_name, args))
        if not self.allow:
            return PermissionLookup(permitted=False)
        return PermissionLookup(permitted=True, tool_use_id="toolu_01ABC", scope=self.scope)


class Ctx:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


async def test_tool_runs_only_with_a_permission():
    check = Check(allow=False)
    c = connector.Connector("egress", check)

    calls = []

    @c.tool
    async def echo(text: str) -> str:
        """echo"""
        calls.append(text)
        return text

    guarded = c.mcp._tool_manager.get_tool("echo").fn
    with pytest.raises(Forbidden):
        await guarded(Ctx({"x-milos-session": "sess_1.sig"}), text="hi")
    assert check.calls == [("sess_1.sig", "mcp__egress__echo", {"text": "hi"})] and calls == []

    check.allow = True
    assert await guarded(Ctx({"x-milos-session": "sess_1.sig"}), text="hi") == "hi"
    with pytest.raises(Forbidden):
        await guarded(Ctx({}), text="no session header")


def test_check_url_rejects_http_and_private_hosts(monkeypatch):
    with pytest.raises(Forbidden):
        connector.check_url("http://example.com/")
    with pytest.raises(Forbidden):
        connector.check_url("https://metadata.google.internal/computeMetadata/v1/")
    monkeypatch.setattr(
        connector.socket,
        "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("10.0.0.5", 443))],
    )
    with pytest.raises(Forbidden):
        connector.check_url("https://intranet.example.com/")
    with pytest.raises(Forbidden):
        connector.check_url("https://example.com/" + "a" * 3000)


async def test_fetch_follows_checked_redirects_and_truncates(monkeypatch):
    monkeypatch.setattr(
        connector.socket,
        "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(connector, "MAX_RESPONSE_BYTES", 5)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://example.com/end"})
        return httpx.Response(200, text="0123456789")

    body = await connector.fetch("https://example.com/start", transport=httpx.MockTransport(handler))
    assert body == "01234"


async def test_api_permission_check_calls_internal_api():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["session"] = request.headers["x-milos-session"]
        return httpx.Response(200, json={"permitted": True, "tool_use_id": "t1"})

    check = connector.ApiPermissionCheck("http://internal")
    check._http = httpx.AsyncClient(base_url="http://internal", transport=httpx.MockTransport(handler))
    found = await check.permitted("sess_1.sig", "mcp__egress__web_fetch", {"url": "https://x"})
    assert found == PermissionLookup(permitted=True, tool_use_id="t1")
    assert seen["path"] == "/internal/sessions/sess_1/permissions"
    assert seen["params"]["tool_name"] == "mcp__egress__web_fetch" and seen["session"] == "sess_1.sig"


def test_mcp_url_appends_the_transport_path_once():
    from milos.runner import mcp_url

    assert mcp_url("https://egress.run.app") == "https://egress.run.app/mcp"
    assert mcp_url("https://egress.run.app/") == "https://egress.run.app/mcp"
    assert mcp_url("http://localhost:8080/mcp") == "http://localhost:8080/mcp"


async def test_runner_connector_url_reaches_the_mcp_transport():
    from milos.runner import Gate, build_options
    from milos.settings import RunnerSettings

    from .conftest import definition

    settings = RunnerSettings(
        session_id="test",
        session_token="test.sig",
        lease_token="lease",
        api_url="http://api",
        project="test",
        connector_urls={"egress": "http://egress"},
    )
    options = build_options(
        definition(connectors=["egress"]),
        settings,
        Gate(None),
        resume=None,
        connector_headers={"egress": {"Authorization": "Bearer identity"}},
    )
    config = options.mcp_servers["egress"]
    app = connector.egress(Check(allow=True)).app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        response = await client.post(
            config["url"],
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "deployment-test", "version": "1"},
                },
            },
        )
    assert response.status_code == 200 and '"serverInfo"' in response.text


class Files:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    async def list(self, prefix: str) -> list[str]:
        return sorted(name for name in self.files if name.startswith(prefix))

    async def read(self, path: str) -> bytes | None:
        return self.files.get(path)


async def test_internal_data_tools_read_only_inside_the_bucket():
    check = Check(allow=True)
    c = connector.internal(check, data=Files({"weekly/2026-W36.csv": b"week,revenue\n2026-W36,10\n"}))
    tools = c.mcp._tool_manager
    ctx = Ctx({"x-milos-session": "sess_1.sig"})

    assert await tools.get_tool("list_files").fn(ctx, prefix="weekly/") == ["weekly/2026-W36.csv"]
    assert (await tools.get_tool("read_file").fn(ctx, path="weekly/2026-W36.csv")).startswith("week,revenue")
    assert check.calls[0][1:] == ("mcp__internal__list_files", {"prefix": "weekly/"})
    with pytest.raises(Forbidden):
        await tools.get_tool("read_file").fn(ctx, path="../secrets")
    with pytest.raises(Forbidden):
        await tools.get_tool("read_file").fn(ctx, path="weekly/missing.csv")


def test_internal_without_a_bucket_has_no_tools():
    assert connector.internal(Check(allow=True)).mcp._tool_manager.list_tools() == []


# --- BigQuery ------------------------------------------------------------------------


def table(dataset: str, name: str = "t", project: str = "data") -> Table:
    return Table(project=project, dataset=dataset, table=name)


def plan(kind: str = "SELECT", reads: tuple[Table, ...] = (), writes: tuple[Table, ...] = (), size: int = 10) -> Plan:
    return Plan(statement_type=kind, reads=reads, writes=writes, bytes=size)


PLANS = {
    "select shared": plan(reads=(table("weekly_numbers"), table("agent_analyst", "notes"))),
    "select other agent": plan(reads=(table("agent_other"),)),
    "select other project": plan(reads=(table("weekly_numbers", project="elsewhere"),)),
    "select into": plan(reads=(table("weekly_numbers"),), writes=(table("agent_analyst"),)),
    "select too much": plan(reads=(table("weekly_numbers"),), size=10**12),
    "insert via query": plan("INSERT", reads=(table("agent_analyst"),), writes=(table("agent_analyst"),)),
    "ctas": plan("CREATE_TABLE_AS_SELECT", reads=(table("weekly_numbers"),), writes=(table("agent_analyst", "summary"),)),
    "ctas into shared": plan("CREATE_TABLE_AS_SELECT", reads=(table("weekly_numbers"),), writes=(table("weekly_numbers"),)),
    "drop": plan("DROP_TABLE", writes=(table("agent_analyst", "summary"),)),
    "script": plan("SCRIPT"),
    "create schema": plan("CREATE_SCHEMA"),
    "alter expiration_timestamp": plan("ALTER_TABLE", writes=(table("agent_analyst"),)),
    # session ownership inside the workspace
    "select theirs": plan(reads=(table("agent_analyst", "theirs"),)),
    "select missing": plan(reads=(table("agent_analyst", "missing"),)),
    "replace theirs": plan(
        "CREATE_TABLE_AS_SELECT", reads=(table("weekly_numbers"),), writes=(table("agent_analyst", "theirs"),)
    ),
    "insert mine": plan("INSERT", reads=(table("agent_analyst", "notes"),), writes=(table("agent_analyst", "notes"),)),
    "replace seeded": plan(
        "CREATE_TABLE_AS_SELECT", reads=(table("weekly_numbers"),), writes=(table("agent_analyst", "seeded"),)
    ),
}


MINE = {"milos_session": "sess_1"}
THEIRS = {"milos_session": "sess_2"}


def bigquery(scope: DataScope | None = SCOPE, session: str = "sess_1", **kwargs: Any):
    warehouse = FakeWarehouse(plans=PLANS, **kwargs)
    # tables that already exist in the workspace: one of this session's, one of another's
    warehouse.table_labels = {"data.agent_analyst.notes": dict(MINE), "data.agent_analyst.theirs": dict(THEIRS)}
    c = connector.internal(Check(allow=True, scope=scope), warehouse=warehouse)
    return warehouse, c.mcp._tool_manager, Ctx({"x-milos-session": f"{session}.sig"})


def test_bigquery_tools_hide_the_grant_from_their_schema():
    _, tools, _ = bigquery()
    assert sorted(t.name for t in tools.list_tools()) == ["bq_insert_rows", "bq_query", "bq_tables", "bq_write"]
    assert set(tools.get_tool("bq_query").parameters["properties"]) == {"sql"}


async def test_bq_query_runs_only_selects_inside_the_scope():
    warehouse, tools, ctx = bigquery(rows=[{"week": "2026-W36", "revenue": 10}])
    query = tools.get_tool("bq_query").fn

    result = await query(ctx, sql="select shared")
    assert result == {"rows": [{"week": "2026-W36", "revenue": 10}], "row_count": 1, "truncated": False, "bytes_processed": 10}
    assert warehouse.jobs[0]["agent_id"] == "analyst"
    assert warehouse.jobs[0]["labels"] == {"milos_session": "sess_1", "milos_tool_use": "toolu_01abc", "milos_agent": "analyst"}

    for sql in ("select other agent", "select other project", "select into", "select too much", "insert via query", "script"):
        with pytest.raises(Forbidden):
            await query(ctx, sql=sql)
    assert len(warehouse.jobs) == 1  # a refused statement never runs


async def test_bq_query_bounds_what_comes_back():
    rows = [{"n": i} for i in range(2000)]
    warehouse, tools, ctx = bigquery(rows=rows)
    result = await tools.get_tool("bq_query").fn(ctx, sql="select shared")
    assert result["row_count"] == 1000 and result["truncated"] is True

    kept, cut = connector.bounded_rows([{"text": "x" * 100} for _ in range(10)], limit=500)
    assert cut and 0 < len(kept) < 10


async def test_bq_write_targets_only_the_workspace():
    warehouse, tools, ctx = bigquery()
    write = tools.get_tool("bq_write").fn

    assert await write(ctx, sql="ctas") == {"statement": "CREATE_TABLE_AS_SELECT", "affected_rows": 0, "bytes_processed": 10}
    assert await write(ctx, sql="drop") == {"statement": "DROP_TABLE", "affected_rows": 0, "bytes_processed": 10}
    for sql in ("ctas into shared", "select shared", "script", "create schema", "alter expiration_timestamp", " "):
        with pytest.raises(Forbidden):
            await write(ctx, sql=sql)
    assert [j["sql"] for j in warehouse.jobs] == ["ctas", "drop"]


async def test_bq_insert_rows_appends_inside_the_workspace():
    warehouse, tools, ctx = bigquery()
    insert = tools.get_tool("bq_insert_rows").fn
    rows = [{"week": "2026-W36", "revenue": 10}]

    assert await insert(ctx, table="weekly", rows=rows) == {"table": "data.agent_analyst.weekly", "inserted_rows": 1}
    assert warehouse.loads[0]["table"] == Table(project="data", dataset="agent_analyst", table="weekly")
    with pytest.raises(Forbidden):
        await insert(ctx, table="weekly_numbers.sales", rows=rows)
    with pytest.raises(Forbidden):
        await insert(ctx, table="weekly", rows=[])
    with pytest.raises(Forbidden):
        await insert(ctx, table="weekly", rows=rows * 1001)
    assert len(warehouse.loads) == 1


async def test_bq_tables_lists_reachable_datasets_only():
    listing = {"weekly_numbers": [TableInfo(table="sales", rows=52, columns=["week STRING", "revenue INTEGER"])]}
    _, tools, ctx = bigquery(tables=listing)
    found = await tools.get_tool("bq_tables").fn(ctx, dataset="weekly_numbers")
    assert found == [{"table": "sales", "rows": 52, "columns": ["week STRING", "revenue INTEGER"]}]
    with pytest.raises(Forbidden):
        await tools.get_tool("bq_tables").fn(ctx, dataset="agent_other")


async def test_workspace_tables_belong_to_the_session_that_created_them():
    warehouse, tools, ctx = bigquery()
    query, write = tools.get_tool("bq_query").fn, tools.get_tool("bq_write").fn

    # a new table is claimed for this session once the statement created it
    await write(ctx, sql="ctas")
    assert warehouse.table_labels["data.agent_analyst.summary"] == MINE
    await write(ctx, sql="insert mine")  # one's own table may be changed
    assert [j["sql"] for j in warehouse.jobs] == ["ctas", "insert mine"]
    await write(ctx, sql="drop")  # and dropped, after which the name is free again
    assert "data.agent_analyst.summary" not in warehouse.table_labels

    # a table nobody created through milos (seeded by hand, or before ownership existed) is nobody's
    warehouse.table_labels["data.agent_analyst.seeded"] = {}
    with pytest.raises(Forbidden):
        await write(ctx, sql="replace seeded")

    # another session's table can be neither read nor replaced; a missing table cannot be read
    for tool, sql in ((query, "select theirs"), (write, "replace theirs"), (query, "select missing")):
        with pytest.raises(Forbidden):
            await tool(ctx, sql=sql)
    assert len(warehouse.jobs) == 3
    assert warehouse.table_labels["data.agent_analyst.theirs"] == THEIRS

    # the other session sees its own table and not this one's
    _, tools2, ctx2 = bigquery(session="sess_2")
    with pytest.raises(Forbidden):
        await tools2.get_tool("bq_query").fn(ctx2, sql="select shared")  # reads agent_analyst.notes, owned by sess_1
    await tools2.get_tool("bq_query").fn(ctx2, sql="select theirs")


async def test_bq_tables_lists_only_the_sessions_workspace_tables():
    listing = {
        "agent_analyst": [
            TableInfo(table="notes", rows=1, columns=["a STRING"], labels=MINE),
            TableInfo(table="theirs", rows=1, columns=["a STRING"], labels=THEIRS),
            TableInfo(table="seeded", rows=1, columns=["a STRING"]),
        ],
        "weekly_numbers": [TableInfo(table="sales", rows=52, columns=["week STRING"])],
    }
    _, tools, ctx = bigquery(tables=listing)
    mine = await tools.get_tool("bq_tables").fn(ctx, dataset="agent_analyst")
    assert mine == [{"table": "notes", "rows": 1, "columns": ["a STRING"]}]
    shared = await tools.get_tool("bq_tables").fn(ctx, dataset="weekly_numbers")
    assert [t["table"] for t in shared] == ["sales"]


async def test_bq_insert_rows_respects_session_ownership():
    warehouse, tools, ctx = bigquery()
    insert = tools.get_tool("bq_insert_rows").fn
    rows = [{"a": 1}]
    with pytest.raises(Forbidden):
        await insert(ctx, table="theirs", rows=rows)
    assert await insert(ctx, table="notes", rows=rows) == {"table": "data.agent_analyst.notes", "inserted_rows": 1}
    await insert(ctx, table="fresh", rows=rows)
    assert warehouse.table_labels["data.agent_analyst.fresh"] == MINE
    assert [load["table"].table for load in warehouse.loads] == ["notes", "fresh"]


async def test_bigquery_tools_without_a_scope_are_refused():
    warehouse, tools, ctx = bigquery(scope=None)
    with pytest.raises(Forbidden):
        await tools.get_tool("bq_query").fn(ctx, sql="select shared")
    read_only = DataScope(agent_id="analyst", datasets=["weekly_numbers"], workspace=None)
    warehouse, tools, ctx = bigquery(scope=read_only)
    with pytest.raises(Forbidden):
        await tools.get_tool("bq_insert_rows").fn(ctx, table="t", rows=[{"a": 1}])
    with pytest.raises(Forbidden):
        await tools.get_tool("bq_write").fn(ctx, sql="ctas")
    assert warehouse.jobs == [] and warehouse.loads == []
