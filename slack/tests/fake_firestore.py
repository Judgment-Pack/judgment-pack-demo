"""An in-memory stand-in for the slice of the Firestore client this app uses.

Deliberately small: `collection()`, `document()`, `get()`, `set()` (with and
without `merge`), `delete()`, `stream()`, and a transaction object that reads
and writes through the same dict.

What it proves and what it does not, stated so nobody mistakes one for the
other: it proves this app's LOGIC — what is written, what is read back, what a
lease does when it is already held. It does not prove atomicity, retries, or
the TTL policy; those are the library's and the service's, and they are
exercised only against a real Firestore. `bot/store.py` calls the library's
`transactional` decorator when the library is installed, and calls the bare
function here, which is exactly the seam this fake sits in.
"""

from __future__ import annotations

import copy
import threading


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
        if self._client.fail_on and self._client.fail_on in ("get", "all"):
            raise RuntimeError("fake firestore: reads are failing")
        with self._client.lock:
            return FakeSnapshot(self._client.data.get(self._collection, {}).get(self.id))

    def set(self, document, merge=False):
        self._client.writes += 1
        if self._client.fail_on and self._client.fail_on in ("set", "all"):
            raise RuntimeError("fake firestore: writes are failing")
        with self._client.lock:
            bucket = self._client.data.setdefault(self._collection, {})
            if merge and self.id in bucket:
                bucket[self.id].update(copy.deepcopy(document))
            else:
                bucket[self.id] = copy.deepcopy(document)

    def delete(self):
        with self._client.lock:
            self._client.data.get(self._collection, {}).pop(self.id, None)


class FakeCollection:
    def __init__(self, client, name):
        self._client = client
        self._name = name

    def document(self, doc_id):
        return FakeDocument(self._client, self._name, doc_id)

    def stream(self):
        if self._client.fail_on and self._client.fail_on in ("stream", "all"):
            raise RuntimeError("fake firestore: listing is failing")
        with self._client.lock:
            documents = list(self._client.data.get(self._name, {}).values())
        return [FakeSnapshot(document) for document in documents]


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

    def __init__(self, data=None, fail_on=None):
        self.data = data or {}
        self.lock = threading.RLock()
        self.reads = 0
        self.writes = 0
        # "get" | "set" | "stream" | "all" — makes a call raise, for the
        # degrade-to-memory paths.
        self.fail_on = fail_on

    def collection(self, name):
        return FakeCollection(self, name)

    def transaction(self):
        return FakeTransaction(self)

    # --- test helpers ------------------------------------------------------

    def documents(self, collection):
        return copy.deepcopy(self.data.get(collection, {}))

    def wipe_local(self):
        """Nothing: the point of the fake is that its data OUTLIVES a process."""
        return self
