"""Block Kit builders.

Pure functions: a session (or a payload) in, a list of block dicts out. No
Slack client, no network, no model — which is what lets the tests canonize the
payloads and the dry run print them on a terminal.

Slack's limits are enforced here rather than discovered in production: 3000
characters per section text, 150 per header, 75 per button label.
"""

from __future__ import annotations

import json

from . import content

SECTION_LIMIT = 2900
HEADER_LIMIT = 150
BUTTON_LIMIT = 75

# Action id grammar. Everything the app listens for starts with "jp:".
ACTION_PREFIX = "jp:"
ACTION_MENU = "jp:menu"
ACTION_ABOUT = "jp:about"
ACTION_START = "jp:start:"  # + flow id
ACTION_STEP = "jp:step:"  # + flow id + ":" + token


TRUNCATED = "\n… (truncated for Slack; the full record is in your session)"


# Whether "Talk to a human" buttons render. Set once at boot from the
# presence of a triage channel: a button that promises a person must have
# somewhere for the note to land.
_HUMAN_HANDOFF = False


def configure_human_handoff(enabled):
    global _HUMAN_HANDOFF
    _HUMAN_HANDOFF = bool(enabled)


def human_button():
    """The path to a person, or None when none is configured."""
    if not _HUMAN_HANDOFF:
        return None
    return button("Talk to a human", "jp:human")


def truncate(text, limit=SECTION_LIMIT):
    """Cut to fit, and say so — a silently shortened payload is a lie."""
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(TRUNCATED))
    return text[:keep] + TRUNCATED


def header(text):
    return {"type": "header", "text": {"type": "plain_text", "text": truncate(text, HEADER_LIMIT)}}


def section(markdown):
    return {"type": "section", "text": {"type": "mrkdwn", "text": truncate(markdown)}}


def context(markdown):
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": truncate(markdown, 2000)}]}


def divider():
    return {"type": "divider"}


def button(label, action_id, value=None, style=None):
    element = {
        "type": "button",
        "text": {"type": "plain_text", "text": truncate(label, BUTTON_LIMIT)},
        "action_id": action_id,
        "value": value if value is not None else action_id,
    }
    if style:
        element["style"] = style
    return element


def actions(buttons):
    return {"type": "actions", "elements": list(buttons)}


def code(text, limit=SECTION_LIMIT - 20):
    body = truncate(text, limit)
    return section("```\n" + body + "\n```")


def json_code(value, limit=SECTION_LIMIT - 20):
    return code(json.dumps(value, indent=2, sort_keys=True), limit)


# --- menu ------------------------------------------------------------------


def flow_button(flow, done, index):
    label = "{}. {}".format(index, flow.TITLE)
    if done:
        label = "✓ " + label + " (again)"
    return button(label, ACTION_START + flow.ID, style=None if done else "primary")


def menu_blocks(catalogue, completed, intro=None):
    """The use-case menu. `catalogue` is the ordered list of flow modules."""
    blocks = []
    if intro:
        blocks.append(section(intro))
    remaining = [f for f in catalogue if f.ID not in completed]
    if remaining:
        blocks.append(section(content.MENU_PROMPT))
    else:
        blocks.append(section(content.ALL_DONE))
    for index, flow in enumerate(catalogue, start=1):
        if flow.ID in completed and remaining:
            continue
        blocks.append(section(content.FLOW_BLURBS[flow.ID]))
    buttons = [
        flow_button(flow, flow.ID in completed, index)
        for index, flow in enumerate(catalogue, start=1)
    ]
    buttons.append(button("About this demo", ACTION_ABOUT))
    human = human_button()
    if human:
        buttons.append(human)
    # Slack allows at most five elements per actions block.
    for start in range(0, len(buttons), 5):
        blocks.append(actions(buttons[start:start + 5]))
    if completed:
        blocks.append(
            context(
                "Completed: "
                + ", ".join(sorted(completed))
                + "  ·  {}/{} use cases".format(len(completed), len(catalogue))
            )
        )
    return blocks


def next_steps_blocks(catalogue, completed, finished_flow_title):
    """What every flow ends with: the remaining use cases, and About."""
    remaining = [f for f in catalogue if f.ID not in completed]
    blocks = [divider()]
    if remaining:
        blocks.append(
            section(
                "*Done: {}.* {} left. Which one next?".format(
                    finished_flow_title, len(remaining)
                )
            )
        )
        for flow in remaining:
            blocks.append(section(content.FLOW_BLURBS[flow.ID]))
        buttons = [
            button(flow.TITLE, ACTION_START + flow.ID, style="primary") for flow in remaining
        ]
    else:
        blocks.append(section(content.ALL_DONE))
        buttons = [button("Run one again", ACTION_MENU)]
    buttons.append(button("About this demo", ACTION_ABOUT))
    human = human_button()
    if human:
        buttons.append(human)
    for start in range(0, len(buttons), 5):
        blocks.append(actions(buttons[start:start + 5]))
    return blocks


# --- about -----------------------------------------------------------------


def about_blocks(catalogue=None, completed=None):
    blocks = [header(content.ABOUT_HEADER), section("*What is built*")]
    for title, body in content.ABOUT_BUILT:
        blocks.append(section("*" + title + "* — " + body))
    blocks.append(divider())
    blocks.append(section(content.ABOUT_DISCUSSED))
    blocks.append(divider())
    blocks.append(section(content.ABOUT_INVITE))
    blocks.append(context(content.BOUNDARY))
    human = human_button()
    if human:
        blocks.append(actions([human]))
    if catalogue is not None:
        blocks.append(divider())
        blocks.extend(menu_blocks(catalogue, completed or set()))
    return blocks


