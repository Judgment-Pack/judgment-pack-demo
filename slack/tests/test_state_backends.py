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
    assert json.loads(document["data_json"])["author_decision_id"] == "gifts-hospitality"
    # An ordinary save owns no lease: those fields belong to try_lease alone.
    assert "lease_holder" not in document
    assert "lease_expires_at_epoch" not in document


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
    data = json.loads(document["data_json"])
    assert "desk" not in data
    assert data["author_decision_id"] == "gifts-hospitality"


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

    # Recovery is proven by a successful READ, never by a write: the write leg
    # is usually healthy while the read leg is not, and it is the read leg
    # whose failure can cost somebody their progress.
    client.fail_on = None
    store.save(live)
    assert store.degraded is True, "a successful write proves nothing about reads"
    fresh = SessionStore(config(), backend=backend)
    assert fresh.get("U1", now=1002.0).active_flow == "judge"
    assert fresh.degraded is False


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


# --- the destruction the reviewer executed ---------------------------------


def test_a_failed_read_never_overwrites_a_live_document():
    """The blocking one: a transient read error must not erase progress.

    Life one leaves a user three use cases in. The container is replaced, and
    in the new one every turn takes the load path — which is exactly when a
    read failure looks like "this user is new". If that turn saved, the
    document would be gone.
    """
    client = FakeFirestore()
    settings = config(STATE_BACKEND="firestore")
    first = SessionStore(settings, backend=build_backend(settings, client=client))
    live = first.get("U1", now=1000.0)
    live.completed.update({"judge", "author", "attested"})
    live.enter("book")
    live.step = 1
    first.save(live)
    before = client.documents("test-sessions")["U1"]

    # A new process (the redeploy), with reads failing.
    client.fail_on = "get"
    second = SessionStore(settings, backend=build_backend(settings, client=client, verify=False))
    served = second.get("U1", now=1100.0)

    assert served is not None, "the click is still answered"
    assert served.persist is False, "nothing may be written for a session we could not read"
    assert second.degraded is True

    served.completed.add("nonsense")  # whatever the turn does
    second.save(served)
    after = client.documents("test-sessions")["U1"]
    assert after == before, "the live document was not touched"

    # Reads come back: the real session is there, whole.
    client.fail_on = None
    third = SessionStore(settings, backend=build_backend(settings, client=client))
    recovered = third.get("U1", now=1200.0)
    assert recovered.completed == {"judge", "author", "attested"}
    assert recovered.active_flow == "book"
    assert recovered.step == 1
    assert recovered.persist is True
    assert third.degraded is False


def test_a_failed_read_is_not_absence_even_for_a_genuinely_new_user():
    client = FakeFirestore(fail_on="get")
    settings = config(STATE_BACKEND="firestore")
    store = SessionStore(settings, backend=build_backend(settings, client=client, verify=False))
    session = store.get("U-NEW", now=1000.0)
    assert session.persist is False
    store.save(session)
    assert client.documents("test-sessions") == {}, "a session we cannot verify is not written"


def test_the_degraded_flag_survives_a_successful_write():
    client = FakeFirestore(fail_on="get")
    settings = config(STATE_BACKEND="firestore")
    store = SessionStore(settings, backend=build_backend(settings, client=client, verify=False))
    session = store.get("U1", now=1000.0)
    session.persist = True  # pretend a caller forced a write
    store.save(session)
    assert store.degraded is True, "only a successful READ proves recovery"


# --- the dedupe race the reviewer executed ---------------------------------


