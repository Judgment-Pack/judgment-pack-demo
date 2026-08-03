"""Restarting mid-demo: rebuild what can be rebuilt, say so when it cannot.

Four tests, one per use case, each modelling the real thing: a user is
mid-flow, the container is replaced, and their next click lands in a process
that has never seen them. Their progress comes back from the backend; their
scratch project and their signing desk do not, because those never left the
old machine.

The invariant every one of them checks: `completed` — the menu the user sees —
is exactly what they left, and nothing claims an artifact that is gone.
"""

from __future__ import annotations

import json
import os

import fakes
from fake_firestore import FakeFirestore

from bot import flows
from bot.config import Config
from bot.reconcile import RESET_NOTICE, Reconciler
from bot.state import SessionStore
from bot.store import build_backend


def config(tmp_path, **overrides):
    env = {
        "STATE_BACKEND": "firestore",
        "FIRESTORE_COLLECTION": "test-sessions",
        "SESSION_ROOT": str(tmp_path),
    }
    env.update(overrides)
    return Config.from_env(env)


def restart(client, settings):
    """A new process against the same collection: new store, empty side table."""
    return SessionStore(
        settings, backend=build_backend(settings, client=client)
    )


class Runtime(fakes.FakeRuntime):
    """A runtime whose project really is a directory, so a rebuild is visible."""

    def __init__(self, root, **kwargs):
        super().__init__(**kwargs)
        self.root = root
        self.rebuilds = 0

    def ensure_project(self, session):
        path = session.scratch_dir or os.path.join(self.root, "session-" + session.user_id)
        session.scratch_dir = path
        if not os.path.isdir(os.path.join(path, "packs")):
            os.makedirs(os.path.join(path, "packs"), exist_ok=True)
            with open(os.path.join(path, "jpack.json"), "w") as handle:
                json.dump({"configVersion": "3", "packs": {}}, handle)
            self.rebuilds += 1
        return path


def mid_flow(client, settings, flow_id, step, completed=("judge",), data=None):
    """Leave a user mid-flow, then throw the process away."""
    store = SessionStore(settings, backend=build_backend(settings, client=client))
    session = store.get("U1", now=1000.0)
    session.enter(flow_id)
    session.step = step
    session.completed.update(completed)
    session.welcomed = True
    session.scratch_dir = os.path.join(settings.session_root, "session-U1")
    session.data.update(data or {})
    # A live desk exists in THIS process only.
    session.data["desk"] = object()
    store.save(session)
    return store


# --- use case 1: rebuildable, so it resumes --------------------------------


def test_restart_mid_judge_rebuilds_the_project_and_carries_on(tmp_path):
    client = FakeFirestore()
    settings = config(tmp_path)
    mid_flow(client, settings, "judge", step=2)

    store = restart(client, settings)
    session = store.get("U1", now=1100.0)
    runtime = Runtime(str(tmp_path))
    notice = Reconciler(runtime, fakes.FakeDesk()).reconcile(session)

    assert session.active_flow == "judge"
    assert session.step == 2, "a rebuildable flow keeps its place"
    assert session.completed == {"judge"}
    assert runtime.rebuilds == 1, "the scratch project was re-copied"
    assert notice and "rebuilt your copy of the project" in notice
    assert "desk" not in session.data


# --- use case 2: rebuildable from the persisted pack ------------------------


def test_restart_mid_author_re_registers_the_pack_it_persisted(tmp_path):
    client = FakeFirestore()
    settings = config(tmp_path)
    pack = {"specVersion": "0.2.0-draft", "version": "0.1.0", "title": "Gifts"}
    mid_flow(
        client,
        settings,
        "author",
        step=1,
        data={
            "author_pack": pack,
            "author_decision_id": "gifts-hospitality",
            "author_policy": "Gifts up to 50 USD…",
            "author_envelope": {"pack": pack, "facts": {"gift": {"valueUsd": "32"}}},
        },
    )

    store = restart(client, settings)
    session = store.get("U1", now=1100.0)
    runtime = Runtime(str(tmp_path))
    notice = Reconciler(runtime, fakes.FakeDesk()).reconcile(session)

    assert session.step == 1, "the newborn pack is back, so the flow keeps its place"
    assert notice and "re-registered the pack you authored" in notice
    written = os.path.join(session.scratch_dir, "packs", "gifts-hospitality.pack.json")
    assert os.path.isfile(written), "the pack was rebuilt from the persisted bytes"
    assert json.load(open(written)) == pack, "the SAME bytes, not a new draft"
    declared = json.load(open(os.path.join(session.scratch_dir, "jpack.json")))
    assert "gifts-hospitality" in declared["packs"]
    assert ("packs-lock",) in runtime.calls, "moving the reviewed set is declared, not hidden"


