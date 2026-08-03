"""The types a flow speaks: a turn in, replies out.

Separate from the router so flow modules and the router can import each other's
vocabulary without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from .. import blocks


@dataclass
class Deps:
    """Everything a flow is allowed to reach: no globals, no Slack client.

    `sink` is the one transport-shaped member: a callable the app supplies so
    a flow can push the runtime's answer to Slack the moment it exists,
    instead of holding it until the turn returns. It is None in the tests and
    the dry run, where replies are simply batched.
    """

    config: Any
    runtime: Any
    desk: Any
    model: Any
    sink: Optional[Any] = None


@dataclass
class Turn:
    user_id: str
    session: Any
    action: Optional[str] = None  # the step token from a button, if any
    text: Optional[str] = None  # what the user typed, if anything


@dataclass
class Reply:
    blocks: List[dict]
    text: str = "Judgment Pack demo"


@dataclass
class FlowResult:
    replies: List[Reply] = field(default_factory=list)
    done: bool = False
    # True when the flow ended without showing what it exists to show — an
    # unavailable binary, a refused acquisition, a dead desk. Done-and-failed
    # leaves the flow but never records it as completed: a use case nobody
    # saw work is not a use case anybody finished.
    failed: bool = False


def reply(block_list, text="Judgment Pack demo"):
    return Reply(blocks=block_list, text=text)


def flush(deps, replies):
    """Deliver these replies NOW if the transport allows, and stop owning them.

    Every flow calls this with the runtime's answer before it calls the model.
    A disposition that waited on a narration would be a disposition that a
    slow, metered, third-party service could delay or lose — which is the
    exact dependency this demo says it does not have. Returns whatever still
    has to be batched (everything, when there is no sink).
    """
    replies = [one for one in replies if one is not None]
    if deps is not None and getattr(deps, "sink", None) is not None and replies:
        deps.sink(replies)
        return []
    return replies


def step_action(flow_id, token):
    """The action id a flow's own buttons carry."""
    return blocks.ACTION_STEP + flow_id + ":" + token


def step_button(flow_id, token, label, style="primary"):
    return blocks.button(label, step_action(flow_id, token), style=style)


def continue_bar(flow_id, token, label, extra=None):
    """The one-button bar every mid-flow step ends with."""
    buttons = [step_button(flow_id, token, label)]
    if extra:
        buttons.extend(extra)
    buttons.append(blocks.button("Back to the menu", blocks.ACTION_MENU, style=None))
    return blocks.actions(buttons)
