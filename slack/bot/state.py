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
from .store import (
    LOAD_FAILED,
    StateUnavailable,
    apply_document,
    build_backend,
    to_document,
)

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

# Who this process is, for the lease a multi-instance version would take. A
# seam: `try_lease` writes it, and nothing in the request path reads it. An
# ordinary save must never touch the lease fields — that was how a routine
# turn used to erase a claim another instance held.
HOLDER = "{}-{}".format(os.environ.get("K_REVISION", "local"), uuid.uuid4().hex[:8])


@dataclass
class Session:
    """One Slack user's run through the demo, as this process sees it.

    The first eight members are persisted; `data` is persisted in its
    JSON-safe part only. `lock`, `restored`, `persist`, `alive` and the desk
    inside `data` are this process's alone — and `persist`/`alive` are the two
    that decide whether this object may be written back at all.
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
    # False when this session was built WITHOUT a successful read of the
    # backend: we do not know what document exists for this user, so nothing
    # about them may be written until a read proves it. The turn is still
    # served, and the user is told once.
    persist: bool = field(default=True, repr=False, compare=False)
    # False once this session has been reaped — its desk killed, its scratch
    # copy deleted, its document removed. A turn still holding the object must
    # not write it back to life.
    alive: bool = field(default=True, repr=False, compare=False)

    def touch(self, now=None):
        self.last_seen = time.time() if now is None else now

    def enter(self, flow_id):
        self.active_flow = flow_id
        self.step = 0

    def finish(self, flow_id):
        self.completed.add(flow_id)
        self.active_flow = None
        self.step = 0

    def leave(self):
        """Abandon the active flow without crediting it."""
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

        Boot already proved the backend reachable and WRITABLE
        (`build_backend` refuses to start otherwise). If it goes away
        mid-run, the honest trade is to keep answering the user out of this
        process's memory and say so in the log — a dropped click teaches them
        nothing.

        A success here does NOT clear the degraded flag. Recovery is proven by
        a successful READ (`_load`), because the write leg is usually healthy
        while the read leg is not — and it is the read leg whose failure can
        cost somebody their progress.
        """
        try:
            return call(*args, **kwargs)
        except StateUnavailable as error:
            self._degrade(error)
            return None
        except Exception as error:  # noqa: BLE001 - same posture for a surprise
            self._degrade("{}: {}".format(type(error).__name__, error))
            return None

    def _degrade(self, reason):
        if not self._degraded:
            log.error(
                "state backend unavailable — degrading to this process's memory for "
                "now; sessions will not survive a restart while this lasts: %s",
                reason,
            )
        self._degraded = True

    def _load(self, user_id):
        """Read one document. Returns (document_or_None, read_succeeded).

        The second member is the whole point: `None` with `True` means this
        user has no session yet, and `None` with `False` means nobody knows.
        A caller that conflates them writes an empty session over a live one.
        """
        document = self._store.load(user_id)
        if document is LOAD_FAILED:
            self._degrade("the session document could not be read")
            return None, False
        if self._degraded:
            log.warning("state backend is answering reads again")
            self._degraded = False
        return document, True

    @property
    def backend_name(self):
        return self._backend.name

    @property
    def degraded(self):
        return self._degraded

    # --- lookup -----------------------------------------------------------

    def get(self, user_id, create=True, now=None):
        """The live session for this user.

        On the turn path this touches the backend exactly ONCE — one read of
        one document — and never scans the collection. Expiring other
        people's sessions is the sweeper's job (and the TTL policy's); a
        user's click should not pay for it, and it certainly should not pay
        for it while holding the lock every other user's click needs.
        """
        now = time.time() if now is None else now
        stale = []
        with self._lock:
            session = self._local.get(user_id)
            if session is not None and now - session.last_seen > self._config.session_ttl_seconds:
                # THIS user's session has expired. One comparison, no backend
                # call, no scan: everybody else's expiry is the sweeper's.
                self._local.pop(user_id, None)
                session.alive = False
                stale.append(session)
                session = None
            if session is not None:
                session.touch(now)
                self.save(session)
                return session
        if stale:
            self._durable(self._store.delete, stale[0].user_id)
            self._release_many(stale)
            if not create:
                return None

        document, read_ok = self._load(user_id)

        with self._lock:
            session = self._local.get(user_id)  # another thread may have won
            if session is not None:
                session.touch(now)
                self.save(session)
                return session
            if document:
                session = Session(
                    user_id=user_id, created_at=now, last_seen=now, restored=True
                )
                apply_document(session, document)
                log.info(
                    "restored session for %s from %s (flow %s, step %s, completed %s)",
                    user_id,
                    self._backend.name,
                    session.active_flow,
                    session.step,
                    sorted(session.completed),
                )
            elif not read_ok:
                # The read FAILED. This user may be three use cases in, with
                # everything sitting in a document nobody could see just now.
                # Serve the turn, but never write: an empty session saved here
                # would destroy exactly what durability exists to keep, in the
                # one window (just after a redeploy) where every turn lands
                # here. `persist` stays False until a later read proves the
                # document's state.
                if not create:
                    return None
                session = Session(
                    user_id=user_id, created_at=now, last_seen=now, persist=False
                )
                log.error(
                    "serving %s from a blank session because the backend read failed; "
                    "NOTHING will be written for them until a read succeeds",
                    user_id,
                )
            elif create:
                session = Session(user_id=user_id, created_at=now, last_seen=now)
            else:
                return None
            self._local[user_id] = session
            session.touch(now)
            evicted = self._overflow_locally()
        self.save(session)
        self._release_many(evicted)
        return session

    def save(self, session):
        """Persist the metadata half of a session. Cheap, and called often.

        Two sessions are never written: one whose read failed (`persist` is
        False — we do not know what we would be overwriting) and one that has
        already been reaped (`alive` is False — the sweeper deleted its
        document, killed its desk and removed its project, and a late write
        from an in-flight turn would resurrect a session with no local half).
        """
        if not session.persist:
            return None
        if not session.alive:
            log.info("not writing %s: the session was reaped mid-turn", session.user_id)
            return None
        document = to_document(session, self._config.session_ttl_seconds)
        self._durable(self._store.save, document)
        return document

    def _overflow_locally(self):
        """Enforce the cap over sessions THIS process holds. Lock held."""
        gone = []
        while len(self._local) > self._config.max_sessions:
            oldest = min(self._local.values(), key=lambda s: s.last_seen)
            self._local.pop(oldest.user_id, None)
            oldest.alive = False
            gone.append(oldest)
        for session in gone:
            self._durable(self._store.delete, session.user_id)
        return gone

    def _expired_locally(self, now):
        """Local sessions past the TTL. Lock held; no backend reads."""
        gone = []
        for user_id, session in list(self._local.items()):
            if now - session.last_seen > self._config.session_ttl_seconds:
                self._local.pop(user_id, None)
                session.alive = False
                gone.append(session)
        return gone

    # --- expiry -----------------------------------------------------------

    def sweep(self, now=None, durable=True):
        """Expire sessions. The sweeper thread's job, not a turn's.

        Two halves. The local one kills desks and deletes scratch copies —
        things no TTL policy can reach. The durable one is a bounded query for
        documents whose expiry has passed, run OFF the table lock, as a
        backstop for the window before the policy deletes them (and for a
        deployment that never created one).
        """
        now = time.time() if now is None else now
        with self._lock:
            expired = self._expired_locally(now)
        for session in expired:
            # The document goes with the desk and the directory: a policy that
            # deletes it eventually is a backstop, not a reason to leave a
            # session half-reaped in the meantime.
            self._durable(self._store.delete, session.user_id)
        self._release_many(expired)
        gone = [session.user_id for session in expired]

        if durable and self._backend.durable:
            for document in self._durable(self._store.expired, now) or []:
                user_id = document.get("user_id")
                if not user_id or user_id in gone:
                    continue
                with self._lock:
                    session = self._local.pop(user_id, None)
                if session is not None:
                    session.alive = False
                shell = session or _shell(document)
                self._durable(self._store.delete, user_id)
                self._release_many([shell])
                gone.append(user_id)
        return gone

    def start_sweeper(self, interval=60.0):
        """Expire on a timer, not on the next visitor's request.

        Without this, a quiet workspace keeps every session's gateway process
        alive indefinitely — the TTL would be honored only by traffic that may
        never come. (A Firestore TTL policy deletes the DOCUMENTS on its own
        schedule, including while this service is scaled to zero; this thread
        is what kills the processes and the directories, which no policy can
        reach.)
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
            if session is not None:
                session.alive = False
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
            session.alive = False
            if self._on_evict is not None:
                try:
                    self._on_evict(session)
                except Exception:  # noqa: BLE001 - never let cleanup break a request
                    log.exception("evicting a session failed")
            remove_scratch(session, self._config.session_root)


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


def inside(path, root):
    """Is `path` really under `root`, after symlinks?

    `scratch_dir` now round-trips through an external datastore, and it is fed
    straight to rmtree. The module docstring already calls it a hint; this is
    the code agreeing. A hint that points outside the scratch root is not a
    directory this app may delete, and it is not one it may reuse either.
    """
    if not path or not root:
        return False
    try:
        real_path = os.path.realpath(path)
        real_root = os.path.realpath(root)
        return os.path.commonpath([real_path, real_root]) == real_root and real_path != real_root
    except (ValueError, OSError):
        return False


def remove_scratch(session, root=None):
    """Delete a session's scratch copy of the demo project, if it has one.

    Only ever inside the configured scratch root: a stored path that points
    anywhere else is refused and logged rather than obeyed.
    """
    path = session.scratch_dir
    session.scratch_dir = None
    if not path:
        return
    if root is not None and not inside(path, root):
        log.error(
            "refusing to delete %r for %s: it is not inside the scratch root %r",
            path,
            session.user_id,
            root,
        )
        return
    if not os.path.isdir(path):
        return
    shutil.rmtree(path, ignore_errors=True)
