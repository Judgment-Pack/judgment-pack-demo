"""Where session state lives — and, more importantly, what it can be.

THE SPLIT, which every other module in this package assumes:

    PERSISTED (one Firestore document per Slack user, or one dict entry in
    the memory backend) — *metadata about where somebody is in the demo*:
    user id, created_at / last_seen, active_flow, step, completed[],
    welcomed, a scratch_dir PATH HINT, expires_at (a real timestamp, so a
    Firestore TTL policy can do the deleting), the lease fields, and a
    JSON-safe subset of the flow-local `data`.

    PROCESS-LOCAL, never persisted, kept in a side table keyed by user id —
    *things that only mean anything inside one running container*: the
    threading.Lock that serializes a user's turns, the live Desk (a Popen,
    a port, a keypair on this filesystem), and anything else in `data` that
    will not survive a round trip through JSON.

What that buys, stated exactly: a user's PROGRESS survives a restart, a
redeploy, or an instance being replaced. It does not make the demo
multi-instance, because two things every flow needs are inherently local —
the scratch copy of the project that `jpack` subprocesses read off this
disk, and the live gateway process with its keypair and its store. Those are
instance-local and REBUILDABLE, which is what `bot/reconcile.py` is for.

A persisted `scratch_dir` is therefore a hint, not a promise: it names where
this user's project WAS. After a restart the directory is gone, and the
reconciler re-copies it before the next turn runs.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone

log = logging.getLogger("jpack-slack.store")

MEMORY = "memory"
FIRESTORE = "firestore"

# The members of a session that mean anything outside this process.
PERSISTED_FIELDS = (
    "user_id",
    "created_at",
    "last_seen",
    "active_flow",
    "step",
    "completed",
    "welcomed",
    "scratch_dir",
)

# Keys of `session.data` that are never persisted even when they would
# serialize: the live desk is a process handle, and a stale one read back
# after a restart would be a lie about a gateway that is not running.
LOCAL_ONLY_DATA = ("desk",)

# A persisted value has to be small as well as JSON-safe: a drafted pack is
# a few kilobytes, and Firestore's document limit is 1 MiB for everything.
MAX_DATA_BYTES = 200_000


class StateUnavailable(Exception):
    """The configured state backend cannot be reached.

    Raised at boot, where it is fatal: a demo that silently forgets is worse
    than one that will not start. Mid-run the callers catch it, log loudly,
    and degrade to this process's memory for that turn rather than dropping
    somebody's click.
    """


# --- the persisted projection ---------------------------------------------


def json_safe_data(data):
    """The subset of flow-local data that survives a round trip.

    Draft pack documents, the id of the pack that was registered, the newest
    run id, scalars a flow left behind. Everything else — a Popen, a socket,
    a Desk — is dropped here rather than at write time, so the rule lives in
    one readable place.
    """
    safe = {}
    for key, value in (data or {}).items():
        if key in LOCAL_ONLY_DATA or key.startswith("_"):
            continue
        try:
            encoded = json.dumps(value)
        except (TypeError, ValueError):
            continue
        if len(encoded) > MAX_DATA_BYTES:
            log.info("dropping session data %r from the persisted document: too large", key)
            continue
        safe[key] = json.loads(encoded)
    return safe


def to_document(session, ttl_seconds, holder=None, lease_seconds=0):
    """The persisted shape of one session."""
    expires_at = float(session.last_seen) + float(ttl_seconds)
    document = {
        "user_id": session.user_id,
        "created_at": float(session.created_at),
        "last_seen": float(session.last_seen),
        "active_flow": session.active_flow,
        "step": int(session.step),
        "completed": sorted(session.completed),
        "welcomed": bool(session.welcomed),
        # A hint about where this user's project was, not a promise that it
        # is still there. See the module docstring.
        "scratch_dir": session.scratch_dir,
        "data": json_safe_data(session.data),
        "expires_at_epoch": expires_at,
        # A real timestamp, because a Firestore TTL policy deletes on a
        # timestamp field and nothing else.
        "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc),
        "updated_at": datetime.fromtimestamp(float(session.last_seen), tz=timezone.utc),
    }
    if holder:
        document["lease_holder"] = holder
        document["lease_expires_at_epoch"] = float(session.last_seen) + float(lease_seconds)
    return document


def apply_document(session, document):
    """Load a persisted document into a live session object.

    The lock and the desk are NOT touched: they belong to this process, and
    the caller has just created or found them in the side table.
    """
    session.created_at = float(document.get("created_at") or time.time())
    session.last_seen = float(document.get("last_seen") or session.created_at)
    session.active_flow = document.get("active_flow")
    session.step = int(document.get("step") or 0)
    session.completed = set(document.get("completed") or [])
    session.welcomed = bool(document.get("welcomed"))
    session.scratch_dir = document.get("scratch_dir")
    data = document.get("data") or {}
    session.data.update({k: v for k, v in data.items() if k not in LOCAL_ONLY_DATA})
    return session


# --- the interface ---------------------------------------------------------


class StateStore:
    """What a state backend must do. Both implementations are below.

    Every method speaks in plain documents (dicts) — no Session objects, no
    locks, no processes. That is the seam: the thing on the other side may be
    a dict in this process or a collection in Firestore, and nothing above
    this line is allowed to care.
    """

    backend = MEMORY
    durable = False

    def load(self, user_id):
        """The persisted document for this user, or None."""
        raise NotImplementedError

    def save(self, document):
        """Write one document, whole."""
        raise NotImplementedError

    def delete(self, user_id):
        raise NotImplementedError

    def documents(self):
        """Every live document. Used by the sweeper and by diagnostics."""
        raise NotImplementedError

    def expired(self, now):
        """Documents whose expires_at has passed."""
        return [
            document
            for document in self.documents()
            if float(document.get("expires_at_epoch") or 0) <= now
        ]

    def overflow(self, cap):
        """Documents beyond the cap, least recently seen first."""
        documents = sorted(self.documents(), key=lambda d: float(d.get("last_seen") or 0))
        return documents[: max(0, len(documents) - cap)]

    def try_lease(self, user_id, holder, seconds, now=None):
        """Take (or renew) the turn lease for this user.

        A SEAM, not a guarantee, and nothing in the request path depends on
        it today: the single instance is serialized by a threading.Lock. It
        exists because a multi-instance version needs a cross-process claim,
        and the honest place to put one is here — beside the state it
        protects. See the multi-instance section of slack/DESIGN.md for what
        else that version would have to solve first.
        """
        return True

    def release_lease(self, user_id, holder):
        return True

    def healthcheck(self):
        """Raise StateUnavailable when this backend cannot be used."""
        return True

    def close(self):
        return None


# --- memory ----------------------------------------------------------------


class MemoryStore(StateStore):
    """Today's behavior, moved behind the interface and otherwise unchanged.

    An ordered dict of documents, least recently seen first. It is what the
    tests, the dry run, and any `docker run` with no configuration use, and
    it forgets everything when the process ends — which is precisely the
    limitation the Firestore backend removes.
    """

    backend = MEMORY
    durable = False

    def __init__(self, config=None):
        self._config = config
        self._documents = OrderedDict()
        self._lock = threading.RLock()

    def load(self, user_id):
        with self._lock:
            document = self._documents.get(user_id)
            return dict(document) if document else None

    def save(self, document):
        with self._lock:
            user_id = document["user_id"]
            self._documents[user_id] = dict(document)
            self._documents.move_to_end(user_id)

    def delete(self, user_id):
        with self._lock:
            self._documents.pop(user_id, None)

    def documents(self):
        with self._lock:
            return [dict(document) for document in self._documents.values()]


# --- firestore -------------------------------------------------------------


def _firestore_client(config):
    from google.cloud import firestore  # imported here: a deploy dependency

    kwargs = {}
    if config.firestore_project:
        kwargs["project"] = config.firestore_project
    if config.firestore_database:
        kwargs["database"] = config.firestore_database
    return firestore.Client(**kwargs)


def _transactional(function):
    """The library's decorator when it is installed, else the bare function.

    The fake client in the tests has no decorator to offer; it applies writes
    as they are made. What that means honestly: the tests cover this module's
    lease LOGIC, and the atomicity and retry around it are the library's,
    exercised only against a real Firestore.
    """
    try:
        from google.cloud.firestore import transactional

        return transactional(function)
    except Exception:  # noqa: BLE001 - no library, or an older shape
        return function


def _lease_in_transaction(transaction, reference, holder, seconds, now):
    snapshot = reference.get(transaction=transaction)
    document = snapshot.to_dict() if getattr(snapshot, "exists", False) else None
    if document:
        current = document.get("lease_holder")
        until = float(document.get("lease_expires_at_epoch") or 0)
        if current and current != holder and until > now:
            return False
    transaction.set(
        reference,
        {
            "lease_holder": holder,
            "lease_expires_at_epoch": now + float(seconds),
            "lease_expires_at": datetime.fromtimestamp(now + float(seconds), tz=timezone.utc),
        },
        merge=True,
    )
    return True


class FirestoreStore(StateStore):
    """Session metadata in one Firestore collection, one document per user.

    Durable across restarts, redeploys and instance replacement — and that is
    the whole claim. It does not make the demo multi-instance; see the module
    docstring for the two things that would have to be solved first.
    """

    backend = FIRESTORE
    durable = True

    def __init__(self, config, client=None):
        self._config = config
        self._collection_name = config.firestore_collection
        if client is not None:
            self._client = client
            return
        try:
            self._client = _firestore_client(config)
        except ImportError as error:
            raise StateUnavailable(
                "STATE_BACKEND=firestore needs google-cloud-firestore, which is not "
                "installed ({}). Install it, or set STATE_BACKEND=memory.".format(error)
            )
        except Exception as error:  # noqa: BLE001 - credentials, project, network
            raise StateUnavailable(
                "cannot build a Firestore client ({}: {}). On Cloud Run the service "
                "account needs roles/datastore.user; locally, run `gcloud auth "
                "application-default login` or set STATE_BACKEND=memory.".format(
                    type(error).__name__, error
                )
            )

    def _collection(self):
        return self._client.collection(self._collection_name)

    def _document(self, user_id):
        return self._collection().document(user_id)

    def load(self, user_id):
        try:
            snapshot = self._document(user_id).get()
        except Exception as error:  # noqa: BLE001
            raise StateUnavailable("reading {} failed: {}".format(user_id, error))
        if not getattr(snapshot, "exists", False):
            return None
        return snapshot.to_dict()

    def save(self, document):
        try:
            self._document(document["user_id"]).set(document)
        except Exception as error:  # noqa: BLE001
            raise StateUnavailable("writing {} failed: {}".format(document["user_id"], error))

    def delete(self, user_id):
        try:
            self._document(user_id).delete()
        except Exception as error:  # noqa: BLE001
            raise StateUnavailable("deleting {} failed: {}".format(user_id, error))

    def documents(self):
        try:
            return [snapshot.to_dict() for snapshot in self._collection().stream()]
        except Exception as error:  # noqa: BLE001
            raise StateUnavailable("listing sessions failed: {}".format(error))

    def try_lease(self, user_id, holder, seconds, now=None):
        now = time.time() if now is None else now
        try:
            transaction = self._client.transaction()
            runner = _transactional(_lease_in_transaction)
            return bool(runner(transaction, self._document(user_id), holder, seconds, now))
        except Exception as error:  # noqa: BLE001
            raise StateUnavailable("leasing {} failed: {}".format(user_id, error))

    def release_lease(self, user_id, holder):
        try:
            self._document(user_id).set(
                {"lease_holder": None, "lease_expires_at_epoch": 0.0}, merge=True
            )
            return True
        except Exception as error:  # noqa: BLE001
            raise StateUnavailable("releasing {} failed: {}".format(user_id, error))

    def healthcheck(self):
        """One real round trip. Called at boot, where failure is fatal."""
        try:
            self._document("__healthcheck__").get()
        except Exception as error:  # noqa: BLE001
            raise StateUnavailable(
                "cannot reach Firestore collection {!r}: {}".format(
                    self._collection_name, error
                )
            )
        return True


# --- de-duplication --------------------------------------------------------


class MemoryDedupe:
    """Idempotence for Slack's at-least-once EVENT delivery.

    Slack retries an event when it does not see a 200 in three seconds and
    marks the retry with X-Slack-Retry-Num. Acting twice on one event would
    run two evaluations and post two narrations, so every event entry point
    checks the event id here first.

    It covers events and nothing else: interactive payloads carry no event id,
    so a double-click is a second genuine turn. The turn lock, not this, is
    what makes that safe.
    """

    def __init__(self, capacity=512):
        self._capacity = capacity
        self._seen = OrderedDict()
        self._lock = threading.Lock()

    def seen(self, event_id):
        """True when this event id was already handled (and records it)."""
        if not event_id:
            return False
        with self._lock:
            if event_id in self._seen:
                self._seen.move_to_end(event_id)
                return True
            self._seen[event_id] = time.time()
            while len(self._seen) > self._capacity:
                self._seen.popitem(last=False)
            return False

    def __len__(self):
        with self._lock:
            return len(self._seen)


class FirestoreDedupe:
    """The same promise, kept across a restart.

    One small document per event id, with an expires_at a TTL policy sweeps.
    A retry that arrives after a redeploy — exactly when a slow turn caused
    the retry in the first place — must still be recognized as a retry.

    Firestore unavailable mid-run degrades to the in-process LRU: answering a
    click twice is a worse failure than answering it not at all.
    """

    def __init__(self, config, store):
        self._config = config
        self._store = store
        self._fallback = MemoryDedupe()
        self._collection = config.firestore_collection + "-events"

    def seen(self, event_id):
        if not event_id:
            return False
        client = getattr(self._store, "_client", None)
        if client is None:
            return self._fallback.seen(event_id)
        reference = client.collection(self._collection).document(str(event_id))
        try:
            snapshot = reference.get()
            if getattr(snapshot, "exists", False):
                return True
            now = time.time()
            reference.set(
                {
                    "event_id": str(event_id),
                    "seen_at": now,
                    "expires_at": datetime.fromtimestamp(
                        now + self._config.dedupe_ttl_seconds, tz=timezone.utc
                    ),
                    "expires_at_epoch": now + self._config.dedupe_ttl_seconds,
                }
            )
            return False
        except Exception as error:  # noqa: BLE001
            log.error("dedupe write failed, degrading to memory for this event: %s", error)
            return self._fallback.seen(event_id)


# --- rate limits -----------------------------------------------------------


class RateLimited(Exception):
    """Raised when a user has spent a budget."""

    def __init__(self, retry_after_seconds):
        super().__init__("budget spent")
        self.retry_after_seconds = retry_after_seconds


class MemoryRateLimiter:
    """Per-user token bucket.

    Two of these exist: a small one over model calls (narration and drafting
    are the only work a vendor bills for) and a generous one over turns that
    start subprocesses — one instance with one CPU can be flooded by free work
    just as easily as by paid work.
    """

    def __init__(self, capacity=20, per_seconds=3600.0, name="bucket"):
        self.name = name
        self._capacity = float(capacity)
        self._rate = float(capacity) / float(per_seconds) if per_seconds else 0.0
        self._buckets = {}  # user_id -> (tokens, updated_at)
        self._lock = threading.Lock()

    def _refill(self, user_id, now):
        tokens, updated = self._buckets.get(user_id, (self._capacity, now))
        tokens = min(self._capacity, tokens + (now - updated) * self._rate)
        return tokens

    def allow(self, user_id, now=None):
        """Spend one token. True when the call may proceed."""
        now = time.time() if now is None else now
        with self._lock:
            tokens = self._refill(user_id, now)
            if tokens < 1.0:
                self._buckets[user_id] = (tokens, now)
                return False
            self._buckets[user_id] = (tokens - 1.0, now)
            return True

    def retry_after(self, user_id, now=None):
        """Seconds until one more token exists, rounded up."""
        now = time.time() if now is None else now
        with self._lock:
            tokens = self._refill(user_id, now)
        if tokens >= 1.0 or self._rate <= 0:
            return 0
        return int((1.0 - tokens) / self._rate) + 1

    def tokens(self, user_id, now=None):
        now = time.time() if now is None else now
        with self._lock:
            return self._refill(user_id, now)


class FirestoreRateLimiter(MemoryRateLimiter):
    """The same bucket, kept in a document per user per bucket.

    Without this a redeploy hands everybody a fresh budget, which is not a
    disaster but is a lie: the point of the limit is that a demo cannot bill
    the whole workspace, and a limit that resets on every restart does not
    make that promise.

    Reads and writes are best-effort: on any Firestore error the in-process
    bucket answers, because a metered demo that stops answering is worse than
    one that occasionally meters generously.
    """

    def __init__(self, config, store, capacity, per_seconds=3600.0, name="bucket"):
        super().__init__(capacity, per_seconds, name)
        self._config = config
        self._store = store
        self._collection = config.firestore_collection + "-limits"

    def _reference(self, user_id):
        client = getattr(self._store, "_client", None)
        if client is None:
            return None
        return client.collection(self._collection).document("{}-{}".format(self.name, user_id))

    def allow(self, user_id, now=None):
        now = time.time() if now is None else now
        reference = self._reference(user_id)
        if reference is None:
            return super().allow(user_id, now)
        try:
            snapshot = reference.get()
            document = snapshot.to_dict() if getattr(snapshot, "exists", False) else {}
            tokens = float(document.get("tokens", self._capacity))
            updated = float(document.get("updated_at", now))
            tokens = min(self._capacity, tokens + (now - updated) * self._rate)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            reference.set(
                {
                    "tokens": tokens,
                    "updated_at": now,
                    "bucket": self.name,
                    "user_id": user_id,
                    "expires_at": datetime.fromtimestamp(
                        now + max(3600.0, self._config.session_ttl_seconds), tz=timezone.utc
                    ),
                    "expires_at_epoch": now + max(3600.0, self._config.session_ttl_seconds),
                }
            )
            return allowed
        except Exception as error:  # noqa: BLE001
            log.error("rate-limit read/write failed, using the local bucket: %s", error)
            return super().allow(user_id, now)

    def retry_after(self, user_id, now=None):
        now = time.time() if now is None else now
        reference = self._reference(user_id)
        if reference is None:
            return super().retry_after(user_id, now)
        try:
            snapshot = reference.get()
            document = snapshot.to_dict() if getattr(snapshot, "exists", False) else {}
            tokens = float(document.get("tokens", self._capacity))
            updated = float(document.get("updated_at", now))
            tokens = min(self._capacity, tokens + (now - updated) * self._rate)
        except Exception:  # noqa: BLE001
            return super().retry_after(user_id, now)
        if tokens >= 1.0 or self._rate <= 0:
            return 0
        return int((1.0 - tokens) / self._rate) + 1


# --- the bundle ------------------------------------------------------------


class StateBackend:
    """One object that hands out the three state-shaped things app.py needs.

    Which implementations they are is decided once, here, by STATE_BACKEND —
    and nothing above this line is written differently for either choice.
    """

    def __init__(self, config, store, client=None):
        self.config = config
        self.store = store
        self.name = store.backend
        self.durable = store.durable

    def dedupe(self):
        if self.store.backend == FIRESTORE:
            return FirestoreDedupe(self.config, self.store)
        return MemoryDedupe()

    def limiter(self, name, capacity, per_seconds=3600.0):
        if self.store.backend == FIRESTORE:
            return FirestoreRateLimiter(self.config, self.store, capacity, per_seconds, name)
        return MemoryRateLimiter(capacity, per_seconds, name)


def build_backend(config, client=None, verify=True):
    """Choose a backend from configuration, and prove it works before serving.

    A Firestore backend that cannot be reached at boot is FATAL, on purpose:
    a demo configured to remember, that silently forgets instead, is worse
    than one that refuses to start and says why.
    """
    choice = (config.state_backend or MEMORY).strip().lower()
    if choice == FIRESTORE:
        store = FirestoreStore(config, client=client)
        if verify:
            store.healthcheck()  # raises StateUnavailable
        log.info(
            "state backend: firestore collection %r (sessions survive restarts; "
            "scratch projects and desks do not — they are rebuilt)",
            config.firestore_collection,
        )
    elif choice == MEMORY:
        store = MemoryStore(config)
        log.info("state backend: memory (state is lost when this process ends)")
    else:
        raise StateUnavailable(
            "STATE_BACKEND={!r} is not one of: {}, {}".format(choice, MEMORY, FIRESTORE)
        )
    return StateBackend(config, store, client=client)
