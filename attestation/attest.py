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
      pinned key, scope the verdict to this session, re-digest the artifact,
      run the specified derivation rule, and write attested/screening-inputs.json.
      Exit 0 derived (resolved or absent), 3 withheld (inputs written with
      evidence "unknown" — the answer, not an error, whether from a failed
      verification or from the rule itself), 4 the verifier could not reach a
      verdict at all, 1 could not even start (no gateway, no pin, no session,
      bad template). Any stale inputs document is removed before anything
      else, so no failure path leaves a previous run's positive result behind.

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


def cmd_screen(args):
    subject = args.subject.strip()
    if not subject:
        fail("attest: subject must be a non-empty string")
    load_template(args.template)  # fail before acquiring, not after

    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]", "-", subject.lower())).strip("-")
    session = "ofac-{}-{}-{}".format(
        slug[:40],
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        secrets.token_hex(2),
    )
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", session):
        fail(f"attest: session id {session!r} is not a valid token")

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
    mine = [f for f in verdict.get("findings", []) if f.get("sessionId") == session]
    bad = sorted({f.get("status") for f in mine if f.get("status") != "ok"})
    if not mine:
        return False, ["no findings for this session (unregistered or missing store)"]
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
        ["gateway", "verify", STORE, REGISTRY_FETCHED, AUTHORITY],
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

    verdict, (ok, reasons) = run_verify(session)
    say(narrate,
        f"verify    store-wide ok={verdict.get('ok')}  "
        f"this session: {'ok' if ok else ', '.join(reasons)}  (pin {PIN})",
        "          registry fetched from the key holder, not read from the store")

    withheld = None
    claim = None
    if not ok:
        withheld = "verification failed: " + ", ".join(reasons)
    else:
        _, receipt = newest_receipt(session)
        path, hexpart = artifact_path(receipt)
        try:
            with open(path, "rb") as f:
                artifact_bytes = f.read()
        except OSError:
            withheld = "artifact missing from the store"
        if withheld is None:
            if hashlib.sha256(artifact_bytes).hexdigest() != hexpart:
                withheld = "artifact re-digest mismatch"
            else:
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

    tamper = verbs.add_parser("tamper", help="edit the attested artifact in the store")
    tamper.add_argument("--match-count", default="0")
    tamper.set_defaults(run=cmd_tamper)

    rollback = verbs.add_parser("rollback", help="delete the session's newest receipt")
    rollback.set_defaults(run=cmd_rollback)

    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
