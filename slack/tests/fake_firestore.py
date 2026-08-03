"""An in-memory stand-in for the slice of the Firestore client this app uses.

Deliberately small: `collection()`, `document()`, `get()`, `set()` (with and
without `merge`), `create()`, `delete()`, `stream()`, a `where()`/`limit()`
query, and a transaction object that reads and writes through the same dict.

What it proves and what it does not, stated so nobody mistakes one for the
other: it proves this app's LOGIC — what is written, what is read back, which
of two concurrent deliveries wins a create, what a lease does when it is
already held. It does not prove atomicity across a network, retries, or the
TTL policy; those are the library's and the service's, and they are exercised
only against a real Firestore. `bot/store.py` calls the library's
`transactional` decorator when the library is installed and the bare function
here, which is exactly the seam this fake sits in.

`create()` is modelled faithfully in the one respect that matters: it is
atomic under this object's lock and raises `AlreadyExists` for the loser. That
is the property de-duplication depends on, and the reason a `get` followed by
a `set` was not good enough — two deliveries of one Slack event both read
"not there" and both ran.

The counters (`reads`, `writes`, `creates`, `streams`, `queries`) exist so
tests can assert cost: a turn reads one document and writes one, and nothing
on the turn path streams a collection.
"""

from __future__ import annotations

import copy
import threading
import time


class InvalidArgument(Exception):
    """The service refused the request itself — a reserved id, a bad field."""


class AlreadyExists(Exception):
    """The name the real client's google.api_core exception carries."""


class FakeSnapshot:
    def __init__(self, data):
        self._data = copy.deepcopy(data) if data is not None else None

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return copy.deepcopy(self._data)


class FakeDocument:
    def __init__(self, client, collection, doc_id):
        self._client = client
        self._collection = collection
        self.id = doc_id

    # The real client takes an optional transaction on a read inside one.
    def get(self, transaction=None):
        self._client.reads += 1
        self._client.fail("get")
        if self._client.get_latency:
            time.sleep(self._client.get_latency)
        with self._client.lock:
            return FakeSnapshot(self._client.data.get(self._collection, {}).get(self.id))

    def set(self, document, merge=False):
        self._client.writes += 1
        self._client.fail("set")
        with self._client.lock:
            bucket = self._client.data.setdefault(self._collection, {})
            if merge and self.id in bucket:
                bucket[self.id].update(copy.deepcopy(document))
            else:
                bucket[self.id] = copy.deepcopy(document)

    def create(self, document):
        """Atomic create-if-absent, like the real one: the loser raises."""
        self._client.creates += 1
        self._client.fail("create")
        with self._client.lock:
            bucket = self._client.data.setdefault(self._collection, {})
            if self.id in bucket:
                raise AlreadyExists("document {} already exists".format(self.id))
            bucket[self.id] = copy.deepcopy(document)

    def delete(self):
        with self._client.lock:
            self._client.data.get(self._collection, {}).pop(self.id, None)


OPERATORS = {
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
    "==": lambda a, b: a == b,
}


class FakeQuery:
    def __init__(self, client, name, predicates=(), limit=None):
        self._client = client
        self._name = name
        self._predicates = list(predicates)
        self._limit = limit

    def where(self, field=None, op=None, value=None, filter=None):  # noqa: A002
        if filter is not None:
            field = getattr(filter, "field_path", field)
            op = getattr(filter, "op_string", op)
            value = getattr(filter, "value", value)
        return FakeQuery(
            self._client, self._name, self._predicates + [(field, op, value)], self._limit
        )

    def limit(self, count):
        return FakeQuery(self._client, self._name, self._predicates, count)

    def stream(self):
        if self._predicates:
            self._client.queries += 1
        else:
            self._client.streams += 1
        self._client.fail("stream")
        with self._client.lock:
            documents = list(self._client.data.get(self._name, {}).values())
        matched = [
            document
            for document in documents
            if all(
                OPERATORS[op](document.get(field), value)
                for field, op, value in self._predicates
            )
        ]
        if self._limit is not None:
            matched = matched[: self._limit]
        self._client.reads += len(matched) if self._predicates else len(documents)
        return [FakeSnapshot(document) for document in matched]


class FakeCollection(FakeQuery):
    def document(self, doc_id):
        # The real service reserves ids of the form __name__ and refuses them
        # with InvalidArgument. The first deployed revision died on exactly
        # this — a probe named __healthcheck__ that every test accepted — so
        # the fake now enforces the one naming rule production enforces.
        if doc_id.startswith("__") and doc_id.endswith("__"):
            raise InvalidArgument(
                'Resource id "%s" is invalid because it is reserved.' % doc_id
            )
        return FakeDocument(self._client, self._name, doc_id)


class FakeTransaction:
    """Applies writes as they are made.

    The real one buffers and commits atomically; this one cannot, and the
    module docstring says so. It is enough to exercise the lease's decision.
    """

    def __init__(self, client):
        self._client = client

    def set(self, reference, document, merge=False):
        reference.set(document, merge=merge)

    def update(self, reference, document):
        reference.set(document, merge=True)

    def delete(self, reference):
        reference.delete()


class FakeFirestore:
    """The client object. `data` is public so tests can look inside."""

    def __init__(self, data=None, fail_on=None, get_latency=0.0):
        self.data = data or {}
        self.lock = threading.RLock()
        self.reads = 0
        self.writes = 0
        self.creates = 0
        self.streams = 0  # unfiltered scans: the thing a turn must never do
        self.queries = 0  # bounded where() clauses: what the sweeper does
        # "get" | "set" | "create" | "stream" | "all" — makes a call raise,
        # for the degrade-to-memory and refuse-to-boot paths.
        self.fail_on = fail_on
        # Stands in for a network round trip, so a race has a window to lose.
        self.get_latency = get_latency

    def fail(self, operation):
        if self.fail_on and self.fail_on in (operation, "all"):
            raise RuntimeError("fake firestore: {} is failing".format(operation))

    def collection(self, name):
        return FakeCollection(self, name)

    def transaction(self):
        return FakeTransaction(self)

    # --- test helpers ------------------------------------------------------

    def documents(self, collection):
        return copy.deepcopy(self.data.get(collection, {}))
