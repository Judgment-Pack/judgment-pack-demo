"""The Slack self-serve demo for judgment packs.

Module map:

    config.py    environment and secrets (read once, never logged)
    state.py     per-user sessions, event de-duplication, the model rate limit
    runtime.py   the `jpack` binary — every disposition comes from here
    desk.py      the attestation gateway, one per session
    model.py     Gemini, used only to narrate and to draft
    blocks.py    Block Kit builders (pure functions)
    content.py   all the prose, including the About surface
    flows/       the four use cases as a state machine
    app.py       Slack wiring: events, actions, the slash command
    dryrun.py    the same flows on a terminal, with no Slack and no API key

The boundary the whole thing exists to demonstrate holds inside it too: the
runtime decides, the model narrates, and no narration can change a
disposition.
"""

__all__ = []
