"""Live session objects, the turn lock, and the table that holds them.

State is now split in two, and the split is documented in full at the top of
`bot/store.py`:

* the PERSISTED document — where a user is in the demo — lives behind a
  `StateStore` (memory by default, Firestore when configured), so progress
  survives a restart;
* the PROCESS-LOCAL parts — the threading.Lock that serializes a user's
  turns, and the live Desk with its Popen and its keypair — live in this
  module's side table and die with the process, because they cannot mean
  anything anywhere else.

What is still true, and load-bearing:

* Slack's interactive payloads carry no event id, so de-duplication cannot
  cover a double-click. What covers it is the per-session TURN LOCK: one turn
  per user at a time, and a second one is answered rather than run.
* Reaping a session terminates a process and deletes a directory. That must
  never happen while the table lock is held, or one slow reap stalls every
  other user's turn.

Durability does not make this multi-instance: `bot/reconcile.py` rebuilds what
a new container can rebuild and says so plainly when something cannot be
rebuilt.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

# Re-exported under their old names so importers that predate the split keep
# working: with no backend configured these ARE the implementations.
from .store import MemoryDedupe as EventDedupe  # noqa: F401
from .store import MemoryRateLimiter as RateLimiter  # noqa: F401
from .store import MemoryStore  # noqa: F401
from .store import RateLimited  # noqa: F401
from .store import StateUnavailable, apply_document, build_backend, to_document

log = logging.getLogger("jpack-slack.state")

__all__ = [
    "EventDedupe",
    "MemoryStore",
    "RateLimited",
    "RateLimiter",
    "Session",
    "SessionStore",
    "StateUnavailable",
    "TurnLock",
    "apply_document",
    "build_backend",
    "remove_scratch",
    "to_document",
]

# Who this process is, for the lease field on a persisted document. A seam
# for a multi-instance version; nothing in the request path depends on it.
HOLDER = "{}-{}".format(os.environ.get("K_REVISION", "local"), uuid.uuid4().hex[:8])


@dataclass
class Session:
    """One Slack user's run through the demo, as this process sees it.

    The first eight members are persisted; `data` is persisted in its
    JSON-safe part only; `lock`, `desk` (inside `data`) and `restored` are
    this process's alone.
    """

    user_id: str
    created_at: float
    last_seen: float
    scratch_dir: Optional[str] = None
    active_flow: Optional[str] = None
    step: int = 0
    completed: Set[str] = field(default_factory=set)
    welcomed: bool = False
    # Flow-local scratch data. The JSON-safe part persists (a draft pack, the
    # newest run id, scalars); the live Desk under "desk" never does.
    data: Dict[str, Any] = field(default_factory=dict)
    # Held for the whole of one turn: a click, a slash command, or a message.
    # Everything a turn touches — this object, the scratch project on disk,
    # the session's gateway — is single-writer only because of this lock.
    lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)
    # True when this object was rebuilt from a persisted document by a process
    # that had never seen this user before — i.e. after a restart. The
    # reconciler clears it once it has rebuilt what it can.
    restored: bool = field(default=False, repr=False, compare=False)

    def touch(self, now=None):
        self.last_seen = time.time() if now is None else now

    def enter(self, flow_id):
        self.active_flow = flow_id
        self.step = 0

    def finish(self, flow_id):
        self.completed.add(flow_id)
        self.active_flow = None
        self.step = 0


class TurnLock:
    """One turn's hold on one session, or an honest failure to get it.

        with TurnLock(session) as turn:
            if not turn:
                ...  # someone else is mid-turn: say so, do not queue
    """

    def __init__(self, session):
        self.session = session
        self.acquired = session.lock.acquire(False)

    def __bool__(self):
        return self.acquired

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def release(self):
        if self.acquired:
            self.acquired = False
            self.session.lock.release()


class SessionStore:
    """The table of live sessions, backed by a StateStore.

    Semantics are exactly what they were when this was a plain dict: a TTL, a
    hard cap on how many sessions exist at once, eviction of the least
    recently seen, and reaping (kill the desk, delete the scratch copy) that
    happens OUTSIDE the table lock.

    What changed is where the metadata lives. With the memory backend this is
    the same object graph as before. With Firestore, a process that has never
    seen a user still finds their progress — and marks the session `restored`
    so the reconciler knows to rebuild the local half.
    """

    def __init__(self, config, on_evict=None, backend=None):
        self._config = config
        self._backend = backend or build_backend(config, verify=False)
        self._store = self._backend.store
        self._local = {}  # user_id -> Session (locks, desks: this process only)
        self._lock = threading.RLock()
        self._on_evict = on_evict
        self._sweeper = None
        self._stop = threading.Event()
        self._degraded = False

    # --- backend calls, which must never drop a turn ----------------------

    def _durable(self, call, *args, **kwargs):
        """Talk to the backend; on failure log loudly and carry on locally.

        Boot already proved the backend reachable (`build_backend` refuses to
        start otherwise). If it goes away mid-run, the honest trade is to
        keep answering the user out of this process's memory and say so in
        the log — a dropped click teaches them nothing.
        """
        try:
            result = call(*args, **kwargs)
            if self._degraded:
                log.warning("state backend is answering again")
                self._degraded = False
            return result
        except StateUnavailable as error:
            if not self._degraded:
                log.error(
                    "state backend unavailable — degrading to this process's memory "
                    "for now; sessions will not survive a restart while this lasts: %s",
                    error,
                )
                self._degraded = True
            return None
        except Exception as error:  # noqa: BLE001 - same posture for a surprise
            log.error("state backend raised %s; degrading to memory", type(error).__name__)
            self._degraded = True
            return None

    @property
    def backend_name(self):
        return self._backend.name

    @property
    def degraded(self):
        return self._degraded

    # --- lookup -----------------------------------------------------------

    def get(self, user_id, create=True, now=None):
        now = time.time() if now is None else now
        evicted = []
        with self._lock:
            evicted.extend(self._expired(now))
            session = self._local.get(user_id)
            if session is None:
                document = self._durable(self._store.load, user_id)
                if document:
                    session = Session(
                        user_id=user_id, created_at=now, last_seen=now, restored=True
                    )
                    apply_document(session, document)
                    self._local[user_id] = session
                    log.info(
                        "restored session for %s from %s (flow %s, step %s, completed %s)",
                        user_id,
                        self._backend.name,
                        session.active_flow,
                        session.step,
                        sorted(session.completed),
                    )
                elif create:
                    session = Session(user_id=user_id, created_at=now, last_seen=now)
                    self._local[user_id] = session
                else:
                    self._release_many(evicted)
                    return None
                session.touch(now)
                # Written before the cap is enforced: the cap counts documents,
                # and a session the backend has not heard of yet would let the
                # table grow by one every time.
                self.save(session)
                evicted.extend(self._overflow(now))
            session.touch(now)
        self.save(session)
        self._release_many(evicted)
        return session

    def save(self, session):
        """Persist the metadata half of a session. Cheap, and called often."""
        document = to_document(
            session,
            self._config.session_ttl_seconds,
            holder=HOLDER,
            lease_seconds=self._config.lease_seconds,
        )
        self._durable(self._store.save, document)
        return document

    def _expired(self, now):
        """Sessions past the TTL, dropped from both halves."""
        gone = []
        documents = self._durable(self._store.expired, now) or []
        for document in documents:
            user_id = document.get("user_id")
            if not user_id:
                continue
            session = self._local.pop(user_id, None) or _shell(document)
            self._durable(self._store.delete, user_id)
            gone.append(session)
        # A local session whose backend document vanished (a TTL policy got
        # there first) still owns a desk and a directory here.
        for user_id, session in list(self._local.items()):
            if now - session.last_seen > self._config.session_ttl_seconds:
                self._local.pop(user_id, None)
                self._durable(self._store.delete, user_id)
                if session not in gone:
                    gone.append(session)
        return gone

    def _overflow(self, now):
        gone = []
        cap = self._config.max_sessions
        for document in self._durable(self._store.overflow, cap) or []:
            user_id = document.get("user_id")
            if not user_id:
                continue
            session = self._local.pop(user_id, None) or _shell(document)
            self._durable(self._store.delete, user_id)
            gone.append(session)
        return gone

    # --- expiry -----------------------------------------------------------

    def sweep(self, now=None):
        """Drop sessions past the TTL. Returns the evicted user ids."""
        now = time.time() if now is None else now
        with self._lock:
            expired = self._expired(now)
        self._release_many(expired)
        return [session.user_id for session in expired]

    def start_sweeper(self, interval=60.0):
        """Expire on a timer, not on the next visitor's request.

        Without this, a quiet workspace keeps every session's gateway process
        alive indefinitely — the TTL would be honored only by traffic that may
        never come. (A Firestore TTL policy deletes the DOCUMENTS on its own
        schedule; this thread is what kills the processes and the directories,
        which no policy can reach.)
        """
        if self._sweeper is not None:
            return self._sweeper

        def loop():
            while not self._stop.wait(interval):
                try:
                    gone = self.sweep()
                    if gone:
                        log.info("swept %d expired session(s)", len(gone))
                except Exception:  # noqa: BLE001 - a reaper must not die
                    log.exception("session sweep failed")

        self._sweeper = threading.Thread(target=loop, name="session-sweeper", daemon=True)
        self._sweeper.start()
        return self._sweeper

    def stop_sweeper(self):
        self._stop.set()

    def drop(self, user_id):
        with self._lock:
            session = self._local.pop(user_id, None)
            self._durable(self._store.delete, user_id)
        if session is not None:
            self._release_many([session])
        return session

    def all(self):
        with self._lock:
            return list(self._local.values())

    def _release_many(self, sessions):
        # Called with NO lock held: on_evict kills a process and waits for it,
        # and remove_scratch deletes a directory tree.
        for session in sessions:
            if self._on_evict is not None:
                try:
                    self._on_evict(session)
                except Exception:  # noqa: BLE001 - never let cleanup break a request
                    log.exception("evicting a session failed")
            remove_scratch(session)


def _shell(document):
    """A session object for a document this process has never held.

    Used only for reaping: it carries the scratch_dir hint so the sweeper can
    delete a directory a previous life left behind.
    """
    session = Session(
        user_id=document.get("user_id", "?"),
        created_at=float(document.get("created_at") or 0),
        last_seen=float(document.get("last_seen") or 0),
    )
    apply_document(session, document)
    return session


def remove_scratch(session):
    """Delete a session's scratch copy of the demo project, if it has one."""
    path = session.scratch_dir
    session.scratch_dir = None
    if not path:
        return
    if not os.path.isdir(path):
        return
    shutil.rmtree(path, ignore_errors=True)
