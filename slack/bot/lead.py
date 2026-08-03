"""The path to a human: the lead modal and the triage message.

Pure builders and parsers — no Slack client, no globals — so the tests can
pin the payload shapes and the escaping without a workspace. `bot/app.py`
wires these to `views_open` and the `view_submission` event; the private
triage channel is the whole backend. For a two-person company the channel IS
the CRM: threads are tickets, and nothing here builds a queue to buy.

What is deliberately recorded WITH each lead: the consent checkbox's state
and the version tag of the consent wording it was shown with. What is
deliberately NOT collected: anything the user did not type — no profile
harvesting, no email lookup.
"""

from __future__ import annotations

from . import blocks

CALLBACK_ID = "jp:lead"
ACTION_OPEN = "jp:human"
CONSENT_VERSION = "consent-2026-08-v1"
CONSENT_WORDING = "You may contact me about this (otherwise the note is read, not replied to)"

# Block/action ids inside the modal. The parser and the builder must agree,
# and the tests hold them to it.
FIELD_NAME = ("lead_name", "value")
FIELD_COMPANY = ("lead_company", "value")
FIELD_DECISION = ("lead_decision", "value")
FIELD_EMAIL = ("lead_email", "value")
FIELD_CONSENT = ("lead_consent", "value")


def _input(block_id, label, *, multiline=False, optional=False, placeholder=None):
    element = {
        "type": "plain_text_input",
        "action_id": "value",
        "multiline": multiline,
    }
    if placeholder:
        element["placeholder"] = {"type": "plain_text", "text": placeholder}
    return {
        "type": "input",
        "block_id": block_id,
        "optional": optional,
        "label": {"type": "plain_text", "text": label},
        "element": element,
    }


def build_modal():
    """The lead modal. Opened straight off the trigger_id — never via _turn:
    a trigger_id is single-use and dies in ~3 seconds, and the turn lock can
    hold a turn far longer than that."""
    return {
        "type": "modal",
        "callback_id": CALLBACK_ID,
        "title": {"type": "plain_text", "text": "Talk to a human"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Not now"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "This goes to the people who build Judgment Pack — a person "
                        "reads it, usually within a day. Only what you type here is "
                        "sent."
                    ),
                },
            },
            _input(FIELD_NAME[0], "Your name"),
            _input(FIELD_COMPANY[0], "Company / team", optional=True),
            _input(
                FIELD_DECISION[0],
                "The decision you wish this could encode",
                multiline=True,
                placeholder="A policy, an approval, a screening — in your own words",
            ),
            _input(
                FIELD_EMAIL[0],
                "Email (only if you want a reply outside Slack)",
                optional=True,
            ),
            {
                "type": "input",
                "block_id": FIELD_CONSENT[0],
                "optional": True,
                "label": {"type": "plain_text", "text": "Contact"},
                "element": {
                    "type": "checkboxes",
                    "action_id": "value",
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": CONSENT_WORDING},
                            "value": CONSENT_VERSION,
                        }
                    ],
                },
            },
        ],
    }


def _field(values, field):
    block_id, action_id = field
    holder = (values.get(block_id) or {}).get(action_id) or {}
    return (holder.get("value") or "").strip()


def parse_submission(view):
    """The typed fields out of a view_submission payload, nothing else."""
    values = (view.get("state") or {}).get("values") or {}
    consent_options = (
        (values.get(FIELD_CONSENT[0]) or {}).get(FIELD_CONSENT[1]) or {}
    ).get("selected_options") or []
    return {
        "name": _field(values, FIELD_NAME),
        "company": _field(values, FIELD_COMPANY),
        "decision": _field(values, FIELD_DECISION),
        "email": _field(values, FIELD_EMAIL),
        "consent": bool(consent_options),
    }


def triage_blocks(user_id, username, lead, session):
    """The message the triage channel gets. Everything user-typed is escaped:
    this is the one place third-party text lands in a channel other people
    read, and an injected `<!channel>` or a live link must arrive as text."""
    esc = blocks.escape_mrkdwn
    who = "<@{}>".format(user_id) if user_id else "?"
    lines = [
        "*New lead* from {} ({})".format(who, esc(username) or "unknown handle"),
        "*Name:* {}".format(esc(lead["name"]) or "—"),
        "*Company:* {}".format(esc(lead["company"]) or "—"),
        "*Email:* {}".format(esc(lead["email"]) or "—"),
        "*Contact consent:* {} ({})".format(
            "yes" if lead["consent"] else "no", CONSENT_VERSION
        ),
    ]
    where = "menu"
    if session is not None:
        if session.active_flow:
            where = "{} step {}".format(session.active_flow, session.step)
        lines.append(
            "*Where they are:* {} · completed: {}".format(
                where, ", ".join(sorted(session.completed)) or "none"
            )
        )
    body = [
        blocks.section("\n".join(lines)),
        blocks.section("*The decision they want encoded:*"),
        blocks.code(esc(lead["decision"]) or "(empty)"),
        blocks.context(
            "Reply in this thread for the record; DM {} to answer them directly.".format(who)
        ),
    ]
    return body


CONFIRMATION = (
    ":white_check_mark: *Sent — a human reads this, usually within a day.* "
    "Replies come back here in our DM{email}. Meanwhile everything in the demo "
    "keeps working, and nothing else about you was collected."
)

FALLBACK = (
    ":octagonal_sign: *I could not reach the humans just now* — that is a delivery "
    "failure on our side, not a judgment on your note. It was NOT saved anywhere, "
    "so please file it at {issues} or try the button again in a minute."
)


def confirmation_text(lead):
    return CONFIRMATION.format(
        email=", or at the email you gave" if (lead["email"] and lead["consent"]) else ""
    )
