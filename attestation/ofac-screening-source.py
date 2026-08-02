#!/usr/bin/env python3
"""The screening desk's operator-configured source (`gateway serve --source`).

Runs INSIDE the gateway's trust domain: the gateway execs this program once per
/acquire call, writes canon(arguments) to stdin, and content-addresses + signs
whatever single JSON value comes back on stdout. The sandbox container holds a
copy of this file too (same image), but the gateway consults the copy in its
own container — nothing the agent can edit feeds this program.

The desk performs the screening at call time against a baked snapshot
(watchlist.json, also inside the trust domain) and emits the artifact shape the
derivation rule (rules/screening.rule.json) consumes: /status,
/screenedLegalName, /observedAt, /datedRecord, /matchCount — the count as a
decimal STRING, because the rule requires isDecimalString and the packs compare
decimal strings. `observedAt` is the moment of this screening (second
resolution), so freshness holds by construction and artifacts are distinct per
second — a re-acquisition in a later second cannot silently heal a tampered
older artifact.

The gateway's canonical domain refuses float literals and duplicate members;
everything emitted here is strings, booleans, and arrays of the same.
"""

import json
import os
import sys
from datetime import datetime, timezone

WATCHLIST = os.environ.get("WATCHLIST", "/usr/local/share/ofac/watchlist.json")


def main():
    try:
        arguments = json.load(sys.stdin)
    except Exception:
        sys.stderr.write("arguments: not a JSON value\n")
        return 2
    subject = arguments.get("subject") if isinstance(arguments, dict) else None
    if not isinstance(subject, str) or not subject.strip():
        sys.stderr.write('arguments: a non-empty string "subject" is required\n')
        return 2

    with open(WATCHLIST) as f:
        world = json.load(f)
    hits = [e for e in world["entries"] if e["name"].casefold() == subject.casefold()]

    artifact = {
        "status": "found",
        "screenedLegalName": subject,
        "observedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "datedRecord": True,
        "matchCount": str(len(hits)),
        "matches": [
            {"listEntry": e["name"], "program": e["program"], "profile": e["profile"]}
            for e in hits
        ],
        "lists": world["lists"],
        "listSnapshot": world["listSnapshot"],
        "sourceNote": "SYNTHETIC DEMO FIXTURE - the desk screens against a baked snapshot; no real sanctions list is queried",
    }
    json.dump(artifact, sys.stdout, separators=(",", ":"), sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
