"""The per-user model budget, and the sessions it lives beside.

The limiter meters model calls only. Nothing here can stop a disposition from
being produced: the runtime is keyless and free, which is the whole point.
"""

from __future__ import annotations

import os

from bot.config import Config
from bot.state import RateLimiter, SessionStore


def test_a_user_spends_their_bucket_and_is_then_refused():
    limiter = RateLimiter(capacity=3, per_seconds=3600)
    now = 1000.0
    assert [limiter.allow("U1", now) for _ in range(3)] == [True, True, True]
    assert limiter.allow("U1", now) is False


def test_budgets_are_per_user():
    limiter = RateLimiter(capacity=1, per_seconds=3600)
    now = 1000.0
    assert limiter.allow("U1", now) is True
    assert limiter.allow("U1", now) is False
    assert limiter.allow("U2", now) is True


def test_tokens_refill_over_time():
    limiter = RateLimiter(capacity=2, per_seconds=3600)
    now = 0.0
    limiter.allow("U1", now)
    limiter.allow("U1", now)
    assert limiter.allow("U1", now) is False
    # Half an hour later one token has come back (2 per hour).
    assert limiter.allow("U1", now + 1800) is True


def test_retry_after_is_a_positive_wait_when_spent():
    limiter = RateLimiter(capacity=1, per_seconds=3600)
    now = 0.0
    limiter.allow("U1", now)
    assert limiter.retry_after("U1", now) > 0
    assert limiter.retry_after("U1", now) <= 3601
    assert limiter.retry_after("U2", now) == 0


def test_refusal_copy_is_polite_and_names_the_budget():
    from bot import content

    text = content.RATE_LIMITED.format(n=20, minutes=7)
    assert "20" in text and "7" in text
    assert "still works" in text


def _config(**overrides):
    env = {"SESSION_ROOT": "/tmp/does-not-need-to-exist"}
    env.update(overrides)
    return Config.from_env(env)


def test_sessions_expire_after_the_ttl():
    store = SessionStore(_config(SESSION_TTL_SECONDS="60"))
    store.get("U1", now=1000.0)
    assert store.get("U1", create=False, now=1030.0) is not None
    assert store.get("U1", create=False, now=2000.0) is None


def test_the_session_table_is_capped_and_evicts_the_least_recent():
    store = SessionStore(_config(MAX_SESSIONS="2"))
    store.get("U1", now=1.0)
    store.get("U2", now=2.0)
    store.get("U3", now=3.0)
    assert store.get("U1", create=False, now=4.0) is None
    assert store.get("U2", create=False, now=4.0) is not None
    assert store.get("U3", create=False, now=4.0) is not None


def test_eviction_reaps_the_desk_and_the_scratch_copy(tmp_path):
    reaped = []
    # SESSION_ROOT must be the real root here: a scratch path outside it is
    # refused by the containment guard, which is its own test below.
    store = SessionStore(
        _config(SESSION_TTL_SECONDS="1", SESSION_ROOT=str(tmp_path)),
        on_evict=lambda s: reaped.append(s.user_id),
    )
    session = store.get("U1", now=0.0)
    scratch = tmp_path / "session-U1"
    scratch.mkdir()
    (scratch / "packs").mkdir()
    session.scratch_dir = str(scratch)
    store.sweep(now=100.0)
    assert reaped == ["U1"]
    assert not os.path.exists(str(scratch))
