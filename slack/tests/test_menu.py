"""The menu and the remaining-options logic — the promise the app makes:
after finishing any use case, you are offered the ones you have not done."""

from __future__ import annotations

import time

from bot import blocks, content, flows
from bot.state import Session


def session(completed=()):
    now = time.time()
    return Session(user_id="U1", created_at=now, last_seen=now, completed=set(completed))


def button_labels(block_list):
    labels = []
    for block in block_list:
        if block.get("type") == "actions":
            labels += [element["text"]["text"] for element in block["elements"]]
    return labels


def action_ids(block_list):
    ids = []
    for block in block_list:
        if block.get("type") == "actions":
            ids += [element["action_id"] for element in block["elements"]]
    return ids


def test_catalogue_is_the_four_use_cases_in_order():
    assert [flow.ID for flow in flows.CATALOGUE] == ["judge", "author", "attested", "book"]
    assert set(content.FLOW_BLURBS) == set(flows.BY_ID)


def test_remaining_drops_completed_flows():
    assert [f.ID for f in flows.remaining(set())] == ["judge", "author", "attested", "book"]
    assert [f.ID for f in flows.remaining({"judge", "book"})] == ["author", "attested"]
    assert flows.remaining({"judge", "author", "attested", "book"}) == []


def test_menu_offers_every_use_case_and_about():
    payload = flows.menu(session()).blocks
    ids = action_ids(payload)
    for flow in flows.CATALOGUE:
        assert blocks.ACTION_START + flow.ID in ids
    assert blocks.ACTION_ABOUT in ids


def test_menu_marks_completed_and_shows_progress():
    payload = flows.menu(session({"judge"})).blocks
    labels = button_labels(payload)
    assert any(label.startswith("✓") and "Judge a vendor" in label for label in labels)
    contexts = [b for b in payload if b.get("type") == "context"]
    assert any("1/4" in element["text"] for b in contexts for element in b["elements"])


def test_next_steps_offers_only_what_is_left():
    payload = blocks.next_steps_blocks(flows.CATALOGUE, {"judge"}, "Judge a vendor")
    ids = action_ids(payload)
    assert blocks.ACTION_START + "judge" not in ids
    for flow_id in ("author", "attested", "book"):
        assert blocks.ACTION_START + flow_id in ids
    text = " ".join(str(b) for b in payload)
    assert "3 left" in text


def test_next_steps_after_the_last_one_closes_the_demo():
    payload = blocks.next_steps_blocks(
        flows.CATALOGUE, {"judge", "author", "attested", "book"}, "The decision book"
    )
    text = " ".join(str(b) for b in payload)
    assert "all four" in text
    assert content.DEMO_REPO in text


def test_about_names_every_repository_and_the_invitation():
    payload = flows.about(session()).blocks
    text = " ".join(str(b) for b in payload)
    for link in (
        content.RUNTIME_REPO,
        content.DEMO_REPO,
        content.GATEWAY_REPO,
        content.CONFORMANCE,
        content.RFC_0003,
    ):
        assert link in text
    assert "Open an issue" in text
    assert "byte-lineage, not truth" in text
