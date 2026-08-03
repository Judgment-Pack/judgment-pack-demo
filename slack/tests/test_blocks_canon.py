"""Canon of the Block Kit payloads.

Two things are pinned: the shape Slack will accept (limits, action-id
grammar), and the shape the demo's honesty depends on — a disposition is
rendered with its COMPLETE reason set, its trace, and its handoff, before any
narration exists, and the boundary line rides along with every narration.
"""

from __future__ import annotations

import json

import fakes

from bot import blocks, content, flows
from bot.state import Session


def walk(block_list):
    for block in block_list:
        yield block


def test_disposition_blocks_are_the_runtime_speaking():
    payload = fakes.UNRESOLVED
    rendered = blocks.disposition_blocks(
        payload, title="t", facts={"a": "b"}, evidence={"tax-form": "present"}
    )
    text = json.dumps(rendered)
    # the complete reason set, not a chosen headline cause
    assert "missing-required-evidence" in text and "unknown" in text
    # the trace, with its onUnknown
    assert "sanctions-match-hard-stop" in text and "escalate" in text
    # the handoff, as recorded, with the "not a delivery" caveat
    assert "Vendor risk committee" in text
    assert "request recorded, not a delivery" in text
    # the pack identity and the conformance pointer, never a claim restated
    assert "CONFORMANCE.md" in text
    assert "conformance claim" in text.lower()


def test_outcome_headline_names_the_outcome():
    rendered = blocks.disposition_blocks(fakes.APPROVE)
    assert "approve" in json.dumps(rendered)


def test_narration_always_carries_the_boundary_line():
    rendered = blocks.narration_blocks("some narration", "gemini-x")
    assert content.BOUNDARY in json.dumps(rendered)


def test_absent_narration_says_the_disposition_is_unchanged():
    rendered = blocks.narration_blocks(None)
    assert "runtime produced it without the model" in json.dumps(rendered)


def test_error_blocks_quote_the_refusal_verbatim():
    rendered = blocks.error_blocks("It refused", "JPS-LOCK-VERIFY: reviewed set mismatch")
    text = json.dumps(rendered)
    assert "JPS-LOCK-VERIFY" in text
    assert "refusing loudly is the demo" in text


def test_every_action_id_uses_the_reserved_prefix():
    state = Session(user_id="U1", created_at=0, last_seen=0)
    payloads = [
        flows.menu(state).blocks,
        flows.about(state).blocks,
        flows.welcome(state).blocks,
        blocks.next_steps_blocks(flows.CATALOGUE, {"judge"}, "Judge a vendor"),
    ]
    for payload in payloads:
        for block in walk(payload):
            for element in block.get("elements", []) if block.get("type") == "actions" else []:
                assert element["action_id"].startswith(blocks.ACTION_PREFIX)


def test_slack_limits_are_respected_everywhere():
    state = Session(user_id="U1", created_at=0, last_seen=0)
    payloads = [
        flows.menu(state).blocks,
        flows.about(state).blocks,
        flows.welcome(state).blocks,
        blocks.disposition_blocks(fakes.UNRESOLVED, title="t"),
    ]
    for payload in payloads:
        assert len(payload) <= 100
        for block in payload:
            if block.get("type") == "section":
                assert len(block["text"]["text"]) <= 3000
            if block.get("type") == "header":
                assert len(block["text"]["text"]) <= 150
            if block.get("type") == "actions":
                assert len(block["elements"]) <= 5
                for element in block["elements"]:
                    assert len(element["text"]["text"]) <= 75


def test_truncation_is_marked_never_silent():
    long_text = "x" * 5000
    out = blocks.truncate(long_text)
    assert len(out) <= blocks.SECTION_LIMIT
    assert "truncated" in out


def test_step_action_ids_round_trip():
    action = flows.step_action("judge", "case")
    assert action == "jp:step:judge:case"
    rest = action[len(blocks.ACTION_STEP):]
    flow_id, _, token = rest.partition(":")
    assert (flow_id, token) == ("judge", "case")


def test_menu_blocks_are_json_serializable():
    state = Session(user_id="U1", created_at=0, last_seen=0)
    json.dumps(flows.menu(state).blocks)
    json.dumps(flows.about(state).blocks)
