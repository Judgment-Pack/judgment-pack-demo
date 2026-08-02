"""Per-user session state, event de-duplication, and the rate limits.

STATE LIMITATION (deliberate, documented in slack/DESIGN.md): everything here
lives in one process's memory. The deploy script pins the Cloud Run service to
min-instances=1 and max-instances=1 so a user's session, their scratch copy of
the demo project, and their screening desk all stay on the machine that
created them. Firestore is the named upgrade path when this outgrows one
instance; it is not built.

Two concurrency facts drive the shapes here:

* Slack's interactive payloads carry no event id, so de-duplication cannot
  cover a double-click. What covers it is the per-session TURN LOCK: one turn
  per user at a time, and a second one is answered rather than run.
* Reaping a session terminates a process and deletes a directory. That must
  never happen while the table lock is held, or one slow reap stalls every
  other user's turn.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

log = logging.getLogger("jpack-slack.state")


@dataclass
class Session:
    """One Slack user's run through the demo."""

    user_id: str
    created_at: float
    last_seen: float
    scratch_dir: Optional[str] = None
    active_flow: Optional[str] = None
    step: int = 0
    completed: Set[str] = field(default_factory=set)
    welcomed: bool = False
    # Flow-local scratch data (a draft pack, a desk handle, the newest run id).
    data: Dict[str, Any] = field(default_factory=dict)
    # Held for the whole of one turn: a click, a slash command, or a message.
    # Everything a turn touches — this object, the scratch project on disk,
    # the session's gateway — is single-writer only because of this lock.
    lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)

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
    """Thread-safe session table with a TTL and a hard cap.

    Scratch directories are real copies of the demo project, so expiry deletes
    bytes: sessions older than the TTL are removed, and when the table is full
    the least recently seen session is evicted. Both the process kill and the
    delete happen OUTSIDE the table lock.
    """

    def __init__(self, config, on_evict=None):
        self._config = config
        self._sessions = OrderedDict()  # user_id -> Session, LRU order
        self._lock = threading.RLock()
        self._on_evict = on_evict
        self._sweeper = None
        self._stop = threading.Event()

    # --- lookup -----------------------------------------------------------

    def get(self, user_id, create=True, now=None):
        now = time.time() if now is None else now
        evicted = []
        with self._lock:
            evicted.extend(self._expired(now))
            session = self._sessions.get(user_id)
            if session is None:
                if not create:
                    self._release_many(evicted)
                    return None
                session = Session(user_id=user_id, created_at=now, last_seen=now)
                self._sessions[user_id] = session
                evicted.extend(self._overflow())
            session.touch(now)
            self._sessions.move_to_end(user_id)
        self._release_many(evicted)
        return session

    def _expired(self, now):
        gone = []
        for user_id, session in list(self._sessions.items()):
            if now - session.last_seen > self._config.session_ttl_seconds:
                self._sessions.pop(user_id, None)
                gone.append(session)
        return gone

    def _overflow(self):
        gone = []
        while len(self._sessions) > self._config.max_sessions:
            _, session = self._sessions.popitem(last=False)
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
        never come.
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
            session = self._sessions.pop(user_id, None)
        if session is not None:
            self._release_many([session])
        return session

    def all(self):
        with self._lock:
            return list(self._sessions.values())

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


def remove_scratch(session):
    """Delete a session's scratch copy of the demo project, if it has one."""
    path = session.scratch_dir
    session.scratch_dir = None
    if not path:
        return
    if not os.path.isdir(path):
        return
    shutil.rmtree(path, ignore_errors=True)


class EventDedupe:
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


class RateLimited(Exception):
    """Raised when a user has spent a budget."""

    def __init__(self, retry_after_seconds):
        super().__init__("budget spent")
        self.retry_after_seconds = retry_after_seconds


class RateLimiter:
    """Per-user token bucket.

    Two of these exist: a small one over model calls (narration and drafting
    are the only work a vendor bills for) and a generous one over turns that
    start subprocesses — one instance with one CPU can be flooded by free work
    just as easily as by paid work.
    """

    def __init__(self, capacity=20, per_seconds=3600.0):
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
