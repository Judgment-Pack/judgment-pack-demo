"""One turn per user at a time, and every subprocess started clean.

Slack's interactive payloads carry no event id, so the de-duplicator cannot
see a double-click — the turn lock is what makes a second click safe. These
tests hold the two invariants that follow from that: exactly one of two
concurrent turns executes, and no child process is handed a secret.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import fakes

from bot import flows
from bot.config import Config
from bot.desk import DeskManager
from bot.flows.base import Turn
from bot.runtime import JpackRuntime
from bot.state import Session, SessionStore, TurnLock


def session():
    now = time.time()
    return Session(user_id="U1", created_at=now, last_seen=now)


# --- the turn lock ---------------------------------------------------------


def test_two_concurrent_turns_for_one_user_run_exactly_one():
    state = session()
    started = []
    refused = []
    entered = threading.Event()
    finish = threading.Event()

    def turn(name):
        with TurnLock(state) as held:
            if not held:
                refused.append(name)
                return
            started.append(name)
            entered.set()
            finish.wait(2)

    first = threading.Thread(target=turn, args=("a",))
    first.start()
    assert entered.wait(2), "the first turn never took the lock"
    second = threading.Thread(target=turn, args=("b",))
    second.start()
    second.join(2)

    assert refused == ["b"], "the second turn should be answered, not run"
    assert started == ["a"]
    finish.set()
    first.join(2)

    # And the lock is released afterwards, so the next click works.
    with TurnLock(state) as held:
        assert bool(held)


def test_the_lock_is_per_user():
    one, two = session(), Session(user_id="U2", created_at=0, last_seen=0)
    with TurnLock(one) as first:
        assert bool(first)
        with TurnLock(two) as second:
            assert bool(second)


def test_a_double_click_cannot_run_two_flow_turns():
    """The state machine's own invariant under the lock: one step, once."""
    state = session()
    deps = fakes.deps()
    flows.start(Turn(user_id="U1", session=state), deps, "judge")

    results = []

    def click():
        with TurnLock(state) as held:
            if not held:
                results.append("busy")
                return
            flows.dispatch(Turn(user_id="U1", session=state, action="case"), deps)
            results.append("ran")

    threads = [threading.Thread(target=click) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    # Whether the second click was refused or simply queued behind the first,
    # the session advanced one step per executed turn — never two at once.
    assert deps.runtime.evaluations == results.count("ran")
    assert state.step == results.count("ran")


# --- the child environment -------------------------------------------------

SECRETS = ("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "GEMINI_API_KEY")


def _with_secrets(monkeypatch):
    for name in SECRETS:
        monkeypatch.setenv(name, "sensitive-" + name.lower())
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))


def test_child_env_carries_no_secret(monkeypatch):
    _with_secrets(monkeypatch)
    config = Config.from_env(dict(os.environ))
    env = config.child_env({"JPACK_CONFIG": "/x/jpack.json"})
    for name in SECRETS:
        assert name not in env
    assert env["JPACK_CONFIG"] == "/x/jpack.json"
    assert "PATH" in env


def test_jpack_children_see_no_secret(monkeypatch):
    _with_secrets(monkeypatch)
    config = Config.from_env(dict(os.environ))
    env = JpackRuntime(config)._env("/tmp/project")
    for name in SECRETS:
        assert name not in env
    assert env["JPACK_CONFIG"] == os.path.join("/tmp/project", "jpack.json")


def test_attest_and_gateway_children_see_no_secret(monkeypatch):
    _with_secrets(monkeypatch)
    config = Config.from_env(dict(os.environ))
    manager = DeskManager(config)

    class _Desk:
        url = "http://127.0.0.1:1"
        store = "/tmp/store"
        pin_file = "/tmp/pin"

    for env in (
        manager._attest_env(_Desk()),
        config.child_env({"WATCHLIST": config.ofac_watchlist}),
    ):
        for name in SECRETS:
            assert name not in env
    assert manager._attest_env(_Desk())["GATEWAY_STORE"] == "/tmp/store"


def test_a_real_child_process_cannot_read_the_secrets(monkeypatch):
    """Not a claim about the env dict — a claim about a running process."""
    _with_secrets(monkeypatch)
    config = Config.from_env(dict(os.environ))
    proc = subprocess.run(
        [sys.executable, "-c", "import os,json;print(json.dumps(sorted(os.environ)))"],
        stdout=subprocess.PIPE,
        env=config.child_env(),
    )
    seen = proc.stdout.decode()
    for name in SECRETS:
        assert name not in seen


# --- session reaping -------------------------------------------------------


def test_the_sweeper_thread_expires_sessions_without_traffic():
    config = Config.from_env({"SESSION_TTL_SECONDS": "0", "SESSION_ROOT": "/tmp"})
    reaped = []
    store = SessionStore(config, on_evict=lambda s: reaped.append(s.user_id))
    store.get("U1")
    thread = store.start_sweeper(interval=0.05)
    assert thread.daemon
    deadline = time.time() + 3
    while time.time() < deadline and not reaped:
        time.sleep(0.05)
    store.stop_sweeper()
    assert reaped == ["U1"], "a quiet workspace must still expire sessions"


def test_reaping_happens_outside_the_table_lock():
    """A slow desk shutdown must not stall every other user's turn.

    The table lock is reentrant, so asking for it on the reaping thread would
    prove nothing: another thread has to get it while the reap is in flight.
    """
    config = Config.from_env({"SESSION_TTL_SECONDS": "0", "SESSION_ROOT": "/tmp"})
    observed = []

    def slow_evict(evicted):
        def other_thread():
            got = store._lock.acquire(False)
            observed.append(got)
            if got:
                store._lock.release()

        probe = threading.Thread(target=other_thread)
        probe.start()
        probe.join(2)

    store = SessionStore(config, on_evict=slow_evict)
    store.get("U1", now=0.0)
    store.sweep(now=10_000.0)
    assert observed == [True], "the table was locked while a session was being reaped"
