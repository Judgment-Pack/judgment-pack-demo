"""What the app SAYS must match what the app SHOWS.

Two places where a caption could drift from the artifact beside it: the
decision book's record shapes, and use case 2's "I removed one fact" line when
the draft never carried facts to remove. Both are regression-locked here,
along with the rule that no model prose reaches Slack unattributed.
"""

from __future__ import annotations

import json
import os
import time

import fakes

from bot import flows
from bot.flows import author, book
from bot.flows.base import Turn
from bot.state import Session


def session():
    now = time.time()
    return Session(user_id="U1", created_at=now, last_seen=now)


def text_of(replies):
    return " ".join(json.dumps(one.blocks) for one in replies)


# --- the decision book -----------------------------------------------------


def test_record_shapes_are_told_apart():
    assert book.record_shape(fakes.RECORD) == "single-pack"
    assert book.record_shape(fakes.NODE_RECORD) == "graph-node"
    assert book.record_shape(fakes.COMPOSITE_RECORD) == "composite"
    assert book.record_shape({}) == "unknown"


def test_a_graph_node_record_is_not_captioned_as_single_pack():
    """The default path (use case 3 before 4) shows node records."""
    state = session()
    deps = fakes.deps(
        runtime=fakes.FakeRuntime(records=[fakes.NODE_RECORD, fakes.COMPOSITE_RECORD])
    )
    replies = flows.start(Turn(user_id="U1", session=state), deps, "book")
    shown = text_of(replies)
    assert "graph NODE" in shown
    assert "The newest single-pack record" not in shown
    # And what it says about the printed record is true of it.
    assert '\\"graph\\"' in shown and '\\"pack\\"' in shown


def test_a_composite_record_is_captioned_as_the_headline():
    state = session()
    deps = fakes.deps(runtime=fakes.FakeRuntime(records=[fakes.COMPOSITE_RECORD]))
    replies = flows.start(Turn(user_id="U1", session=state), deps, "book")
    shown = text_of(replies)
    assert "graph-composite headline" in shown


def test_the_summary_line_names_the_graph_when_there_is_no_pack():
    line = book._summarize(fakes.COMPOSITE_RECORD)
    assert "vendor-onboarding-flow (composite headline)" in line
    assert "?" not in line.split("·")[2]


def test_single_pack_records_are_preferred_for_the_replay():
    records = [fakes.NODE_RECORD, fakes.RECORD, fakes.COMPOSITE_RECORD]
    assert book._newest_evaluation(records) is fakes.RECORD


# --- use case 2's envelope -------------------------------------------------


def test_a_draft_without_facts_is_refused_before_it_can_lie():
    assert author._envelope_problem({"pack": {"a": 1}, "facts": {"x": "1"}}) is None
    assert "facts" in author._envelope_problem({"pack": {"a": 1}})
    assert "facts" in author._envelope_problem({"pack": {"a": 1}, "facts": {}})
    assert "pack" in author._envelope_problem({"facts": {"x": "1"}})
    assert author._envelope_problem(None)


def test_a_factless_draft_falls_back_instead_of_removing_None(tmp_path):
    """The failure this prevents: 'I will remove exactly one fact — `None`'."""

    class Factless(fakes.deps().model.__class__):
        def _generate(self, prompt):
            if '"decisionId"' in prompt:
                return json.dumps({"decisionId": "x", "pack": {"specVersion": "0.2.0-draft"}})
            return super()._generate(prompt)

    project = tmp_path / "session-U1"
    (project / "packs").mkdir(parents=True)
    (project / "jpack.json").write_text(json.dumps({"configVersion": "3", "packs": {}}))

    class Runtime(fakes.FakeRuntime):
        def ensure_project(self, state):
            state.scratch_dir = str(project)
            return state.scratch_dir

    state = session()
    deps = fakes.deps(runtime=Runtime())
    deps.model = Factless(deps.config)
    flows.start(Turn(user_id="U1", session=state), deps, "author")
    flows.dispatch(Turn(user_id="U1", session=state, action="canned"), deps)
    replies = flows.dispatch(Turn(user_id="U1", session=state, action="judge"), deps)

    shown = text_of(replies)
    assert "`None`" not in shown
    assert state.data["author_removed_pointer"]
    assert os.path.exists(str(project / "packs" / "gifts-hospitality.pack.json"))


def test_a_repair_that_returns_only_a_pack_keeps_the_scenario():
    """A repair round must not silently discard the case being judged."""
    state = session()
    deps = fakes.deps()
    state.data["author_envelope"] = {
        "pack": {"old": True},
        "scenario": "the original case",
        "facts": {"gift": {"valueUsd": "32"}},
        "evidence": {"gift-register-entry": "present"},
    }

    class Runtime(fakes.FakeRuntime):
        def __init__(self):
            super().__init__()
            self.rounds = 0

        def spec_validate_text(self, text):
            self.rounds += 1
            if self.rounds == 1:
                return fakes.ok({"status": "invalid", "diagnostics": [{"code": "X", "message": "no"}]})
            return fakes.ok({"status": "valid", "diagnostics": []})

    class BarePackRepair(deps.model.__class__):
        def _generate(self, prompt):
            return json.dumps({"specVersion": "0.2.0-draft", "repaired": True})

    deps.runtime = Runtime()
    deps.model = BarePackRepair(deps.config)
    body = []
    turn = Turn(user_id="U1", session=state)
    draft, valid = author._validate_loop(deps, turn, {"old": True}, "policy", body)

    assert valid
    assert draft.get("repaired") is True
    envelope = state.data["author_envelope"]
    assert envelope["facts"] == {"gift": {"valueUsd": "32"}}
    assert envelope["scenario"] == "the original case"


# --- attribution everywhere ------------------------------------------------


def test_the_glue_path_attributes_the_model_like_narrations_do():
    """app.py cannot be imported without slack_bolt, so read the wiring."""
    here = os.path.dirname(os.path.abspath(__file__))
    source = open(os.path.join(os.path.dirname(here), "bot", "app.py")).read()
    assert "blocks.model_blocks(glue.text" in source, (
        "conversational glue must go through the attributed, escaped model surface"
    )
    assert "blocks.section(glue.text)" not in source