# --- dispositions ----------------------------------------------------------

_KIND_EMOJI = {
    "outcome": ":white_check_mark:",
    "unresolved": ":large_orange_diamond:",
    "not-applicable": ":white_circle:",
}

_OUTCOME_EMOJI = {
    "approve": ":white_check_mark:",
    "reject": ":no_entry:",
    "request-info": ":envelope_with_arrow:",
    "clear": ":white_check_mark:",
    "match": ":no_entry:",
}


def disposition_headline(disposition):
    kind = disposition.get("kind", "?")
    if kind == "outcome":
        outcome = disposition.get("outcomeId", "?")
        emoji = _OUTCOME_EMOJI.get(outcome, _KIND_EMOJI.get(kind, ":black_square:"))
        return "{} *{}* — `{}`".format(emoji, kind, outcome)
    reasons = disposition.get("reasons") or []
    emoji = _KIND_EMOJI.get(kind, ":large_orange_diamond:")
    return "{} *{}* — reasons: {}".format(
        emoji, kind, ", ".join("`" + r + "`" for r in reasons) or "_none_"
    )


def handoff_line(payload):
    disposition = payload.get("disposition", {})
    handoff = disposition.get("handoff", {}) or {}
    state = handoff.get("state", "none")
    if state == "none":
        return "Handoff: `none`."
    triggered = ", ".join("`" + t + "`" for t in handoff.get("triggeredBy", []) or [])
    target = payload.get("handoffTarget") or {}
    line = "Handoff: `{}`".format(state)
    if triggered:
        line += ", triggered by {}".format(triggered)
    if target:
        line += " → *{}* ({})".format(target.get("name", "?"), target.get("kind", "?"))
    return line + ". _A requested handoff is a request recorded, not a delivery._"


def trace_lines(payload, limit=12):
    lines = []
    for entry in (payload.get("trace") or [])[:limit]:
        stage = entry.get("stage", "?")
        ident = entry.get("id")
        condition = entry.get("condition", "?")
        bullet = "• `{}`".format(stage if ident is None else "{} {}".format(stage, ident))
        bullet += " → condition *{}*".format(condition)
        if entry.get("outcome"):
            bullet += ", outcome `{}`".format(entry["outcome"])
        if entry.get("effect"):
            bullet += ", effect `{}`".format(entry["effect"])
        if entry.get("onUnknown"):
            bullet += ", onUnknown `{}`".format(entry["onUnknown"])
        lines.append(bullet)
    return lines


def disposition_blocks(payload, title=None, facts=None, evidence=None):
    """The runtime's answer, rendered without a single model token."""
    blocks = []
    if title:
        blocks.append(section("*" + title + "*"))
    disposition = payload.get("disposition", {})
    blocks.append(section(disposition_headline(disposition)))
    if facts is not None or evidence is not None:
        parts = []
        if facts is not None:
            parts.append("facts " + json.dumps(facts, sort_keys=True))
        if evidence is not None:
            parts.append("evidence " + json.dumps(evidence, sort_keys=True))
        blocks.append(context("Inputs as evaluated — " + "  ·  ".join(parts)))
    lines = trace_lines(payload)
    if lines:
        blocks.append(section("*The trace* (the pack-grounded record of what fired)\n" + "\n".join(lines)))
    blocks.append(section(handoff_line(payload)))
    pack_id = payload.get("packId")
    if pack_id:
        blocks.append(
            context(
                "pack `{}` v`{}`  ·  surface `{}`  ·  conformance claim: `{}` in the runtime repo".format(
                    pack_id,
                    payload.get("packVersion", "?"),
                    payload.get("command", "?"),
                    payload.get("conformanceClaimReference", "CONFORMANCE.md"),
                )
            )
        )
    return blocks


def escape_mrkdwn(text):
    """Neutralize Slack's three control characters in untrusted text.

    Model output is the one text in this app nobody reviewed. Unescaped, a
    narration could emit a live link (`<https://…|click here>`) or a broadcast
    (`<!channel>`) — so a prompt injection that the standing data-not-
    instructions rule failed to stop would still reach the workspace as
    something clickable. The rule is an instruction; this is enforcement.
    """
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def model_blocks(text, model_name=None, label="Narration", note=None):
    """The ONLY way model prose reaches Slack: labelled, escaped, bounded.

    Every model utterance in this app carries the model's name and is followed
    by the boundary line, because a demo whose thesis is a visible
    model/runtime line cannot afford one unattributed sentence.
    """
    if not text:
        return [context(note or content.MODEL_UNAVAILABLE)]
    attribution = "*{}* — written by `{}`, which decided nothing".format(
        label, model_name or "the model"
    )
    return [section(attribution + "\n" + escape_mrkdwn(text)), context(content.BOUNDARY)]


def narration_blocks(text, model_name=None, note=None):
    return model_blocks(
        text, model_name, label="Narration (the model reading the payload above)", note=note
    )


def error_blocks(title, detail):
    """A refusal is an answer: show it verbatim rather than smoothing it."""
    return [
        section(":octagonal_sign: *{}*".format(title)),
        code(detail or "(no output)"),
        context(
            "This is reported exactly as the tool said it — refusing loudly is the demo, and a "
            "refusal is never edited away."
        ),
    ]
