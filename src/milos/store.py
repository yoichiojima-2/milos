"""A small transactional document store over Firestore.

The platform's invariants live in `service.py`; this module only provides the
primitives they need: read a document, run a query, and commit a set of writes
atomically with the reads that justified them. `create` fails if the document
exists, which is how create-only collections (permissions, approvals,
requests) are enforced at the storage layer rather than by convention.

Every read inside a transaction happens before any write, which is Firestore's
rule too. The in-memory fake in `tests/fakes.py` implements the same protocol,
so the invariants are tested without credentials; the Firestore implementation
is exercised against the emulator (`tests/test_store_emulator.py`).
"""

from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum
from functools import cache
from types import SimpleNamespace
from typing import Any, Literal, Protocol

from .errors import AlreadyExists, Conflict

type Op = Literal["==", "in", "array_contains", "<", "<=", ">", ">="]
type Filter = tuple[str, Op, Any]  # (field, op, value); enum values are stored as their strings


class Reader(Protocol):
    async def get(self, path: str) -> dict[str, Any] | None: ...

    async def query(
        self,
        collection: str,
        *,
        where: Sequence[Filter] = (),
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...


class Transaction(Reader, Protocol):
    """Reads see the committed state; writes are buffered until commit."""

    def create(self, path: str, data: dict[str, Any]) -> None: ...

    def set(self, path: str, data: dict[str, Any]) -> None: ...

    def update(self, path: str, fields: dict[str, Any]) -> None: ...


class Store(Reader, Protocol):
    async def transaction[T](self, fn: Callable[[Transaction], Awaitable[T]]) -> T: ...


# --- Firestore --------------------------------------------------------------


@cache
def _lib() -> SimpleNamespace:
    # Deferred: google-cloud-firestore is only needed on Cloud Run and against the emulator.
    from google.api_core import exceptions
    from google.cloud.firestore import AsyncClient, async_transactional
    from google.cloud.firestore_v1 import FieldFilter
    from google.cloud.firestore_v1.query import Query

    return SimpleNamespace(
        AsyncClient=AsyncClient,
        transactional=async_transactional,
        FieldFilter=FieldFilter,
        Query=Query,
        AlreadyExists=exceptions.AlreadyExists,
        Aborted=exceptions.Aborted,
    )


def _split(path: str) -> list[str]:
    parts = path.strip("/").split("/")
    if len(parts) % 2:
        raise ValueError(f"{path!r} is not a document path")
    return parts


def _value(value: Any) -> Any:
    """Filter values as Firestore stores them: enums as strings, sequences of them as lists."""
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, list | tuple | frozenset | set):
        return [_value(v) for v in value]
    return value


async def _query(
    client: Any,
    transaction: Any,
    collection: str,
    *,
    where: Sequence[Filter] = (),
    order_by: str | None = None,
    descending: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    lib = _lib()
    q = client.collection(*collection.strip("/").split("/"))
    for field, op, value in where:
        q = q.where(filter=lib.FieldFilter(field, op, _value(value)))
    if order_by:
        q = q.order_by(order_by, direction=lib.Query.DESCENDING if descending else lib.Query.ASCENDING)
    if limit is not None:
        q = q.limit(limit)
    return [doc.to_dict() async for doc in q.stream(transaction=transaction)]


class FirestoreTransaction:
    def __init__(self, client: Any, transaction: Any) -> None:
        self._client = client
        self._tx = transaction

    async def get(self, path: str) -> dict[str, Any] | None:
        snapshot = await self._client.document(*_split(path)).get(transaction=self._tx)
        return snapshot.to_dict() if snapshot.exists else None

    async def query(self, collection: str, **kwargs: Any) -> list[dict[str, Any]]:
        return await _query(self._client, self._tx, collection, **kwargs)

    def create(self, path: str, data: dict[str, Any]) -> None:
        self._tx.create(self._client.document(*_split(path)), data)

    def set(self, path: str, data: dict[str, Any]) -> None:
        self._tx.set(self._client.document(*_split(path)), data)

    def update(self, path: str, fields: dict[str, Any]) -> None:
        self._tx.update(self._client.document(*_split(path)), fields)


class FirestoreStore:
    """Implements `Store` on google-cloud-firestore's async client."""

    def __init__(self, client: Any | None = None, *, project: str | None = None) -> None:
        self._client = client if client is not None else _lib().AsyncClient(project=project)

    async def get(self, path: str) -> dict[str, Any] | None:
        snapshot = await self._client.document(*_split(path)).get()
        return snapshot.to_dict() if snapshot.exists else None

    async def query(self, collection: str, **kwargs: Any) -> list[dict[str, Any]]:
        return await _query(self._client, None, collection, **kwargs)

    async def transaction[T](self, fn: Callable[[Transaction], Awaitable[T]]) -> T:
        lib = _lib()

        async def run(tx: Any) -> T:
            return await fn(FirestoreTransaction(self._client, tx))

        transactional: Callable[[Any], Awaitable[T]] = lib.transactional(run)
        try:
            return await transactional(self._client.transaction())
        except lib.AlreadyExists as error:
            raise AlreadyExists(str(error)) from error
        except lib.Aborted as error:  # contention beyond the client's retries
            raise Conflict(f"transaction aborted: {error}") from error