def test_restart_mid_author_without_the_pack_starts_that_flow_again(tmp_path):
    client = FakeFirestore()
    settings = config(tmp_path)
    mid_flow(
        client, settings, "author", step=1, data={"author_policy": "Gifts up to 50 USD…"}
    )

    store = restart(client, settings)
    session = store.get("U1", now=1100.0)
    notice = Reconciler(Runtime(str(tmp_path)), fakes.FakeDesk()).reconcile(session)

    assert session.step == 0
    assert session.active_flow == "author"
    assert notice == RESET_NOTICE["author"]
    assert session.completed == {"judge"}, "what they finished is untouched"
    assert session.data["author_policy"] == "Gifts up to 50 USD…", (
        "their own policy text survives — the notice promises it is all this flow needs"
    )


# --- use case 3: NOT rebuildable ------------------------------------------


def test_restart_mid_attested_starts_that_use_case_again_and_says_why(tmp_path):
    client = FakeFirestore()
    settings = config(tmp_path)
    mid_flow(client, settings, "attested", step=1, completed=("judge", "author"))

    store = restart(client, settings)
    session = store.get("U1", now=1100.0)
    desk = fakes.FakeDesk()
    notice = Reconciler(Runtime(str(tmp_path)), desk).reconcile(session)

    assert session.step == 0, "a desk that is gone cannot be resumed, only restarted"
    assert session.active_flow == "attested"
    assert notice == RESET_NOTICE["attested"]
    assert "receipts" in notice and "cannot be re-created" in notice
    assert session.completed == {"judge", "author"}
    assert "desk" not in session.data, "no stale handle claims a gateway that is not running"
    assert desk.started is False, "reconciliation does not quietly start a replacement desk"


# --- use case 4: NOT rebuildable ------------------------------------------


def test_restart_mid_book_starts_that_use_case_again_and_names_the_loss(tmp_path):
    client = FakeFirestore()
    settings = config(tmp_path)
    mid_flow(client, settings, "book", step=1, completed=("judge", "author", "attested"))

    store = restart(client, settings)
    session = store.get("U1", now=1100.0)
    notice = Reconciler(Runtime(str(tmp_path)), fakes.FakeDesk()).reconcile(session)

    assert session.step == 0
    assert notice == RESET_NOTICE["book"]
    assert "audit trail was a file" in notice
    assert session.completed == {"judge", "author", "attested"}


# --- the quiet cases -------------------------------------------------------


def test_a_restart_between_flows_says_nothing(tmp_path):
    client = FakeFirestore()
    settings = config(tmp_path)
    store = SessionStore(settings, backend=build_backend(settings, client=client))
    session = store.get("U1", now=1000.0)
    session.completed.update({"judge", "book"})
    session.welcomed = True
    store.save(session)

    store = restart(client, settings)
    session = store.get("U1", now=1100.0)
    runtime = Runtime(str(tmp_path))
    assert Reconciler(runtime, fakes.FakeDesk()).reconcile(session) is None
    assert runtime.rebuilds == 0, "nothing is rebuilt until a flow needs it"
    assert session.completed == {"judge", "book"}
    assert session.welcomed is True, "they are not greeted twice"


def test_a_session_this_process_already_holds_is_not_reconciled(tmp_path):
    client = FakeFirestore()
    settings = config(tmp_path)
    store = mid_flow(client, settings, "attested", step=1)
    session = store.get("U1", now=1001.0)  # same process, same object
    assert session.restored is False
    assert Reconciler(Runtime(str(tmp_path)), fakes.FakeDesk()).reconcile(session) is None
    assert session.step == 1, "a live session is never reset out from under a turn"


def test_reconciling_twice_only_acts_once(tmp_path):
    client = FakeFirestore()
    settings = config(tmp_path)
    mid_flow(client, settings, "judge", step=2)
    store = restart(client, settings)
    session = store.get("U1", now=1100.0)
    reconciler = Reconciler(Runtime(str(tmp_path)), fakes.FakeDesk())
    assert reconciler.reconcile(session) is not None
    assert reconciler.reconcile(session) is None


def test_every_flow_declares_what_it_needs():
    """A new flow with no NEEDS would silently be treated as project-only."""
    for flow in flows.CATALOGUE:
        assert hasattr(flow, "NEEDS"), flow.ID
        assert flow.NEEDS, flow.ID
        for need in flow.NEEDS:
            assert need in ("project", "registered-pack", "desk", "audit-trail"), need


def test_the_reset_notice_never_blames_the_user():
    for flow_id, notice in RESET_NOTICE.items():
        assert "service restarted" in notice, flow_id
        assert "Nothing you finished was lost" in notice, flow_id
