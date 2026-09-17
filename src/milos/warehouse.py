"""BigQuery for the internal connector: the agent's workspace and the datasets it may read.

The connector never runs SQL as itself. It impersonates the agent's workspace
service account (`milos-workspace-<agent>`), the only principal with
`dataEditor` on `agent_<id>` and `dataViewer` on the datasets the definition
names, so IAM is the barrier between agents even if this code is wrong. On top
of that every statement is dry-run first, and `check_plan` refuses one that
touches a table outside the scope the API returned with the permission, writes
outside the workspace, or would scan more than the cap. Every job carries the
session and tool use id as labels, so BigQuery's own audit log joins the
journal.

`Warehouse` is the adapter protocol; `BigQueryWarehouse` is the real one and
`tests/fakes.py` has the fake.
"""

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .errors import Forbidden
from .models import DataScope

MAX_QUERY_BYTES = 1_000_000_000  # scanned per statement; also set as the job's maximum_bytes_billed
MAX_ROWS = 1_000  # rows a query returns
MAX_SQL_CHARS = 20_000
QUERY_TIMEOUT = 120  # seconds a statement may run
BIGQUERY_SCOPE = "https://www.googleapis.com/auth/bigquery"

READ_STATEMENTS = frozenset({"SELECT"})
WRITE_STATEMENTS = frozenset(
    {
        "CREATE_TABLE",
        "CREATE_TABLE_AS_SELECT",
        "CREATE_VIEW",
        "DROP_TABLE",
        "DROP_VIEW",
        "ALTER_TABLE",
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "TRUNCATE_TABLE",
    }
)
# Retention is a rule of the dataset, not a choice of the agent: a statement that
# names an expiration (table, partition or view options) is refused outright.
EXPIRATION = re.compile(r"expiration", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Table:
    project: str
    dataset: str
    table: str

    def __str__(self) -> str:
        return f"{self.project}.{self.dataset}.{self.table}"


@dataclass(frozen=True, slots=True)
class Plan:
    """What a dry run says a statement would do."""

    statement_type: str  # BigQuery's statementType: SELECT, CREATE_TABLE_AS_SELECT, INSERT, ...; "" when unknown
    reads: tuple[Table, ...]  # referenced tables
    writes: tuple[Table, ...]  # the DDL target or DML destination
    bytes: int  # estimated bytes processed


@dataclass(frozen=True, slots=True)
class Rows:
    rows: list[dict[str, Any]]
    affected: int | None  # rows a DML statement changed
    bytes: int  # bytes processed
    truncated: bool  # more rows existed than were returned


@dataclass(frozen=True, slots=True)
class TableInfo:
    table: str
    rows: int | None
    columns: list[str]  # "name TYPE"


class Warehouse(Protocol):
    """One BigQuery data project, reached as one workspace identity per agent."""

    @property
    def project(self) -> str: ...

    async def plan(self, agent_id: str, sql: str) -> Plan: ...

    async def run(self, agent_id: str, sql: str, *, labels: dict[str, str], max_rows: int) -> Rows: ...

    async def load(self, agent_id: str, table: Table, rows: list[dict[str, Any]], *, labels: dict[str, str]) -> int: ...

    async def tables(self, agent_id: str, dataset: str) -> list[TableInfo]: ...


def check_sql(sql: str, *, write: bool = False) -> str:
    if not sql.strip():
        raise Forbidden("empty statement")
    if len(sql) > MAX_SQL_CHARS:
        raise Forbidden(f"statement longer than {MAX_SQL_CHARS} characters")
    if write and EXPIRATION.search(sql):
        raise Forbidden("table expiration is set by the dataset, not by the agent")
    return sql


def check_plan(plan: Plan, scope: DataScope, project: str, *, write: bool) -> Plan:
    """Refuse a statement before it runs: wrong kind, a table outside the scope, a write outside the workspace, too many bytes."""
    tool = "bq_write" if write else "bq_query"
    allowed = WRITE_STATEMENTS if write else READ_STATEMENTS
    if plan.statement_type not in allowed:
        raise Forbidden(f"{tool} does not run {plan.statement_type or 'this kind of'} statements")
    readable = scope.readable()
    for table in (*plan.reads, *plan.writes):
        if table.project != project or table.dataset not in readable:
            raise Forbidden(f"{table} is outside the agent's datasets")
    if not write and plan.writes:
        raise Forbidden(f"{tool} does not write")
    for table in plan.writes:
        if table.dataset != scope.workspace:
            raise Forbidden(f"{table} is outside the workspace {scope.workspace}")
    if plan.bytes > MAX_QUERY_BYTES:
        raise Forbidden(f"statement would scan {plan.bytes} bytes; the cap is {MAX_QUERY_BYTES}")
    return plan


def label(value: str) -> str:
    """A BigQuery label value: lowercase letters, digits, `_` and `-`, at most 63 characters."""
    return re.sub(r"[^a-z0-9_-]", "_", value.lower())[:63]


def _plain(value: Any) -> Any:
    """Row values as JSON: dates, decimals and bytes become strings."""
    return json.loads(json.dumps(value, default=str))


class BigQueryWarehouse:
    """The real adapter. Jobs run in the data project under the agent's workspace identity."""

    def __init__(self, project: str, accounts: dict[str, str]) -> None:
        self._project = project
        self._accounts = accounts  # agent id -> workspace service account
        self._clients: dict[str, Any] = {}

    @property
    def project(self) -> str:
        return self._project

    def _client(self, agent_id: str) -> Any:
        if agent_id not in self._clients:
            # Deferred: google-cloud-bigquery is only needed on Cloud Run.
            import google.auth
            from google.auth import impersonated_credentials
            from google.cloud import bigquery

            account = self._accounts.get(agent_id)
            if not account:
                raise Forbidden(f"agent {agent_id} has no workspace identity")
            source, _ = google.auth.default()
            credentials = impersonated_credentials.Credentials(  # type: ignore[no-untyped-call]
                source_credentials=source, target_principal=account, target_scopes=[BIGQUERY_SCOPE]
            )
            self._clients[agent_id] = bigquery.Client(project=self._project, credentials=credentials)
        return self._clients[agent_id]

    async def plan(self, agent_id: str, sql: str) -> Plan:
        from google.cloud import bigquery

        def run() -> Plan:
            config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
            job = self._client(agent_id).query(sql, job_config=config)
            kind = job.statement_type or ""
            reads = tuple(_table(t) for t in job.referenced_tables or [])
            targets = [job.ddl_target_table, job.destination if kind != "SELECT" else None]
            writes = tuple(_table(t) for t in targets if t is not None)
            return Plan(statement_type=kind, reads=reads, writes=writes, bytes=int(job.total_bytes_processed or 0))

        return await asyncio.to_thread(run)

    async def run(self, agent_id: str, sql: str, *, labels: dict[str, str], max_rows: int) -> Rows:
        from google.cloud import bigquery

        def run() -> Rows:
            config = bigquery.QueryJobConfig(maximum_bytes_billed=MAX_QUERY_BYTES, labels=labels)
            job = self._client(agent_id).query(sql, job_config=config)
            rows = [_plain(dict(row)) for row in job.result(max_results=max_rows + 1, timeout=QUERY_TIMEOUT)]
            return Rows(
                rows=rows[:max_rows],
                affected=job.num_dml_affected_rows,
                bytes=int(job.total_bytes_processed or 0),
                truncated=len(rows) > max_rows,
            )

        return await asyncio.to_thread(run)

    async def load(self, agent_id: str, table: Table, rows: list[dict[str, Any]], *, labels: dict[str, str]) -> int:
        from google.cloud import bigquery

        def run() -> int:
            config = bigquery.LoadJobConfig(
                autodetect=True,
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
                schema_update_options=[bigquery.SchemaUpdateOption.ALLOW_FIELD_ADDITION],
                labels=labels,
            )
            job = self._client(agent_id).load_table_from_json(rows, str(table), job_config=config)
            job.result(timeout=QUERY_TIMEOUT)
            return int(job.output_rows or 0)

        return await asyncio.to_thread(run)

    async def tables(self, agent_id: str, dataset: str) -> list[TableInfo]:
        def run() -> list[TableInfo]:
            client = self._client(agent_id)
            found = []
            for item in client.list_tables(f"{self._project}.{dataset}", max_results=MAX_ROWS):
                table = client.get_table(item.reference)
                columns = [f"{field.name} {field.field_type}" for field in table.schema]
                found.append(TableInfo(table=table.table_id, rows=table.num_rows, columns=columns))
            return found

        return await asyncio.to_thread(run)


def _table(ref: Any) -> Table:
    return Table(project=ref.project, dataset=ref.dataset_id, table=ref.table_id)
