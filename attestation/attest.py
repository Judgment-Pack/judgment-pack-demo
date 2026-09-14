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

# The engine (Act 8): the same gateway in its one-configuration-file form, on
# its own loopback port with its own identity, store and pin. A write goes
# through it as an action, refused unless the judgment it cites is in the
# decision book -- this project's own audit/evaluations.jsonl, which the
# engine reads where the runtime wrote it.
ENGINE_URL = os.environ.get("ENGINE_URL", "http://127.0.0.1:8788")
ENGINE_AUTHORITY = os.environ.get("ENGINE_AUTHORITY", "gateway:enterprise-demo-engine")
ENGINE_STORE = os.environ.get("ENGINE_STORE", "/engine/store")
ENGINE_PIN = os.environ.get("ENGINE_PIN", "/engine-pin/pinned.pubkey")
AUDIT_BOOK = os.environ.get("AUDIT_BOOK", os.path.join("audit", "evaluations.jsonl"))
VENDOR_FILE = os.path.join("attested", "vendor.json")
VENDOR_FACTS_FILE = os.path.join("attested", "vendor-facts.json")
VENDOR_EVIDENCE_FILE = os.path.join("attested", "vendor-evidence.json")
CITES_FILE = os.path.join("attested", "cites.json")
ACTION_FILE = os.path.join("attested", "action.json")
ENGINE_REGISTRY_FETCHED = os.path.join("attested", "engine-registry.fetched.jsonl")
ENGINE_RECOVERY = (
    "is the engine up? Recover with:\n"
    "  docker compose up -d --force-recreate engine"
)

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


# --- Act 8: the engine, and a write after a person approves ------------------

def bearer(args):
    """The token that says who is asking: --token, else $ATTEST_TOKEN.

    Minted outside the sandbox, by the stand-in identity provider the engine
    trusts (`docker compose exec engine issuer mint /private/issuer --subject
    NAME` on the host). The sandbox holds no key and cannot mint one, which is
    the point: the agent can propose, and only a person's token can ask.
    """
    token = getattr(args, "token", None) or os.environ.get("ATTEST_TOKEN", "")
    return token.strip()


