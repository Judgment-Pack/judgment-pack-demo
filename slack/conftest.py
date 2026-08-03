"""Make `bot` importable when pytest is run from the repository root.

    python3 -m pytest slack/

None of the tests import slack_bolt or google-genai: every module except
`bot/app.py` is deliberately free of both, so the flows, the state machine and
the block payloads can be proven without a Slack app or an API key.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
