#!/usr/bin/env python3
"""attest — acquire a sanctions screening through the gateway, verify the
store against the sealed registry under the out-of-band pin, and derive the
graph inputs jpack evaluates.

Verbs (run from a project directory, e.g. /projects/enterprise-demo):

  attest screen "<subject>" --template graphs/inputs-<slug>.json
      POST /acquire then /seal. Writes attested/session.json (the claim of
      what was asked — deliberately NOT the response body: derivation reads
      only store bytes a verified receipt covers).

  attest check [--stdout]
      GET /registry from the key holder, run the reference verifier under the
      pinned key, apply gateway SPEC §5a.1's DELIBERATE session-scoped
      verdict (declared in scoped_verdict, with why), re-digest the
      artifact, run the specified derivation rule, and write
      attested/screening-inputs.json.
      Exit 0 derived (resolved or absent), 3 withheld (inputs written with
      evidence "unknown" — the answer, not an error, whether from a failed
      verification or from the rule itself), 4 the verifier could not reach a
      verdict at all, 1 could not even start (no gateway, no pin, no session,
      bad template). Any stale inputs document is removed before anything
      else, so no failure path leaves a previous run's positive result behind.

  attest decide <pack-id> --facts <file> --evidence <file>
      Ask the DECISION desk to judge, and receipt the judgment. The desk
      evaluates against a copy of this project baked into the gateway
      container from the checkout the image was built from; the working tree
      the agent can edit is not the tree that judges. Verifies exactly as
      `check` does, then writes attested/decision.json from the receipted
      artifact bytes. Exit 0 decided, 3 NO DECISION (this run's own receipt or
      artifact failed verification — nothing is written), 4 the verifier could
      not reach a verdict at all, 1 could not even start.

  attest tamper [--match-count N]
      Edit the attested matchCount inside the content-addressed artifact the
      session's receipt cites. The next `attest check` must report
      artifact-mismatch and withhold.

  attest rollback
      Delete the session's newest receipt. The registry seal now promises more
      receipts than the store holds: tail-rollback, and unlike an artifact
      edit, nothing can recreate the signed receipt.

The model is not in this code path: what lands in the facts document is a pure
function of the rule and store bytes, or a withholding.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8787")
AUTHORITY = os.environ.get("GATEWAY_AUTHORITY", "gateway:enterprise-demo")
STORE = os.environ.get("GATEWAY_STORE", "/gateway/store")
# The reference verifier binary: on PATH in the demo image, named explicitly
# on a workstation (the Slack dryrun exports it beside the desk's own copy).
GATEWAY_BIN = os.environ.get("GATEWAY_BIN", "gateway")
PIN = os.environ.get("GATEWAY_PIN", "/gateway-pin/pinned.pubkey")
DERIVE_CLI = os.environ.get(
    "DERIVE_CLI", "/usr/local/share/derivation-rule/derive_cli.py"
)
DERIVE_RULE = os.environ.get(
    "DERIVE_RULE", "/usr/local/share/derivation-rule/rules/screening.rule.json"
)
MAX_AGE_SECONDS = 86400

SESSION_FILE = os.path.join("attested", "session.json")
INPUTS_FILE = os.path.join("attested", "screening-inputs.json")
DECISION_FILE = os.path.join("attested", "decision.json")
REGISTRY_FETCHED = os.path.join("attested", "registry.fetched.jsonl")

RECOVERY = (
    "is the gateway up? Recover with:\n"
    "  docker compose up -d --force-recreate gateway\n"
    "(bare `restart` cannot rejoin the namespace after the sandbox container\n"
    "was recreated, and plain `up -d` is a no-op while it reports Up)"
)


def say(out, *lines):
    for line in lines:
        print(line, file=out)


def fail(*lines, code=1):
    say(sys.stderr, *lines)
    sys.exit(code)


def http_json(path, body):
    request = urllib.request.Request(
        GATEWAY_URL + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace").strip()
        fail(f"attest: gateway answered {error.code} on {path}: {detail}")
    except urllib.error.URLError as error:
        fail(f"attest: cannot reach the gateway at {GATEWAY_URL} ({error.reason});",
             RECOVERY)


def http_fetch(path):
    try:
        with urllib.request.urlopen(GATEWAY_URL + path, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace").strip()
        fail(f"attest: gateway answered {error.code} on {path}: {detail}")
    except urllib.error.URLError as error:
        fail(f"attest: cannot reach the gateway at {GATEWAY_URL} ({error.reason});",
             RECOVERY)


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_session():
    try:
        with open(SESSION_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        fail("attest: no readable attested/session.json — run `attest screen` first")


def load_template(path):
    try:
        with open(path) as f:
            template = json.load(f)
    except (OSError, ValueError) as error:
        fail(f"attest: cannot read template {path}: {error}")
    if not isinstance(template, dict):
        fail(f"attest: template {path} is not a JSON object")
    onboarding = template.get("onboarding")
    if not isinstance(onboarding, dict):
        fail(f"attest: template {path} has no onboarding block")
    facts = onboarding.get("facts")
    vendor = facts.get("vendor") if isinstance(facts, dict) else None
    # One requirement has one source: the graph edge feeds these two members,
    # and the runtime refuses the whole evaluation if the caller sets them too.
    if isinstance(vendor, dict) and "sanctionsScreening" in vendor:
        fail(f"attest: template {path} presets /vendor/sanctionsScreening — "
             "the graph edge is the one source of that fact", code=2)
    evidence = onboarding.get("evidence")
    if isinstance(evidence, dict) and "sanctions-screening" in evidence:
        fail(f"attest: template {path} presets the sanctions-screening evidence — "
             "the graph edge is the one source of that entry", code=2)
    return template


def newest_receipt(session):
    directory = os.path.join(STORE, "receipts", session)
    try:
        indexes = sorted(
            int(name[:-5])
            for name in os.listdir(directory)
            if name.endswith(".json") and name[:-5].isdigit()
        )
    except OSError:
        fail(f"attest: no receipts under {directory} for this session")
    if not indexes:
        fail(f"attest: no receipts under {directory} for this session")
    path = os.path.join(directory, f"{indexes[-1]}.json")
    with open(path) as f:
        return path, json.load(f)


def artifact_path(receipt):
    digest = receipt.get("resultDigest", "")
    hexpart = digest.split(":", 1)[-1]
    if not re.fullmatch(r"[0-9a-f]{64}", hexpart):
        fail(f"attest: receipt resultDigest {digest!r} is not a sha-256 digest")
    return os.path.join(STORE, "artifacts", hexpart), hexpart


def mint_session(prefix, subject):
    """One session id per acquisition, shaped so the store path is a token."""
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]", "-", subject.lower())).strip("-")
    session = "{}-{}-{}-{}".format(
        prefix,
        slug[:40],
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        secrets.token_hex(2),
    )
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", session):
        fail(f"attest: session id {session!r} is not a valid token")
    return session


def receipted_artifact(session, narrate):
    """Verify the store for one session and return the bytes its receipt covers.

    The whole verification ceremony `check` performs, in the order that makes it
    mean anything: the registry from the key holder, the reference verifier under
    the out-of-band pin, the verdict scoped to this session, and the artifact
    re-digested against what the receipt signed. Returns None when any of it
    fails — the caller decides what a failure means, because for a screening it
    is a withholding and for a decision it is no decision at all.
    """
    verdict, (ok, reasons) = run_verify(session)
    say(narrate,
        f"verify    store-wide ok={verdict.get('ok')}  "
        f"this session: {'ok' if ok else ', '.join(reasons)}  (pin {PIN})",
        "          registry fetched from the key holder, not read from the store")
    if not ok:
        return None, "verification failed: " + ", ".join(reasons), None
    _, receipt = newest_receipt(session)
    path, hexpart = artifact_path(receipt)
    try:
        with open(path, "rb") as f:
            artifact_bytes = f.read()
    except OSError:
        return None, "artifact missing from the store", receipt
    if hashlib.sha256(artifact_bytes).hexdigest() != hexpart:
        return None, "artifact re-digest mismatch", receipt
    return artifact_bytes, None, receipt


def cmd_screen(args):
    subject = args.subject.strip()
    if not subject:
        fail("attest: subject must be a non-empty string")
    load_template(args.template)  # fail before acquiring, not after

    session = mint_session("ofac", subject)

    # A new session obsoletes any previously derived document immediately —
    # nothing may evaluate another subject's (or another verdict's) inputs.
    try:
        os.remove(INPUTS_FILE)
    except OSError:
        pass

    acquired = http_json(
        "/acquire",
        {"session": session, "source": "ofac-screening", "arguments": {"subject": subject}},
    )
    sealed = http_json("/seal", {"session": session})

    os.makedirs("attested", exist_ok=True)
    with open(SESSION_FILE, "w") as f:
        json.dump(
            {"session": session, "subject": subject, "template": args.template,
             "acquiredAt": now_utc()},
            f, indent=2)
        f.write("\n")

    receipt = acquired.get("receipt", {})
    say(sys.stdout,
        f"acquired  session {session}",
        f"          authority {receipt.get('authority')}  keyId {receipt.get('keyId')}",
        f"          resultDigest {receipt.get('resultDigest')}",
        f"sealed    finalCount {sealed.get('finalCount', sealed)}",
        "",
        "The response body above the store is informational only — nothing",
        "downstream reads it. Next: attest check")


def scoped_verdict(verdict, session):
    # Gateway SPEC §5a.1's DELIBERATE session-scoped mode, chosen by name: a
    # project store here accumulates sessions across beats, and the tampering
    # this demo stages on purpose leaves earlier sessions dirty — a staged
    # tamper must not withhold every later screening, which is the exact case
    # the spec carved the deliberate mode for. The mode's full check list —
    # own findings all ok and at least one present, registered, seal intact,
    # chain intact — collapses to the scan below because every finding this
    # verifier emits carries sessionId and status (the invariant §5a.1
    # states); a verifier growing a store-level finding that names no session
    # would need this widened.
    mine = [f for f in verdict.get("findings", []) if f.get("sessionId") == session]
    if not mine:
        return False, ["no findings for this session (unregistered or missing store)"]
    bad = sorted({f.get("status") for f in mine if f.get("status") != "ok"})
    if bad:
        return False, bad
    return True, []


def run_verify(session):
    registry = http_fetch("/registry")
    os.makedirs("attested", exist_ok=True)
    with open(REGISTRY_FETCHED, "wb") as f:
        f.write(registry)
    try:
        with open(PIN, "rb") as f:
            pin = f.read()
    except OSError as error:
        fail(f"attest: cannot read the pinned public key at {PIN}: {error}")
    proc = subprocess.run(
        [GATEWAY_BIN, "verify", STORE, REGISTRY_FETCHED, AUTHORITY],
        input=pin, capture_output=True)
    if proc.returncode != 0:
        fail("attest: NO VERDICT — the verifier could not audit the store at all",
             f"  {proc.stderr.decode(errors='replace').strip()}",
             "This is not a tamper verdict. " + RECOVERY, code=4)
    verdict = json.loads(proc.stdout)
    return verdict, scoped_verdict(verdict, session)


def derive(subject, artifact):
    with open(DERIVE_RULE) as f:
        rule = json.load(f)
    request = {"rule": rule, "artifact": artifact,
               "params": {"subject": subject, "asOf": now_utc(),
                          "maxAgeSeconds": MAX_AGE_SECONDS}}
    proc = subprocess.run(
        [sys.executable, DERIVE_CLI], input=json.dumps(request).encode(),
        capture_output=True, cwd=os.path.dirname(DERIVE_CLI))
    if proc.returncode != 0:
        return None
    return json.loads(proc.stdout)


def cmd_check(args):
    state = load_session()
    session, subject = state["session"], state["subject"]
    template = load_template(state["template"])
    narrate = sys.stderr if args.stdout else sys.stdout

    # Remove any previous document FIRST: no failure path below may leave a
    # stale positive behind for the evaluation step to pick up.
    try:
        os.remove(INPUTS_FILE)
    except OSError:
        pass

    artifact_bytes, withheld, _ = receipted_artifact(session, narrate)
    claim = None
    if withheld is None:
        claim = derive(subject, json.loads(artifact_bytes))
        if claim is None:
            withheld = "the derivation rule rejected the artifact"

    if withheld is None:
        screening = {"facts": claim["facts"], "evidence": claim["evidenceAvailability"]}
        say(narrate,
            f"derive    {claim.get('acquisitionStatus')} (reason: {claim.get('reason')})"
            f"  basis {' '.join(claim.get('basis', []))}",
            f"          session {session}",
            f"          receipted screenedLegalName: "
            f"{json.loads(artifact_bytes).get('screenedLegalName')!r} — the desk",
            "          attests THIS string was screened; whether it names the vendor",
            "          under evaluation is the template author's claim, not the desk's")
    else:
        screening = {"facts": {}, "evidence": {"screening-record": "unknown"}}
        say(narrate,
            f"WITHHELD  {withheld}  (session {session})",
            "          the screening evidence is recorded as unknown — that is the",
            "          answer, not an obstacle; the graph escalates from here")

    document = {"screening": screening, "onboarding": template["onboarding"]}
    rendered = json.dumps(document, indent=2) + "\n"
    os.makedirs("attested", exist_ok=True)
    with open(INPUTS_FILE, "w") as f:
        f.write(rendered)
    if args.stdout:
        sys.stdout.write(rendered)
    say(narrate, "",
        f"wrote {INPUTS_FILE}. Next:",
        "  jpack experimental graph evaluate graphs/vendor-onboarding.graph.json \\",
        f"    --inputs {INPUTS_FILE}")
    unknown = screening["evidence"].get("screening-record") == "unknown"
    sys.exit(3 if unknown else 0)


def load_document(path, what):
    try:
        with open(path) as f:
            document = json.load(f)
    except (OSError, ValueError) as error:
        fail(f"attest: cannot read the {what} document {path}: {error}")
    if not isinstance(document, dict):
        fail(f"attest: the {what} document {path} is not a JSON object")
    return document


def cmd_decide(args):
    """Ask the decision desk to judge, and receipt the judgment.

    The desk evaluates against a copy of this project inside the gateway
    container, laid down at image build from the checkout the image was built
    from. The working copy beside this command can be edited, forged, or
    deleted, and none of that reaches the desk — which is the whole point of
    asking it.
    """
    # FIRST, before anything that can fail: a new decide run obsoletes the
    # previous judgment the moment it starts. An argument this command cannot
    # read is exactly the case where a stale decision.json would otherwise sit
    # there looking like this run's answer.
    try:
        os.remove(DECISION_FILE)
    except OSError:
        pass

    pack_id = args.pack_id.strip()
    if not pack_id:
        fail("attest: pack-id must be a non-empty string")
    facts = load_document(args.facts, "facts")
    evidence = load_document(args.evidence, "evidence") if args.evidence else {}

    session = mint_session("desk", pack_id)

    acquired = http_json("/acquire", {
        "session": session,
        "source": "decision-desk",
        "arguments": {"packId": pack_id, "facts": facts, "evidence": evidence},
    })
    sealed = http_json("/seal", {"session": session})
    receipt = acquired.get("receipt", {})
    say(sys.stdout,
        f"acquired  session {session}",
        f"          authority {receipt.get('authority')}  keyId {receipt.get('keyId')}",
        f"          resultDigest {receipt.get('resultDigest')}",
        f"sealed    finalCount {sealed.get('finalCount', sealed)}",
        "")

    artifact_bytes, refused, verified_receipt = receipted_artifact(session, sys.stdout)
    if refused is not None:
        # Not a withholding. A screening that cannot be verified degrades to
        # "unknown", which is an answer the graph acts on; a judgment that
        # cannot be verified is not a judgment at all, and writing one would be
        # the exact thing this desk exists to make impossible.
        say(sys.stderr,
            "",
            f"NO DECISION  {refused}  (session {session})",
            "             nothing was written: an unverifiable judgment is not a",
            "             judgment, and this command will not leave one on disk",
            "             for something downstream to read as one.")
        sys.exit(3)

    decision = json.loads(artifact_bytes)
    disposition = decision.get("disposition", {})
    os.makedirs("attested", exist_ok=True)
    with open(DECISION_FILE, "w") as f:
        f.write(json.dumps(decision, indent=2, sort_keys=True) + "\n")

    outcome = disposition.get("outcomeId")
    headline = disposition.get("kind", "?")
    if outcome:
        headline += f" {outcome}"
    say(sys.stdout,
        "",
        f"decided   {headline}",
        f"          pack {decision.get('packId')} {decision.get('packVersion')}"
        f"  (desk project {decision.get('deskProject')})",
        f"          receipt keyId {verified_receipt.get('keyId')}"
        f"  resultDigest {verified_receipt.get('resultDigest')}",
        f"          handoff {disposition.get('handoff', {}).get('state', '?')}"
        f"  reasons {' '.join(disposition.get('reasons', [])) or '(none)'}",
        "",
        f"wrote {DECISION_FILE}",
        "This judgment came from the desk's own baked copy of the project, not",
        "from the working tree beside you. The copy you can edit is not the copy",
        "that judges — and the receipt above signs which one did.")
    sys.exit(0)


def cmd_tamper(args):
    state = load_session()
    _, receipt = newest_receipt(state["session"])
    path, hexpart = artifact_path(receipt)
    try:
        with open(path, "rb") as f:
            artifact_bytes = f.read()
    except OSError:
        fail(f"attest: artifact missing from the store ({path})")
    artifact = json.loads(artifact_bytes)
    old, new = artifact.get("matchCount"), args.match_count
    if old == new:
        fail(f"attest: matchCount is already {new!r} — pick a different value")
    # The store holds canonical (compact) bytes; build the needle the same way.
    edited = artifact_bytes.replace(
        b'"matchCount":' + json.dumps(old).encode(),
        b'"matchCount":' + json.dumps(new).encode())
    if edited == artifact_bytes:
        fail("attest: could not locate the matchCount bytes to edit")
    with open(path, "wb") as f:
        f.write(edited)
    say(sys.stdout,
        f"tampered  {path}",
        f"          matchCount {old!r} -> {new!r} (the receipt still signs "
        f"sha256:{hexpart})",
        "The stored bytes no longer match what was attested. Next: attest check")


def cmd_rollback(_args):
    state = load_session()
    path, receipt = newest_receipt(state["session"])
    os.remove(path)
    say(sys.stdout,
        f"rolled back  removed {path} (callIndex {receipt.get('callIndex')})",
        "The registry seal still promises that receipt existed; nothing can",
        "re-create its signature. Next: attest check")


def main():
    parser = argparse.ArgumentParser(prog="attest")
    verbs = parser.add_subparsers(dest="verb", required=True)

    screen = verbs.add_parser("screen", help="acquire + seal one screening")
    screen.add_argument("subject")
    screen.add_argument("--template", required=True,
                        help="committed graph-inputs file supplying the onboarding "
                             "block — it must be the scenario written for THIS "
                             "counterparty; the desk does not check that binding")
    screen.set_defaults(run=cmd_screen)

    check = verbs.add_parser("check", help="verify the store, derive the inputs")
    check.add_argument("--stdout", action="store_true",
                       help="write the inputs document to stdout (narration to stderr)")
    check.set_defaults(run=cmd_check)

    decide = verbs.add_parser("decide", help="receipt a judgment from the decision desk")
    decide.add_argument("pack_id", metavar="pack-id",
                        help="the decision id the desk's own jpack.json declares")
    decide.add_argument("--facts", required=True,
                        help="JSON facts document (decimal strings, never numbers)")
    decide.add_argument("--evidence",
                        help="optional JSON evidence-availability document")
    decide.set_defaults(run=cmd_decide)

    tamper = verbs.add_parser("tamper", help="edit the attested artifact in the store")
    tamper.add_argument("--match-count", default="0")
    tamper.set_defaults(run=cmd_tamper)

    rollback = verbs.add_parser("rollback", help="delete the session's newest receipt")
    rollback.set_defaults(run=cmd_rollback)

    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