def engine_call(method, path, body=None, token=""):
    """One request to the engine; returns (status, decoded body).

    A refusal (4xx) is an answer, not a transport failure: the engine names the
    step of its ladder the request fell at, and the caller reads it.
    """
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(ENGINE_URL + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, {"error": raw.decode(errors="replace")}
    except urllib.error.URLError as error:
        fail(f"attest: cannot reach the engine at {ENGINE_URL} ({error.reason});",
             "  " + ENGINE_RECOVERY, code=4)


def load_json_file(path, what):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        fail(f"attest: no readable {path} — {what}")


def cmd_read(args):
    """Read one vendor's record through the engine, with a receipt (ledger 1).

    The engine derives `tickets/live` from the platform's binding and runs the
    MCP adapter as the platform's own user, which calls the ticket system's
    `get_vendor` tool; what came back is retained under its digest and signed
    with the acquisition record -- which tool, through which adapter, when --
    and the caller the token names. The record carries the onboarding facts as
    the ticket system filed them; they are written beside the citation so the
    next beat can judge them and cite this receipt.
    """
    vendor_id = args.vendor_id.strip()
    if not vendor_id:
        fail("attest: vendor-id must be a non-empty string")
    token = bearer(args)
    session = mint_session("act", vendor_id)
    status, answer = engine_call("POST", "/acquire", {
        "session": session,
        "source": "tickets/live",
        "arguments": {"tool": "get_vendor", "arguments": {"id": vendor_id}},
    }, token)
    if status != 200:
        fail(f"attest: the engine refused the read ({status}): {answer.get('error')}",
             "  every request to the engine carries a person's token: --token or $ATTEST_TOKEN" if status == 401 else "",
             code=5)
    receipt = answer.get("receipt", {})
    record = (answer.get("result") or {}).get("structuredContent") or {}
    if record.get("error"):
        fail(f"attest: the ticket system answered: {record['error']}  (receipted all the same: "
             f"session {session}, callIndex {receipt.get('callIndex')})", code=5)
    citation = {"sessionId": receipt.get("sessionId"), "callIndex": receipt.get("callIndex"),
                "signature": receipt.get("signature")}
    os.makedirs("attested", exist_ok=True)
    with open(VENDOR_FILE, "w") as f:
        json.dump({"vendor": vendor_id, "session": session, "citation": citation,
                   "receipt": receipt, "record": record, "salts": answer.get("salts")},
                  f, indent=2, sort_keys=True)
    with open(VENDOR_FACTS_FILE, "w") as f:
        json.dump(record.get("facts", {}), f, indent=2, sort_keys=True)
    with open(VENDOR_EVIDENCE_FILE, "w") as f:
        json.dump(record.get("evidence", {}), f, indent=2, sort_keys=True)
    with open(CITES_FILE, "w") as f:
        json.dump([citation], f, indent=2)
    acquisition = receipt.get("acquisition", {})
    caller = receipt.get("caller") or {}
    say(sys.stdout,
        f"read      {vendor_id}  {record.get('name', '?')}  status {record.get('status', '?')}",
        f"          session {session}  callIndex {receipt.get('callIndex')}",
        f"          authority {receipt.get('authority')}  keyId {receipt.get('keyId')}",
        f"          resultDigest {receipt.get('resultDigest')}",
        f"          acquisition: tool get_vendor through {acquisition.get('adapter', {}).get('name')}"
        f" ({str(acquisition.get('adapter', {}).get('digest', ''))[:19]}…)  observedAt {acquisition.get('observedAt')}",
        f"          statement: a salted commitment (salt returned, not stored)",
        f"          caller {caller.get('subject', 'null')}  ({caller.get('issuer', '-')})",
        "",
        f"wrote {VENDOR_FILE}, {VENDOR_FACTS_FILE}, {VENDOR_EVIDENCE_FILE}, {CITES_FILE}",
        "Next: judge the facts and cite this receipt --",
        "  jpack experimental evaluate --pack-id vendor-onboarding \\",
        f"    --facts {VENDOR_FACTS_FILE} --evidence {VENDOR_EVIDENCE_FILE} --cites {CITES_FILE}")
    sys.exit(0)


def judgment_citing(citation):
    """The newest line of the decision book that cites exactly this receipt.

    Returns (line bytes, record) or (None, None). The digest the action claims
    is the SHA-256 of the line as the verifier takes it: without its newline.
    """
    try:
        with open(AUDIT_BOOK, "rb") as f:
            lines = f.read().split(b"\n")
    except OSError:
        return None, None
    found = (None, None)
    for line in lines:
        line = line.rstrip(b"\r")
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("cites") == [citation]:
            found = (line, record)
    return found


def cmd_act(args):
    """Ask the engine to perform a write, as the person the token names.

    The request cites the read's receipt and the judgment that relied on it;
    the engine holds both -- the receipt in its own store under its own key,
    the record under the decision book by digest -- before any executor runs,
    and refuses at the first step that fails, naming it. What it mints is an
    action receipt: requester, decision, cites, tool, the target's answer.
    """
    vendor_id = args.vendor_id.strip()
    token = bearer(args)
    vendor = load_json_file(VENDOR_FILE, "run `attest read VENDOR` first")
    if vendor.get("vendor") != vendor_id:
        fail(f"attest: {VENDOR_FILE} is {vendor.get('vendor')}'s read, not {vendor_id}'s — "
             f"run `attest read {vendor_id}` first")
    citation = vendor["citation"]
    line, record = judgment_citing(citation)
    if args.decision:
        record_digest = args.decision.strip()
        pack_digest = (record or {}).get("pack", {}).get("digest", "sha256:" + "0" * 64)
    elif line is None:
        fail(f"attest: no line of {AUDIT_BOOK} cites this read (session {citation['sessionId']}, "
             f"callIndex {citation['callIndex']}) — judge first, citing {CITES_FILE}", code=2)
    else:
        record_digest = "sha256:" + hashlib.sha256(line).hexdigest()
        pack_digest = record.get("pack", {}).get("digest", "")
    arguments = {"id": vendor_id, "status": args.status.strip()}
    if args.reason:
        arguments["reason"] = args.reason
    status, answer = engine_call("POST", "/act", {
        "session": vendor["session"],
        "platform": "tickets",
        "tool": "update_vendor_status",
        "arguments": arguments,
        "decision": {"recordDigest": record_digest, "packDigest": pack_digest},
        "cites": [citation],
    }, token)
    if status != 200:
        # A missing or bad token is refused before the body is read, as any
        # request to the engine is: the ladder's first step, the requester.
        step = answer.get("refusedAt") or ("requester" if status == 401 else "?")
        say(sys.stderr,
            f"REFUSED   at the {step} step ({status})",
            f"          {answer.get('error')}",
            "          nothing was sent to the ticket system and nothing was minted.")
        sys.exit(5)
    receipt = answer.get("receipt", {})
    action = receipt.get("action", {})
    result = (answer.get("result") or {}).get("structuredContent") or {}
    # The session is closed here: the read and the action are its two
    # receipts, and the seal registers their count with the key holder, so
    # the verifier holds the chain whole and a receipt removed later is missed.
    sealed_status, sealed = engine_call("POST", "/seal", {"session": vendor["session"]}, token)
    if sealed_status != 200:
        fail(f"attest: the action was minted but the session could not be sealed ({sealed_status}): {sealed.get('error')}",
             "  the receipt is in the store; seal again once whatever is in flight has finished", code=5)
    os.makedirs("attested", exist_ok=True)
    with open(ACTION_FILE, "w") as f:
        json.dump({"vendor": vendor_id, "receipt": receipt, "result": result,
                   "salts": answer.get("salts"), "sealed": sealed}, f, indent=2, sort_keys=True)
    requester = action.get("requester") or {}
    say(sys.stdout,
        f"acted     {vendor_id} -> {arguments['status']}  (callIndex {receipt.get('callIndex')} of session {receipt.get('sessionId')}, "
        f"sealed at finalCount {sealed.get('finalCount', '?')})",
        f"          requester {requester.get('subject')}  ({requester.get('issuer')})",
        f"          decision  {action.get('decision', {}).get('recordDigest')}",
        f"          cites     {citation['sessionId']}/{citation['callIndex']}",
        f"          tool      {action.get('tool', {}).get('name')} on {action.get('tool', {}).get('endpoint')}"
        f"  via {action.get('adapter', {}).get('name')}  observedAt {action.get('observedAt')}",
        f"          the ticket system answered: {json.dumps(result, sort_keys=True)}",
        f"          resultDigest {receipt.get('resultDigest')}  keyId {receipt.get('keyId')}",
        "",
        f"wrote {ACTION_FILE}",
        "The receipt says who asked, which judgment, which receipts, which tool, and",
        "what came back. It does not say the write was right, and it does not say the",
        "requester approved it: a token proves who asked. Next: attest chain")
    sys.exit(0)


def cmd_chain(_args):
    """Verify the engine's store with the decision book: all three ledgers.

    The reference verifier, under the engine's out-of-band pin, with the seal
    registry fetched from the key holder and the decision-record directory
    named: every receipt is checked, and an action receipt's decision must
    resolve to a line of the book by digest and its citations to receipts in
    the store. Scoped to the read's session when one is on disk.
    """
    registry = None
    try:
        with urllib.request.urlopen(ENGINE_URL + "/registry", timeout=60) as response:
            registry = response.read()
    except urllib.error.URLError as error:
        fail(f"attest: cannot reach the engine at {ENGINE_URL} ({error.reason});", "  " + ENGINE_RECOVERY, code=4)
    os.makedirs("attested", exist_ok=True)
    with open(ENGINE_REGISTRY_FETCHED, "wb") as f:
        f.write(registry)
    try:
        with open(ENGINE_PIN, "rb") as f:
            pin = f.read()
    except OSError as error:
        fail(f"attest: cannot read the engine's pinned public key at {ENGINE_PIN}: {error}")
    proc = subprocess.run(
        [GATEWAY_BIN, "verify", ENGINE_STORE, ENGINE_REGISTRY_FETCHED, ENGINE_AUTHORITY,
         "--decision-records", os.path.dirname(AUDIT_BOOK) or "."],
        input=pin, capture_output=True)
    if proc.returncode != 0 and not proc.stdout.strip():
        fail("attest: NO VERDICT — the verifier could not audit the engine's store at all",
             f"  {proc.stderr.decode(errors='replace').strip()}", "  " + ENGINE_RECOVERY, code=4)
    verdict = json.loads(proc.stdout)
    session = None
    try:
        with open(VENDOR_FILE) as f:
            session = json.load(f).get("session")
    except (OSError, ValueError):
        pass
    findings = verdict.get("findings", [])
    mine = [f for f in findings if session is None or f.get("sessionId") == session]
    say(sys.stdout,
        f"verify    engine store-wide ok={verdict.get('ok')}  (pin {ENGINE_PIN}; registry fetched from the key holder;"
        f" decision records {os.path.dirname(AUDIT_BOOK) or '.'})",
        f"          session {session or '(all)'}: {len(mine)} finding(s)")
    bad = 0
    for finding in mine:
        rest = {k: v for k, v in finding.items() if k not in ("sessionId",)}
        marker = "ok " if finding.get("status") == "ok" else "!! "
        if finding.get("status") != "ok":
            bad += 1
        say(sys.stdout, f"          {marker}{json.dumps(rest, sort_keys=True)}")
    if session is not None and not mine:
        say(sys.stdout, "          no findings for this session: unregistered, or the store is gone")
        sys.exit(6)
    if bad:
        say(sys.stdout, "",
            "Something in the chain no longer holds: a receipt, a citation, or the judgment",
            "the action rests on. The receipt outlives the record it cites; the record",
            "cannot be rewritten under it.")
        sys.exit(6)
    say(sys.stdout, "",
        "Ledger 1 (what entered), ledger 2 (what was decided) and ledger 3 (what was done)",
        "reconcile by digest, and a reader who trusts none of the parties can do this too.")
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

    read = verbs.add_parser("read", help="read a vendor's record through the engine, receipted (Act 8)")
    read.add_argument("vendor_id", metavar="vendor-id", help="the ticket system's id, e.g. V-1042")
    read.add_argument("--token", help="a bearer token from the identity provider; $ATTEST_TOKEN otherwise")
    read.set_defaults(run=cmd_read)

    act = verbs.add_parser("act", help="perform a write through the engine, citing the judgment (Act 8)")
    act.add_argument("vendor_id", metavar="vendor-id")
    act.add_argument("status", help="the status to set, e.g. approved")
    act.add_argument("--reason", help="recorded by the ticket system beside the status")
    act.add_argument("--token", help="a bearer token from the identity provider; $ATTEST_TOKEN otherwise")
    act.add_argument("--decision", metavar="DIGEST",
                     help="claim this record digest instead of the book's line — the refusal beat")
    act.set_defaults(run=cmd_act)

    chain = verbs.add_parser("chain", help="verify the engine's store with the decision book: three ledgers (Act 8)")
    chain.set_defaults(run=cmd_chain)

    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