def test_concurrent_deliveries_of_one_event_dedupe():
    """Two threads, one event id, a slow round trip: exactly one runs."""
    client = FakeFirestore(get_latency=0.02)
    backend = build_backend(config(STATE_BACKEND="firestore"), client=client)
    dedupe = backend.dedupe()
    answers = []

    def deliver():
        answers.append(dedupe.seen("Ev-RETRY-1"))

    threads = [threading.Thread(target=deliver) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert sorted(answers) == [False, True], "one delivery ran, one was recognized as a retry"


def test_dedupe_uses_create_not_check_then_write():
    client = FakeFirestore()
    backend = build_backend(config(STATE_BACKEND="firestore"), client=client)
    dedupe = backend.dedupe()
    assert dedupe.seen("Ev1") is False
    assert client.creates == 1
    assert dedupe.seen("Ev1") is True
    assert client.creates == 2, "the second call attempted the same atomic create"


def test_dedupe_degrades_to_memory_on_a_real_failure():
    client = FakeFirestore(fail_on="create")
    backend = build_backend(config(STATE_BACKEND="firestore"), client=client, verify=False)
    dedupe = backend.dedupe()
    assert dedupe.seen("Ev1") is False
    assert dedupe.seen("Ev1") is True, "the in-process LRU still suppresses the retry"


# --- the lease the reviewer saw clobbered ----------------------------------


def test_an_ordinary_save_does_not_move_a_lease():
    client = FakeFirestore()
    settings = config(STATE_BACKEND="firestore")
    backend = build_backend(settings, client=client)
    store = FirestoreStore(settings, client=client)
    assert store.try_lease("U1", "instance-a", 60, now=1000.0) is True

    # A routine turn in another process: it never calls try_lease.
    other = SessionStore(settings, backend=backend)
    other.get("U1", now=1010.0)

    document = client.documents("test-sessions")["U1"]
    assert document["lease_holder"] == "instance-a", "a save must not claim the lease"
    assert store.try_lease("U1", "instance-b", 60, now=1020.0) is False, (
        "the holder's claim is still enforced after an unrelated save"
    )


def test_the_lease_timestamp_and_epoch_agree():
    client = FakeFirestore()
    store = FirestoreStore(config(STATE_BACKEND="firestore"), client=client)
    store.try_lease("U1", "instance-a", 60, now=1000.0)
    document = client.documents("test-sessions")["U1"]
    assert document["lease_expires_at_epoch"] == 1060.0
    assert document["lease_expires_at"].timestamp() == 1060.0


# --- the resurrection the reviewer executed --------------------------------


def test_a_reaped_session_cannot_be_written_back_by_an_in_flight_turn(tmp_path):
    client = FakeFirestore()
    settings = config(SESSION_TTL_SECONDS="1", SESSION_ROOT=str(tmp_path),
                      STATE_BACKEND="firestore")
    store = SessionStore(settings, backend=build_backend(settings, client=client))
    live = store.get("U9", now=1000.0)
    live.enter("attested")
    live.step = 3
    store.save(live)

    assert store.sweep(now=9000.0) == ["U9"]
    assert client.documents("test-sessions") == {}
    assert live.alive is False

    # The turn that was still running finishes and saves, as app.py's finally does.
    store.save(live)
    assert client.documents("test-sessions") == {}, "a reaped session stays reaped"


def test_the_cap_evicts_without_scanning_the_collection(tmp_path):
    client = FakeFirestore()
    settings = config(MAX_SESSIONS="2", SESSION_ROOT=str(tmp_path), STATE_BACKEND="firestore")
    store = SessionStore(settings, backend=build_backend(settings, client=client))
    store.get("U1", now=1.0)
    store.get("U2", now=2.0)
    before = client.streams
    store.get("U3", now=3.0)
    assert client.streams == before, "a turn never streams the collection"
    assert store.get("U1", create=False, now=4.0) is None


def test_a_turn_reads_one_document_and_writes_one():
    """The cost claim in DESIGN.md, enforced."""
    client = FakeFirestore()
    settings = config(STATE_BACKEND="firestore")
    store = SessionStore(settings, backend=build_backend(settings, client=client))
    store.get("U1", now=1000.0)  # first contact: one read, one write

    # A second process, built BEFORE measuring: the boot healthcheck is a
    # write, a read and a delete, and it is boot cost, not turn cost.
    fresh = SessionStore(settings, backend=build_backend(settings, client=client))
    reads, writes, streams = client.reads, client.writes, client.streams
    fresh.get("U1", now=1001.0)
    assert client.reads - reads == 1, "one document read per turn"
    assert client.writes - writes == 1, "one document write per turn"
    assert client.streams == streams, "no collection scan on the turn path"


def test_the_sweeper_queries_instead_of_streaming():
    client = FakeFirestore()
    settings = config(SESSION_TTL_SECONDS="1", STATE_BACKEND="firestore")
    store = SessionStore(settings, backend=build_backend(settings, client=client))
    store.get("U1", now=1000.0)
    streams = client.streams
    store.sweep(now=9000.0)
    assert client.streams == streams, "expiry asks a bounded question, not for everything"
    assert client.queries >= 1


# --- the write half of the healthcheck -------------------------------------


def test_a_read_only_principal_fails_the_boot_healthcheck():
    """`roles/datastore.viewer` must not boot a service that promises to remember."""
    client = FakeFirestore(fail_on="set")
    with pytest.raises(StateUnavailable) as raised:
        build_backend(config(STATE_BACKEND="firestore"), client=client)
    assert "roles/datastore.user" in str(raised.value)


def test_the_healthcheck_cleans_up_after_itself():
    client = FakeFirestore()
    build_backend(config(STATE_BACKEND="firestore"), client=client)
    assert "__healthcheck__" not in client.documents("test-sessions")


# --- the containment guard -------------------------------------------------


def test_a_stored_path_outside_the_scratch_root_is_never_deleted(tmp_path):
    from bot.state import inside, remove_scratch

    outside = tmp_path / "not-a-session"
    outside.mkdir()
    live = session()
    live.scratch_dir = str(outside)
    remove_scratch(live, str(tmp_path / "sessions"))
    assert outside.exists(), "a path outside the scratch root is refused, not obeyed"

    root = tmp_path / "sessions"
    (root / "session-U1").mkdir(parents=True)
    live.scratch_dir = str(root / "session-U1")
    remove_scratch(live, str(root))
    assert not (root / "session-U1").exists()

    assert inside(str(root / "session-U1"), str(root)) is True
    assert inside("/etc", str(root)) is False
    assert inside(str(root), str(root)) is False, "the root itself is not a session"
    assert inside(None, str(root)) is False


# --- the document budget ---------------------------------------------------


def test_the_data_budget_is_per_document_not_per_key(caplog):
    data = {"a": "x" * 60_000, "b": "y" * 60_000, "c": "z" * 60_000, "small": "keep me"}
    with caplog.at_level("WARNING"):
        safe = json_safe_data(data, budget=100_000)
    assert len(json.dumps(safe)) <= 100_000
    assert safe["small"] == "keep me", "the smallest, most useful keys survive"
    assert any("dropping session data" in record.message for record in caplog.records)


def test_the_token_bucket_survives_a_backwards_clock():
    from bot.store import refill

    assert refill(2.0, updated_at=1000.0, now=900.0, rate=0.01, capacity=20) == 2.0
    assert refill(0.0, updated_at=1000.0, now=1000.0, rate=0.01, capacity=20) == 0.0
    assert refill(2.0, updated_at=1000.0, now=1100.0, rate=0.01, capacity=20) == 3.0
    assert refill(19.5, updated_at=0.0, now=10_000.0, rate=0.01, capacity=20) == 20.0
