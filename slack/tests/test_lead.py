"""The lead path: modal shape, submission parsing, and the triage message.

What is pinned: that the builder and the parser agree on ids, that consent is
recorded with its wording version, that user-typed text cannot smuggle a
broadcast or a live link into the triage channel, and that the message names
where in the demo the person was standing.
"""

from __future__ import annotations

import time

from bot import blocks, lead
from bot.state import Session


def submission(**fields):
    """A view_submission `view` with the given typed values."""
    values = {
        lead.FIELD_NAME[0]: {"value": {"value": fields.get("name", "")}},
        lead.FIELD_COMPANY[0]: {"value": {"value": fields.get("company", "")}},
        lead.FIELD_DECISION[0]: {"value": {"value": fields.get("decision", "")}},
        lead.FIELD_EMAIL[0]: {"value": {"value": fields.get("email", "")}},
        lead.FIELD_CONSENT[0]: {
            "value": {
                "selected_options": (
                    [{"value": lead.CONSENT_VERSION}] if fields.get("consent") else []
                )
            }
        },
    }
    return {"callback_id": lead.CALLBACK_ID, "state": {"values": values}}


def test_the_modal_and_the_parser_agree_on_every_field():
    modal = lead.build_modal()
    block_ids = {b.get("block_id") for b in modal["blocks"] if b["type"] == "input"}
    for field in (
        lead.FIELD_NAME,
        lead.FIELD_COMPANY,
        lead.FIELD_DECISION,
        lead.FIELD_EMAIL,
        lead.FIELD_CONSENT,
    ):
        assert field[0] in block_ids
    parsed = lead.parse_submission(
        submission(name="Ada", company="Acme", decision="Vendor gifts", email="a@b.c", consent=True)
    )
    assert parsed == {
        "name": "Ada",
        "company": "Acme",
        "decision": "Vendor gifts",
        "email": "a@b.c",
        "consent": True,
    }


def test_the_modal_asks_before_it_contacts():
    modal = lead.build_modal()
    consent = [b for b in modal["blocks"] if b.get("block_id") == lead.FIELD_CONSENT[0]][0]
    assert consent["optional"] is True
    option = consent["element"]["options"][0]
    assert option["value"] == lead.CONSENT_VERSION
    assert "contact me" in option["text"]["text"]


def test_unchecked_consent_parses_as_no():
    parsed = lead.parse_submission(submission(name="Ada", decision="x", consent=False))
    assert parsed["consent"] is False


def test_typed_text_cannot_broadcast_into_the_triage_channel():
    hostile = "<!channel> click <https://evil.example|here>"
    body = lead.triage_blocks(
        "U1",
        "ada",
        {"name": hostile, "company": "", "decision": hostile, "email": "", "consent": False},
        None,
    )
    rendered = str(body)
    assert "<!channel>" not in rendered
    assert "<https://evil.example|here>" not in rendered
    assert "&lt;!channel&gt;" in rendered


def test_the_triage_message_says_where_they_were_standing():
    now = time.time()
    session = Session(user_id="U1", created_at=now, last_seen=now)
    session.active_flow = "attested"
    session.step = 1
    session.completed = {"judge"}
    body = lead.triage_blocks(
        "U1", "ada", {"name": "Ada", "company": "", "decision": "d", "email": "", "consent": True}, session
    )
    rendered = str(body)
    assert "attested step 1" in rendered
    assert "judge" in rendered
    assert lead.CONSENT_VERSION in rendered


def test_the_confirmation_promises_email_only_with_consent():
    with_email = {"name": "", "company": "", "decision": "", "email": "a@b.c", "consent": True}
    without = {"name": "", "company": "", "decision": "", "email": "a@b.c", "consent": False}
    assert "email" in lead.confirmation_text(with_email)
    assert "email" not in lead.confirmation_text(without)


def test_the_human_button_appears_only_when_a_channel_is_configured():
    from bot import flows

    try:
        blocks.configure_human_handoff(True)
        menu = str(blocks.menu_blocks(flows.CATALOGUE, set()))
        next_steps = str(blocks.next_steps_blocks(flows.CATALOGUE, {"judge"}, "Judge a vendor"))
        about = str(blocks.about_blocks())
        assert lead.ACTION_OPEN in menu
        assert lead.ACTION_OPEN in next_steps
        assert lead.ACTION_OPEN in about
    finally:
        blocks.configure_human_handoff(False)
    assert lead.ACTION_OPEN not in str(blocks.menu_blocks(flows.CATALOGUE, set()))
