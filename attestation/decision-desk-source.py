#!/usr/bin/env python3
"""The decision desk's operator-configured source (`gateway serve --source`).

The screening desk attests an *input*. This one attests a *judgment*: the
gateway execs this program once per /acquire call, writes canon(arguments) to
stdin, and content-addresses + signs whatever single JSON value comes back — so
the disposition itself becomes a receipted artifact rather than something the
caller reports having seen.

What makes it a desk and not a convenience is where the law lives. This program
evaluates against a PRISTINE COPY of the reviewed project, baked into the image
at build time from the BUILD CONTEXT — the checkout the image was built from —
and laid down at $DESK_PROJECT (default
/usr/local/share/desk/enterprise-demo) inside the gateway container. The sandbox
holds its own working copy of the same project, and the agent can edit, forge,
or delete any of it; none of that is reachable from here. The copy you can edit
is not the copy that judges.

The bound that wording states honestly: the desk's law is as reviewed as the tree
that built the image. Build from a reset checkout and that is the reviewed tree;
build mid-forgery and the forgery is what gets baked. Today that is operational
discipline — the image build runs `packs verify` against the baked copy when the
project carries a reviewed-set lock, which arms the moment one is committed — and
not a guarantee this program can make about its own law.

That is the wall ADR-0019 names. The runtime's reviewed-set lock makes an
amendment explicit and recorded inside one write domain; it cannot stop an
editor who shares that domain, because `packs lock` is available to whatever can
edit a pack. Putting the deciding party outside the domain is the part a runtime
check cannot do, and this is it.

The desk's copy declares its own audit directory, so the desk keeps ITS OWN
decision book inside its container — every judgment it renders is recorded on
the desk's side of the wall, where the sandbox cannot reach the record any more
than it can reach the law. That is a feature, not a side effect: the receipt
proves what was decided to whoever holds it, and the desk's book proves what the
desk decided to whoever holds the desk.

CANON DOMAIN. The gateway's canonicalizer refuses float literals and duplicate
members; integers in the safe range are legal. The rule for anyone extending
this file is therefore about the desk's OWN members: keep them string-shaped —
no count, no duration, no timestamp-as-epoch — so nothing this program authors
depends on a numeric encoding surviving a canonicalizer. The echoed facts and
evidence are the caller's and may legitimately carry integers; this demo's do
not, because a JSON number compares as unknown to a pack and escalates. A §8.3
disposition is strings, arrays, and objects by construction, so the judgment
half is string-shaped whatever the caller sent.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

DESK_PROJECT = os.environ.get(
    "DESK_PROJECT", "/usr/local/share/desk/enterprise-demo"
)
JPACK = os.environ.get("JPACK_BIN", "jpack")


def bad(message):
    sys.stderr.write(message + "\n")
    return 2


def refusal_line(stdout_text, stderr_text, code):
    """One line naming why the desk refused, short enough to survive the cap."""
    for text in (stdout_text, stderr_text):
        try:
            envelope = json.loads(text)
        except Exception:
            continue
        if not isinstance(envelope, dict):
            continue
        parts = []
        error = envelope.get("evaluationError")
        if isinstance(error, dict) and error.get("class"):
            parts.append(str(error["class"]))
        diagnostics = envelope.get("diagnostics")
        if isinstance(diagnostics, list) and diagnostics:
            first = diagnostics[0]
            if isinstance(first, dict):
                if first.get("code"):
                    parts.append(str(first["code"]))
                if first.get("message"):
                    parts.append(str(first["message"]))
        if parts:
            return "the desk refused: " + " ".join(parts)
    return f"the desk refused this evaluation (evaluator exit {code})"


def main():
    try:
        arguments = json.load(sys.stdin)
    except Exception:
        return bad("arguments: not a JSON value")
    if not isinstance(arguments, dict):
        return bad("arguments: must be a JSON object")

    pack_id = arguments.get("packId")
    if not isinstance(pack_id, str) or not pack_id.strip():
        return bad('arguments: a non-empty string "packId" is required')
    pack_id = pack_id.strip()
    # The canonical domain admits any string, control characters included, and a
    # NUL reaching subprocess is a traceback rather than the refusal every other
    # path here produces. A decision id is a token; hold it to one.
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", pack_id):
        return bad(
            'arguments: "packId" must be a decision id token '
            "(letters, digits, dot, underscore, hyphen; at most 128)"
        )

    facts = arguments.get("facts")
    if not isinstance(facts, dict):
        return bad('arguments: "facts" must be a JSON object')

    # An absent evidence document and an empty one mean the same thing to this
    # desk, and §8.2 makes an omitted document the implicit empty object: the
    # caller may leave it out.
    evidence = arguments.get("evidence", {})
    if evidence is None:
        evidence = {}
    if not isinstance(evidence, dict):
        return bad('arguments: "evidence" must be a JSON object when present')

    config = os.path.join(DESK_PROJECT, "jpack.json")
    with tempfile.TemporaryDirectory() as workspace:
        facts_path = os.path.join(workspace, "facts.json")
        evidence_path = os.path.join(workspace, "evidence.json")
        with open(facts_path, "w") as f:
            json.dump(facts, f)
        with open(evidence_path, "w") as f:
            json.dump(evidence, f)
        try:
            proc = subprocess.run(
                [
                    JPACK, "experimental", "evaluate",
                    "--config", config,
                    "--pack-id", pack_id,
                    "--facts", facts_path,
                    "--evidence", evidence_path,
                    "--format", "json",
                ],
                capture_output=True,
                # Under the gateway's own 30-second context, so the desk returns
                # a refusal it authored rather than being killed mid-call with
                # the caller's documents left in an unswept temporary directory.
                timeout=25,
            )
        except subprocess.TimeoutExpired:
            return bad("the desk's evaluator did not finish within 25 seconds")

    # A refused evaluation is not a disposition (§8.4) and must not become one.
    # The gateway turns a nonzero exit into a 400 carrying this stderr, so the
    # caller learns what the desk refused rather than receiving a receipt for a
    # judgment nobody made.
    if proc.returncode != 0:
        # The gateway caps a source's stderr at 200 bytes when it builds the
        # 400, so the reason has to come FIRST and short. The envelope is parsed
        # for its diagnostic code, message, and §8.4 class where there is one;
        # the raw streams follow, for whoever reads the desk's own logs. An
        # `unsupported` refusal is reported on stdout by design, so both streams
        # are searched.
        raw_out = proc.stdout.decode(errors="replace")
        raw_err = proc.stderr.decode(errors="replace")
        sys.stderr.write(refusal_line(raw_out, raw_err, proc.returncode) + "\n")
        detail = (raw_err + raw_out).strip()
        if detail:
            sys.stderr.write(detail + "\n")
        return 2

    try:
        envelope = json.loads(proc.stdout)
    except Exception:
        return bad("the desk's evaluator did not emit a JSON envelope")
    disposition = envelope.get("disposition")
    if not isinstance(disposition, dict):
        return bad("the desk's evaluator emitted no disposition object")

    artifact = {
        # The id the caller asked for, verbatim, beside the identity the desk
        # actually applied. The receipt's argumentsDigest is keyed and opaque to
        # a public verifier, so without this member nothing externally checkable
        # ties the answer to the question.
        "requestedDecisionId": pack_id,
        "packId": envelope.get("packId", pack_id),
        "packVersion": envelope.get("packVersion", ""),
        # Verbatim: the §8.3 portable disposition as the evaluator produced it.
        # Re-deriving or re-shaping it here would put a second serializer
        # between the judgment and the signature.
        "disposition": disposition,
        # The inputs echoed, so the receipt binds the question to the answer:
        # a signed disposition with no record of what it was asked about is a
        # signature over half a sentence.
        "facts": facts,
        "evidence": evidence,
        # Derived, never a literal: a signed artifact must not assert a
        # provenance label independent of the project that actually judged.
        "deskProject": os.path.basename(os.path.normpath(DESK_PROJECT)),
        "evaluatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    json.dump(artifact, sys.stdout, separators=(",", ":"), sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
