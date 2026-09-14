"""A small transactional document store over Firestore.

The platform's invariants live in `service.py`; this module only provides the
primitives they need: read a document, run a query, and commit a set of writes
atomically with the reads that justified them. `create` fails if the document
exists, which is how create-only collections (permissions, approvals) are
enforced at the storage layer rather than by convention.

Every read inside a transaction happens before any write, which is Firestore's
rule too. The in-memory fake in `tests/fakes.py` implements the same protocol,
so the invariants are tested without credentials; the Firestore implementation
is exercised against the emulator (`FIRESTORE_EMULATOR_HOST`).
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from .errors import AlreadyExists

type Filter = tuple[str, str, Any]  # (field, op, value); ops: ==, in, array_contains, <, <=, >, >=


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


def _split(path: str) -> list[str]:
    parts = path.strip("/").split("/")
    if len(parts) % 2:
        raise ValueError(f"{path!r} is not a document path")
    return parts


class FirestoreTransaction:
    def __init__(self, client: Any, transaction: Any) -> None:
        self._client = client
        self._tx = transaction

    async def get(self, path: str) -> dict[str, Any] | None:
        snapshot = await self._client.document(*_split(path)).get(transaction=self._tx)
        return snapshot.to_dict() if snapshot.exists else None

    async def query(
        self,
        collection: str,
        *,
        where: Sequence[Filter] = (),
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        # Deferred here and below: google-cloud-firestore is only needed on Cloud Run.
        from google.cloud.firestore_v1 import FieldFilter
        from google.cloud.firestore_v1.query import Query

        q = self._client.collection(*collection.strip("/").split("/"))
        for field, op, value in where:
            q = q.where(filter=FieldFilter(field, op, value))
        if order_by:
            q = q.order_by(order_by, direction=Query.DESCENDING if descending else Query.ASCENDING)
        if limit:
            q = q.limit(limit)
        return [doc.to_dict() async for doc in q.stream(transaction=self._tx)]

    def create(self, path: str, data: dict[str, Any]) -> None:
        self._tx.create(self._client.document(*_split(path)), data)

    def set(self, path: str, data: dict[str, Any]) -> None:
        self._tx.set(self._client.document(*_split(path)), data)

    def update(self, path: str, fields: dict[str, Any]) -> None:
        self._tx.update(self._client.document(*_split(path)), fields)


class FirestoreStore:
    """Implements `Store` on google-cloud-firestore's async client."""

    def __init__(self, client: Any | None = None, *, project: str | None = None) -> None:
        if client is None:
            from google.cloud.firestore import AsyncClient

            client = AsyncClient(project=project)
        self._client = client

    async def get(self, path: str) -> dict[str, Any] | None:
        snapshot = await self._client.document(*_split(path)).get()
        return snapshot.to_dict() if snapshot.exists else None

    async def query(self, collection: str, **kwargs: Any) -> list[dict[str, Any]]:
        return await FirestoreTransaction(self._client, None).query(collection, **kwargs)

    async def transaction[T](self, fn: Callable[[Transaction], Awaitable[T]]) -> T:
        from google.api_core.exceptions import AlreadyExists as FirestoreAlreadyExists
        from google.cloud.firestore import async_transactional

        @async_transactional
        async def run(tx: Any) -> T:
            return await fn(FirestoreTransaction(self._client, tx))

        try:
            return await run(self._client.transaction())
        except FirestoreAlreadyExists as error:
            raise AlreadyExists(str(error)) from error
