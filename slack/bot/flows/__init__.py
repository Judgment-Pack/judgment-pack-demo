"""The four use cases, as a small explicit state machine.

A flow is a module with an ID, a TITLE, and one `handle(turn, deps)` function.
The turn carries the user's session (which holds the step counter, the
completed set, and the scratch directory); the function returns replies and
says whether the flow is finished. Nothing here talks to Slack, which is what
makes the whole thing testable and lets the dry run drive it from a terminal.

The router owns three invariants, so no flow has to remember them:

* finishing a flow records it in the completed set and offers the REMAINING
  use cases;
* an action for a flow the user is not in switches them into that flow rather
  than being ignored;
* a message with no active flow is conversational glue plus the menu.
"""

from __future__ import annotations

import re

from .. import blocks, content
from .base import Deps, FlowResult, Reply, Turn, reply, step_action, step_button
from . import attested, author, book, judge

CATALOGUE = [judge, author, attested, book]
BY_ID = {flow.ID: flow for flow in CATALOGUE}

__all__ = [
    "CATALOGUE",
    "BY_ID",
    "Deps",
    "FlowResult",
    "Reply",
    "Turn",
    "reply",
    "step_action",
    "step_button",
    "about",
    "dispatch",
    "menu",
    "remaining",
    "start",
    "welcome",
]


def remaining(completed):
    return [flow for flow in CATALOGUE if flow.ID not in completed]


def catalogue_index(flow_id):
    for index, flow in enumerate(CATALOGUE, start=1):
        if flow.ID == flow_id:
            return index
    return 0


def menu(session, intro=None):
    return reply(
        blocks.menu_blocks(CATALOGUE, session.completed, intro=intro),
        text="Pick a use case",
    )


def about(session):
    return reply(blocks.about_blocks(CATALOGUE, session.completed), text="About this demo")


def welcome(session):
    return reply(
        [blocks.header(content.WELCOME_HEADER)]
        + blocks.menu_blocks(CATALOGUE, session.completed, intro=content.WELCOME),
        text="Welcome to the Judgment Pack demo",
    )


def _finish(session, flow, replies):
    session.finish(flow.ID)
    replies.append(
        reply(
            blocks.next_steps_blocks(CATALOGUE, session.completed, flow.TITLE),
            text="What next?",
        )
    )
    return replies


def start(turn, deps, flow_id):
    """Enter a flow from the menu and run its first step."""
    flow = BY_ID.get(flow_id)
    if flow is None:
        return [menu(turn.session, intro="I do not know that use case.")]
    turn.session.enter(flow_id)
    first = Turn(user_id=turn.user_id, session=turn.session, action="start", text=turn.text)
    return _run(first, deps, flow)


_QUESTION_OPENERS = {
    "what", "why", "how", "can", "could", "does", "do", "did", "is", "are",
    "was", "were", "who", "where", "when", "will", "would", "should", "which",
}


def looks_like_question(text):
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped.rstrip(".!").endswith("?"):
        return True
    first = re.sub(r"[^a-z]", "", stripped.split(None, 1)[0].lower())
    return first in _QUESTION_OPENERS


def dispatch(turn, deps):
    """Route a button or a message into the active flow.

    Returns None when there is no active flow: the caller decides whether that
    is a welcome, the menu, or conversational glue.
    """
    session = turn.session
    if session.active_flow is None:
        return None
    flow = BY_ID.get(session.active_flow)
    if flow is None:
        session.active_flow = None
        return [menu(session)]
    if (
        turn.text
        and not turn.action
        and looks_like_question(turn.text)
        and not getattr(flow, "CONSUMES_TEXT", False)
    ):
        # A question is not a step. This flow ignores free text, so advancing
        # on one would silently guess that asking was clicking — the exact
        # move the product exists to refuse.
        glue = deps.model.glue(
            turn.user_id,
            turn.text,
            situation="mid-flow in use case '{}'".format(flow.TITLE),
        )
        body = blocks.model_blocks(glue.text, deps.model.name, label="Host reply", note=glue.note)
        body.append(
            blocks.context(
                "*{}* is paused exactly where you left it — the buttons above still "
                "work, or type `menu` to leave it.".format(flow.TITLE)
            )
        )
        return [reply(body, text="Answered — your use case is paused")]
    return _run(turn, deps, flow)


def _run(turn, deps, flow):
    result = flow.handle(turn, deps)
    replies = list(result.replies)
    if result.done and result.failed:
        turn.session.leave()
        replies.append(
            reply(
                [
                    blocks.section(
                        "*{} stopped before the end*, so it is not marked complete — "
                        "nothing is until you have actually seen it work.".format(flow.TITLE)
                    ),
                    blocks.actions(
                        [
                            blocks.button(
                                "Try again →", blocks.ACTION_START + flow.ID, style="primary"
                            ),
                            blocks.button("Back to the menu", blocks.ACTION_MENU),
                        ]
                    ),
                ],
                text="Not completed",
            )
        )
        return replies
    if result.done:
        replies = _finish(turn.session, flow, replies)
    return replies
