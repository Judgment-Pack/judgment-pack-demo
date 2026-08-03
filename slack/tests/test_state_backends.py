"""Both backends, the same assertions — plus what only durability changes.

The parity tests exist so the memory backend cannot quietly drift from the
Firestore one: whatever the demo relies on, it relies on in both. The tests
after them are about the difference itself — a document that outlives the
process, and the split between what such a document may carry and what it
must never.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

import pytest
from fake_firestore import FakeFirestore

from bot.config import Config
from bot.state import Session, SessionStore
from bot.store import (
    FirestoreStore,
    MemoryStore,
    StateUnavailable,
    build_backend,
    json_safe_data,
    to_document,
)


SLACK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config(**overrides):
    env = {"SESSION_ROOT": "/tmp", "FIRESTORE_COLLECTION": "test-sessions"}
    env.update(overrides)
    return Config.from_env(env)


def backends():
    """One of each, built the way app.py builds them."""
    memory = build_backend(config(STATE_BACKEND="memory"))
    firestore = build_backend(
        config(STATE_BACKEND="firestore"), client=FakeFirestore(), verify=True
    )
    return [("memory", memory), ("firestore", firestore)]


def session(user_id="U1", now=1000.0):
    return Session(user_id=user_id, created_at=now, last_seen=now)


# --- parity ----------------------------------------------------------------


@pytest.mark.parametrize("name,backend", backends())
def test_a_saved_session_reads_back_the_same(name, backend):
    store = SessionStore(config(), backend=backend)
    live = store.get("U1", now=1000.0)
    live.enter("judge")
    live.step = 2
    live.completed.add("book")
    live.welcomed = True
    live.scratch_dir = "/tmp/session-U1"
    live.data["author_decision_id"] = "gifts-hospitality"
    store.save(live)

    document = backend.store.load("U1")
    assert document["active_flow"] == "judge"
    assert document["step"] == 2
    assert document["completed"] == ["book"]
    assert document["welcomed"] is True
    assert document["scratch_dir"] == "/tmp/session-U1"
    assert document["data"]["author_decision_id"] == "gifts-hospitality"


@pytest.mark.parametrize("name,backend", backends())
def test_ttl_expiry_evicts_and_reaps(name, backend):
    reaped = []
    store = SessionStore(
        config(SESSION_TTL_SECONDS="60"), on_evict=lambda s: reaped.append(s.user_id),
        backend=backend,
    )
    store.get("U1", now=1000.0)
    assert store.get("U1", create=False, now=1030.0) is not None
    assert store.get("U1", create=False, now=5000.0) is None
    assert reaped == ["U1"]
    assert backend.store.load("U1") is None


@pytest.mark.parametrize("name,backend", backends())
def test_the_cap_evicts_the_least_recently_seen(name, backend):
    store = SessionStore(config(MAX_SESSIONS="2"), backend=backend)
    store.get("U1", now=1.0)
    store.get("U2", now=2.0)
    store.get("U3", now=3.0)
    assert store.get("U1", create=False, now=4.0) is None
    assert store.get("U2", create=False, now=4.0) is not None
    assert store.get("U3", create=False, now=4.0) is not None


@pytest.mark.parametrize("name,backend", backends())
def test_drop_removes_both_halves(name, backend):
    store = SessionStore(config(), backend=backend)
    store.get("U1", now=1000.0)
    store.drop("U1")
    assert backend.store.load("U1") is None
    assert store.all() == []


@pytest.mark.parametrize("name,backend", backends())
def test_dedupe_and_limits_behave_the_same(name, backend):
    dedupe = backend.dedupe()
    assert dedupe.seen("Ev1") is False
    assert dedupe.seen("Ev1") is True
    assert dedupe.seen(None) is False

    limiter = backend.limiter("turns", 2)
    assert limiter.allow("U1") is True
    assert limiter.allow("U1") is True
    assert limiter.allow("U1") is False
    assert limiter.retry_after("U1") > 0
    assert limiter.allow("U2") is True


# --- what only durability changes ------------------------------------------


def test_a_session_survives_the_process_that_made_it():
    """The whole point: a NEW store, holding nothing, still knows this user."""
    client = FakeFirestore()
    first = SessionStore(
        config(), backend=build_backend(config(STATE_BACKEND="firestore"), client=client)
    )
    live = first.get("U1", now=1000.0)
    live.enter("judge")
    live.step = 2
    live.completed.add("attested")
    first.save(live)

    # A different process: a new store, a new local table, the same collection.
    second = SessionStore(
        config(), backend=build_backend(config(STATE_BACKEND="firestore"), client=client)
    )
    restored = second.get("U1", now=1100.0)
    assert restored.active_flow == "judge"
    assert restored.step == 2
    assert restored.completed == {"attested"}
    assert restored.restored is True, "a restored session must be flagged for the reconciler"


def test_memory_forgets_and_says_so():
    memory = build_backend(config(STATE_BACKEND="memory"))
    first = SessionStore(config(), backend=memory)
    live = first.get("U1", now=1000.0)
    live.enter("judge")
    first.save(live)
    # A memory backend that outlived its process is a contradiction; a NEW
    # backend is the honest model of a restart.
    second = SessionStore(config(), backend=build_backend(config(STATE_BACKEND="memory")))
    assert second.get("U1", create=False, now=1100.0) is None
    assert memory.durable is False


# --- the split -------------------------------------------------------------


def test_the_live_desk_is_never_persisted():
    live = session()
    live.data["desk"] = object()  # a Popen, a port, a keypair: this process only
    live.data["author_decision_id"] = "gifts-hospitality"
    document = to_document(live, 3600)
    assert "desk" not in document["data"]
    assert document["data"]["author_decision_id"] == "gifts-hospitality"
    json.dumps(document["data"])  # whatever is left must serialize


def test_unserializable_and_oversized_data_is_dropped_not_mangled():
    class NotJSON:
        pass

    safe = json_safe_data(
        {
            "keep": {"a": ["b", 1, True, None]},
            "drop_object": NotJSON(),
            "drop_huge": "x" * 300_000,
            "_private": "skipped",
        }
    )
    assert safe == {"keep": {"a": ["b", 1, True, None]}}


def test_the_lock_is_not_a_field_anybody_could_persist():
    document = to_document(session(), 3600)
    assert "lock" not in document
    assert "restored" not in document
    json.dumps({k: v for k, v in document.items() if not k.endswith("_at") or k.endswith("_epoch")})


def test_expires_at_is_a_real_timestamp_for_the_ttl_policy():
    live = session(now=1000.0)
    document = to_document(live, 7200)
    assert document["expires_at_epoch"] == 1000.0 + 7200
    assert hasattr(document["expires_at"], "tzinfo")
    assert document["expires_at"].tzinfo is not None


def test_scratch_dir_persists_as_a_hint_only():
    live = session()
    live.scratch_dir = "/tmp/session-U1"
    document = to_document(live, 60)
    # It is written, because the sweeper in the NEXT process needs to know
    # where to look; nothing may treat it as proof the directory exists.
    assert document["scratch_dir"] == "/tmp/session-U1"


# --- the lease seam --------------------------------------------------------


def test_a_lease_is_taken_and_respected():
    client = FakeFirestore()
    store = FirestoreStore(config(STATE_BACKEND="firestore"), client=client)
    assert store.try_lease("U1", "instance-a", 60, now=1000.0) is True
    assert store.try_lease("U1", "instance-b", 60, now=1010.0) is False
    # It expires, so a crashed holder cannot lock a user out forever.
    assert store.try_lease("U1", "instance-b", 60, now=1100.0) is True
    assert store.release_lease("U1", "instance-b") is True
    assert store.try_lease("U1", "instance-a", 60, now=1110.0) is True


def test_the_lease_is_a_seam_not_a_promise_in_memory():
    store = MemoryStore(config())
    assert store.try_lease("U1", "a", 60) is True
    assert store.try_lease("U1", "b", 60) is True  # nothing to coordinate


# --- failure posture -------------------------------------------------------


def test_an_unreachable_firestore_refuses_to_boot():
    client = FakeFirestore(fail_on="get")
    with pytest.raises(StateUnavailable):
        build_backend(config(STATE_BACKEND="firestore"), client=client)


def test_an_unknown_backend_name_refuses_to_boot():
    with pytest.raises(StateUnavailable):
        build_backend(config(STATE_BACKEND="postgres"))


def test_a_client_that_cannot_be_built_refuses_with_our_message(monkeypatch):
    """Credentials missing is our refusal to explain, not a library traceback."""
    import bot.store as store_module

    def explode(_config):
        raise RuntimeError("no application default credentials")

    monkeypatch.setattr(store_module, "_firestore_client", explode)
    with pytest.raises(StateUnavailable) as raised:
        build_backend(config(STATE_BACKEND="firestore"))
    message = str(raised.value)
    assert "roles/datastore.user" in message
    assert "STATE_BACKEND=memory" in message


def test_boot_refusal_reaches_the_process_exit(tmp_path):
    """The refusal is fatal in the same way the missing-secrets one is."""
    script = tmp_path / "boot.py"
    script.write_text(
        "import sys; sys.path.insert(0, {!r})\n".format(str(SLACK_DIR))
        + "from bot.config import Config\n"
        "from bot.store import build_backend\n"
        "build_backend(Config.from_env({'STATE_BACKEND': 'firestore'}))\n"
    )
    proc = subprocess.run([sys.executable, str(script)], capture_output=True)
    assert proc.returncode != 0
    assert b"StateUnavailable" in proc.stderr or b"firestore" in proc.stderr.lower()


def test_a_backend_that_fails_mid_run_degrades_instead_of_dropping_the_turn(caplog):
    client = FakeFirestore()
    backend = build_backend(config(STATE_BACKEND="firestore"), client=client)
    store = SessionStore(config(), backend=backend)
    live = store.get("U1", now=1000.0)
    live.enter("judge")
    store.save(live)

    client.fail_on = "all"  # Firestore goes away mid-demo
    with caplog.at_level("ERROR"):
        again = store.get("U1", now=1001.0)
    assert again is live, "the local half still answers the user's click"
    assert store.degraded is True
    assert any("degrad" in record.message.lower() for record in caplog.records)

    client.fail_on = None
    recovered = store.get("U1", now=1002.0)
    assert recovered is live
    assert store.degraded is False


def test_concurrent_saves_do_not_corrupt_the_document():
    client = FakeFirestore()
    backend = build_backend(config(STATE_BACKEND="firestore"), client=client)
    store = SessionStore(config(), backend=backend)
    live = store.get("U1", now=1000.0)

    def hammer():
        for _ in range(20):
            store.save(live)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    document = backend.store.load("U1")
    assert document["user_id"] == "U1"
    assert isinstance(document["completed"], list)
