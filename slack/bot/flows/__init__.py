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
    return _run(turn, deps, flow)


def _run(turn, deps, flow):
    result = flow.handle(turn, deps)
    replies = list(result.replies)
    if result.done:
        replies = _finish(turn.session, flow, replies)
    return replies
