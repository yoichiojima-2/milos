"""In-memory doubles for every GCP dependency.

They implement the same protocols as the real adapters (`milos.store.Store`,
`milos.audit.AuditLog`, `milos.jobs.JobLauncher`, `milos.auth.Directory`,
`milos.snapshots.Blobs`, `milos.warehouse.Warehouse`) with just enough
semantics for the invariants to be testable: create-only documents,
transactions that roll back on error, and queries with the operators the
service uses.
"""

from __future__ import annotations

import copy
import fnmatch
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from milos.errors import AlreadyExists
from milos.warehouse import Plan, Rows, Table, TableInfo


def _matches(doc: dict[str, Any], where: Sequence[tuple[str, str, Any]]) -> bool:
    for field, op, value in where:
        actual = doc.get(field)
        if op == "==" and actual != value:
            return False
        if op == "in" and actual not in value:
            return False
        if op == "array_contains" and value not in (actual or []):
            return False
        if op == "<" and not (actual is not None and actual < value):
            return False
        if op == "<=" and not (actual is not None and actual <= value):
            return False
        if op == ">" and not (actual is not None and actual > value):
            return False
        if op == ">=" and not (actual is not None and actual >= value):
            return False
    return True


class FakeTransaction:
    def __init__(self, docs: dict[str, dict[str, Any]]) -> None:
        self._docs = docs
        self._writes: list[tuple[str, str, dict[str, Any]]] = []

    def _reading(self) -> None:
        # Firestore transactions read first, then write; a read after a write
        # raises ReadAfterWriteError on the real client.
        if self._writes:
            raise RuntimeError("read after write in transaction")

    async def get(self, path: str) -> dict[str, Any] | None:
        self._reading()
        doc = self._docs.get(path)
        return copy.deepcopy(doc) if doc is not None else None

    async def query(
        self,
        collection: str,
        *,
        where: Sequence[tuple[str, str, Any]] = (),
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self._reading()
        prefix = collection.strip("/") + "/"
        rows = [
            copy.deepcopy(doc)
            for path, doc in self._docs.items()
            if path.startswith(prefix) and "/" not in path[len(prefix) :] and _matches(doc, where)
        ]
        if order_by:
            rows.sort(key=lambda d: d.get(order_by), reverse=descending)
        return rows[:limit] if limit else rows

    def create(self, path: str, data: dict[str, Any]) -> None:
        self._writes.append(("create", path, copy.deepcopy(data)))

    def set(self, path: str, data: dict[str, Any]) -> None:
        self._writes.append(("set", path, copy.deepcopy(data)))

    def update(self, path: str, fields: dict[str, Any]) -> None:
        self._writes.append(("update", path, copy.deepcopy(fields)))

    def commit(self) -> None:
        for op, path, _ in self._writes:
            if op == "create" and path in self._docs:
                raise AlreadyExists(path)
            if op == "update" and path not in self._docs:
                raise KeyError(path)
        for op, path, data in self._writes:
            if op == "update":
                for key, value in data.items():
                    _set_path(self._docs[path], key, value)
            else:
                self._docs[path] = data


def _set_path(doc: dict[str, Any], key: str, value: Any) -> None:
    """Apply one dotted-path update the way Firestore's update() does."""
    parts = key.split(".")
    for part in parts[:-1]:
        doc = doc.setdefault(part, {})
    doc[parts[-1]] = value


class FakeStore:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.transactions = 0  # committed transactions, for tests that count them

    async def get(self, path: str) -> dict[str, Any] | None:
        return await FakeTransaction(self.docs).get(path)

    async def query(self, collection: str, **kwargs: Any) -> list[dict[str, Any]]:
        return await FakeTransaction(self.docs).query(collection, **kwargs)

    async def transaction[T](self, fn: Callable[[Any], Awaitable[T]]) -> T:
        tx = FakeTransaction(self.docs)
        result = await fn(tx)
        tx.commit()
        self.transactions += 1
        return result


class FakeAuditLog:
    def __init__(self, *, fail: bool = False) -> None:
        self.entries: list[dict[str, Any]] = []
        self.fail = fail

    def write(self, entry: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("audit sink unavailable")
        self.entries.append(entry)


class FakeJobs:
    def __init__(self) -> None:
        self.launched: list[dict[str, Any]] = []

    async def launch(self, session_id: str, *, agent_id: str, env: dict[str, str]) -> str:
        self.launched.append({"session_id": session_id, "agent_id": agent_id, "env": env})
        return f"exec-{len(self.launched)}"


class FakeDirectory:
    """Group membership by glob: {"analysts@example.com": ["*@example.com"]}."""

    def __init__(self, members: dict[str, list[str]] | None = None) -> None:
        self.members = members or {}

    async def is_member(self, email: str, group: str) -> bool:
        return any(fnmatch.fnmatch(email, pattern) for pattern in self.members.get(group, []))


class FakeBlobs:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, path: str, data: bytes) -> None:
        self.objects[path] = data

    async def get(self, path: str) -> bytes | None:
        return self.objects.get(path)


class FakeWarehouse:
    """Dry-run plans keyed by SQL text, the rows every query returns, and a record of every job and load."""

    def __init__(
        self,
        project: str = "data",
        *,
        plans: dict[str, Plan] | None = None,
        rows: list[dict[str, Any]] | None = None,
        tables: dict[str, list[TableInfo]] | None = None,
    ) -> None:
        self.project = project
        self.plans = plans or {}
        self.rows = rows or []
        self.table_lists = tables or {}
        self.table_labels: dict[str, dict[str, str]] = {}  # "project.dataset.table" -> labels, for tables that exist
        self.jobs: list[dict[str, Any]] = []
        self.loads: list[dict[str, Any]] = []

    async def plan(self, agent_id: str, sql: str) -> Plan:
        return self.plans[sql]

    async def run(self, agent_id: str, sql: str, *, labels: dict[str, str], max_rows: int) -> Rows:
        self.jobs.append({"agent_id": agent_id, "sql": sql, "labels": labels})
        plan = self.plans[sql]
        for table in plan.writes:  # what the statement does to the tables it targets
            if plan.statement_type.startswith("DROP"):
                self.table_labels.pop(str(table), None)
            else:
                self.table_labels.setdefault(str(table), {})
        affected = None if plan.statement_type == "SELECT" else len(self.rows)
        return Rows(rows=self.rows[:max_rows], affected=affected, bytes=plan.bytes, truncated=len(self.rows) > max_rows)

    async def load(self, agent_id: str, table: Table, rows: list[dict[str, Any]], *, labels: dict[str, str]) -> int:
        self.loads.append({"agent_id": agent_id, "table": table, "rows": rows, "labels": labels})
        self.table_labels.setdefault(str(table), {})
        return len(rows)

    async def tables(self, agent_id: str, dataset: str) -> list[TableInfo]:
        return self.table_lists.get(dataset, [])

    async def labels(self, agent_id: str, table: Table) -> dict[str, str] | None:
        found = self.table_labels.get(str(table))
        return dict(found) if found is not None else None

    async def claim(self, agent_id: str, table: Table, labels: dict[str, str]) -> None:
        if str(table) in self.table_labels:
            self.table_labels[str(table)].update(labels)
