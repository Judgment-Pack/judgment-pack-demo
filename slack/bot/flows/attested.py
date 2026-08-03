"""Use case 3 — The attested screening.

The forgery contrast in miniature: the same fact, acquired through a signing
desk, and then forged. One route believed a forgery in use case 1 (the honesty
rule governs the model; it cannot govern a file). This route refuses it — and
the difference is not a better model, it is a receipt.

The honest bound, said out loud every run: the desk proves *byte-lineage, not
truth* — these bytes, from that desk, unaltered since — and its world is a
baked synthetic snapshot. What it removes is the model from the proof path.
"""

from __future__ import annotations

import json
import os

from .. import blocks
from ..desk import DeskError
from ..runtime import RuntimeUnavailable
from .base import FlowResult, continue_bar, flush, reply

ID = "attested"
TITLE = "The attested screening"
SUMMARY = "A signed receipt, a forgery, and a refusal."
# The project AND a live desk. A desk is not rebuildable: its signing key was
# minted in a container that is gone, and receipts signed by a key that no
# longer exists cannot be re-created — that is what a receipt IS. A restart
# mid-flow therefore restarts this use case, loudly (bot/reconcile.py).
NEEDS = ("project", "desk")

SUBJECT = "Meridian Maritime Holdings SA"
TEMPLATE = "graphs/inputs-meridian-match.json"
GRAPH = "graphs/vendor-onboarding.graph.json"
INPUTS = "attested/screening-inputs.json"

INTRO = (
    "*3 · The attested screening*\n\n"
    "In use case 1 the screening status was a fact someone typed after reading a file. Nothing "
    "in that route can tell an honest record from a forged one — *the honesty rule governs the "
    "model; it cannot govern the file.*\n\n"
    "So: a screening desk. A reference attestation gateway runs beside this app, on loopback, "
    "with its own signing seed and its own sealed registry that nothing else can reach. It "
    "performs the screening inside its trust domain and signs a chained receipt for the bytes "
    "it saw. A corpus-tested derivation rule — data, not a model — turns those bytes into the "
    "fact the pack reads.\n\n"
    "Then we forge the stored artifact and watch what happens."
)

BOUNDS = (
    "_What the desk does *not* claim: it proves byte-lineage, not truth — these bytes came from "
    "that desk, unaltered since. Its world is a baked synthetic snapshot; nothing here evidences "
    "that it matches the real OFAC lists. And it attests only that *this exact string* was "
    "screened: whether that names the vendor under evaluation is the template author's claim, "
    "never the desk's._"
)


def _scrub(text, project, config=None):
    """Make tool output fit to post: no container paths, no compose advice.

    Two things must not reach a Slack message. Absolute paths are noise that
    hides the beat. Worse, `attest`'s recovery text tells the reader to run
    `docker compose` — true where that glue usually runs, and useless here,
    where nobody has a compose stack or a shell. A recovery instruction the
    reader cannot follow is not honesty, it is confusion; the operator-facing
    truth goes to the logs instead.
    """
    if not text:
        return text
    if project:
        text = text.replace(project, "<your session>")
    root = getattr(config, "session_root", None) if config else None
    if root:
        text = text.replace(root, "<scratch>")
    cleaned = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("docker compose") or stripped.startswith("(bare `restart`"):
            continue
        if "was recreated, and plain `up -d` is a no-op" in stripped:
            continue
        if stripped.startswith("is the gateway up? Recover with:"):
            cleaned.append("        (the desk for this session is not answering)")
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _read_inputs(project):
    try:
        with open(os.path.join(project, INPUTS)) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def handle(turn, deps):
    session = turn.session
    if turn.action == "start" and session.step == 0:
        return FlowResult(
            replies=[
                reply(
                    [
                        blocks.header("3 · The attested screening"),
                        blocks.section(INTRO),
                        continue_bar(ID, "screen", "Start the desk and screen →"),
                    ],
                    text=TITLE,
                )
            ]
        )
    if session.step == 0:
        return _do_screen(turn, deps)
    return _do_tamper(turn, deps)


def _do_screen(turn, deps):
    session = turn.session
    try:
        project = deps.runtime.ensure_project(session)
        desk = deps.desk.start(session, project)
    except (RuntimeUnavailable, DeskError) as error:
        return FlowResult(
            replies=[
                reply(
                    blocks.error_blocks("The screening desk could not start", str(error)),
                    text="Desk unavailable",
                )
            ],
            done=True,
        )

    body = [
        blocks.section(
            "*The desk is up* on `{}`, authority `{}`, keyId `{}`.\n\nIts signing seed and seal "
            "registry live in a directory this flow's tools never read; the public key was "
            "pinned from `gateway keygen`'s own output at provisioning time, never fetched from "
            "the running desk — asking the gateway you are about to audit for its own key proves "
            "consistency, not authenticity.".format(
                desk.url, deps.config.gateway_authority, desk.key_id
            )
        )
    ]

    screened = deps.desk.attest(session, project, "screen", SUBJECT, "--template", TEMPLATE)
    body.append(blocks.section("*Acquiring* — `attest screen \"{}\"`".format(SUBJECT)))
    body.append(blocks.code(_scrub(screened.narration, project, deps.config)))
    if screened.returncode != 0:
        body.append(
            blocks.section(
                ":octagonal_sign: *The acquisition failed* (exit `{}`), and that is a stated "
                "answer, never an assumed one — there is no silent fallback to the file route. "
                "Sorry: this one is on the deployment, not on you. Try use case 3 again in a "
                "moment; the details are in the service log the operator reads.".format(
                    screened.returncode
                )
            )
        )
        return FlowResult(replies=[reply(body, text="Acquisition failed")], done=True)

    checked = deps.desk.attest(session, project, "check")
    body.append(
        blocks.section(
            "*Verifying and deriving* — `attest check`. The registry is fetched from the key "
            "holder, the reference `gateway verify` binary reaches the verdict, the verdict is "
            "read from its JSON findings and scoped to this session, the artifact is re-digested, "
            "and only then does the derivation rule run."
        )
    )
    body.append(blocks.code(_scrub(checked.narration, project, deps.config)))
    # `attest check` exits 0 derived, 3 withheld, 4 no verdict, 1 could not
    # start. What is said next is decided by that number, never assumed.
    withheld_now = checked.returncode == 3
    if checked.returncode not in (0, 3):
        body.append(
            blocks.section(
                ":octagonal_sign: *The verifier could not reach a verdict at all* (exit `{}`) — "
                "which is not a tamper verdict and must not be read as one. Reported as it "
                "stands; the operator's log has the rest.".format(checked.returncode)
            )
        )
        return FlowResult(replies=[reply(body, text="No verdict")], done=True)

    derived = _read_inputs(project)
    if derived:
        body.append(
            blocks.section(
                "*The derived inputs* — no hand transcription anywhere in that path:"
            )
        )
        body.append(blocks.json_code(derived.get("screening"), limit=1200))
    if withheld_now:
        body.append(
            blocks.section(
                ":large_orange_diamond: *This acquisition was already withheld* — before any "
                "forgery. The evidence is `screening-record: unknown`, and the evaluation below "
                "will escalate for that reason rather than the one this act is about. Withholding "
                "is the answer, not an obstacle; nothing substitutes a value."
            )
        )

    pending = flush(deps, [reply(body, text="Screening acquired")])

    ran = deps.runtime.graph_evaluate(project, GRAPH, INPUTS)
    if not ran.ok or not ran.payload:
        pending.append(reply(blocks.error_blocks("The graph evaluation refused", ran.message())))
        return FlowResult(replies=pending, done=True)

    tail = blocks.disposition_blocks(
        ran.payload, title="The composite disposition, from an attested screening"
    )
    tail.append(blocks.section(_nodes_summary(ran.payload)))
    narration = deps.model.narrate(
        turn.user_id,
        ran.payload,
        note=(
            "This payload is a graph composite: narrate the per-node dispositions, the feeds "
            "(injected or not injected), and every handoff. No pack document is supplied here, "
            "so do not quote rule conditions."
        ),
    )
    tail.extend(blocks.narration_blocks(narration.text, deps.model.name, note=narration.note))
    tail.append(blocks.section(BOUNDS))
    disposition = ran.payload.get("disposition") or {}
    if disposition.get("outcomeId") == "reject":
        tail.append(
            blocks.section(
                "*Same decision as use case 1's hard stop — different provenance.* Only one of "
                "the two routes can prove nobody edited the number afterwards. Let us test that "
                "claim by forging it."
            )
        )
    else:
        tail.append(
            blocks.section(
                "*Read what actually happened above*, not what this act usually shows: the "
                "composite came back `{}`{}. That is the run you got, and it is the one that "
                "counts — the forgery beat below still works from here.".format(
                    disposition.get("kind"),
                    " (" + ", ".join(disposition.get("reasons") or []) + ")"
                    if disposition.get("reasons")
                    else "",
                )
            )
        )
    tail.append(continue_bar(ID, "tamper", "Forge the stored artifact →"))
    session.step = 1
    return FlowResult(replies=pending + [reply(tail, text="Attested screening")])


def _do_tamper(turn, deps):
    session = turn.session
    project = session.scratch_dir
    body = [
        blocks.section(
            "*Forging the store.* `attest tamper --match-count 0` edits the attested "
            "`matchCount` inside the content-addressed artifact the session's receipt cites — a "
            "clean `0`, exactly the value a fabricating agent would want."
        )
    ]
    try:
        tampered = deps.desk.attest(session, project, "tamper", "--match-count", "0")
    except DeskError as error:
        return FlowResult(
            replies=[reply(blocks.error_blocks("The desk is gone", str(error)))], done=True
        )
    body.append(blocks.code(_scrub(tampered.narration, project, deps.config)))
    if tampered.returncode != 0:
        body.append(
            blocks.section(
                ":octagonal_sign: *The forgery did not take* (exit `{}`) — so nothing below is a "
                "verdict about a tampered store, and I am not going to narrate it as one. The "
                "run is reported as it stands. Try use case 3 again from the start.".format(
                    tampered.returncode
                )
            )
        )
        deps.desk.stop(session)
        return FlowResult(replies=[reply(body, text="Forgery failed")], done=True)

    checked = deps.desk.attest(session, project, "check")
    body.append(blocks.section("*Re-verifying* — `attest check` against the same sealed registry:"))
    body.append(blocks.code(_scrub(checked.narration, project, deps.config)))
    if checked.returncode == 3:
        body.append(
            blocks.section(
                ":no_entry: *Withheld* (exit 3). The forged bytes no longer match what was "
                "signed, so the derivation refused to assert anything and recorded "
                "`screening-record: unknown`. A withheld result is the answer, not an obstacle — "
                "nothing substitutes a value and nothing retries until it works."
            )
        )
    elif checked.returncode == 0:
        body.append(
            blocks.section(
                ":octagonal_sign: *The verification passed after a forgery* (exit 0) — which is "
                "not what this act expects, and saying otherwise would be the exact dishonesty "
                "the demo argues against. Read the verifier's output above as it stands and "
                "report it."
            )
        )
    else:
        body.append(
            blocks.section(
                ":octagonal_sign: *No verdict was reached* (exit `{}`). That is not a tamper "
                "verdict and must not be read as one.".format(checked.returncode)
            )
        )
    pending = flush(deps, [reply(body, text="Tampered")])

    ran = deps.runtime.graph_evaluate(project, GRAPH, INPUTS)
    if not ran.ok or not ran.payload:
        pending.append(reply(blocks.error_blocks("The graph evaluation refused", ran.message())))
        deps.desk.stop(session)
        return FlowResult(replies=pending, done=True)

    tail = blocks.disposition_blocks(
        ran.payload, title="The evaluation the forgery bought"
    )
    tail.append(blocks.section(_nodes_summary(ran.payload)))
    narration = deps.model.narrate(
        turn.user_id,
        ran.payload,
        note=(
            "This payload is a graph composite produced after the store was forged. Narrate the "
            "per-node dispositions, whether each feed was injected, and every handoff, strictly "
            "from the payload. No pack document is supplied here, so do not quote rule "
            "conditions."
        ),
    )
    tail.extend(blocks.narration_blocks(narration.text, deps.model.name, note=narration.note))
    # The closing line is derived from the payload, not asserted over it.
    injected = [
        feed.get("injected")
        for node in ran.payload.get("nodes") or []
        for feed in node.get("factFeeds") or []
    ]
    handoffs = ran.payload.get("handoffs") or []
    if injected and not any(injected):
        tail.append(
            blocks.section(
                "*Same forgery, opposite result.* In use case 1 a forged record would have been "
                "narrated faithfully and believed; here the forged `clear` could not carry a "
                "valid receipt, so *no fact was injected* and {} handoff(s) were requested.\n\n"
                "_Use case 1's honesty was a rule the model follows. This one is arithmetic._".format(
                    len(handoffs)
                )
            )
        )
    else:
        tail.append(
            blocks.section(
                "*Read the feeds above literally:* at least one fact was still injected on this "
                "run, so the sentence this act usually ends on would be false here. What the "
                "payload says is what happened; nothing about it is being smoothed."
            )
        )
    tail.append(blocks.section(BOUNDS))
    deps.desk.stop(session)
    return FlowResult(replies=pending + [reply(tail, text="Withheld")], done=True)


def _nodes_summary(payload):
    lines = []
    for node in payload.get("nodes") or []:
        disposition = node.get("disposition") or {}
        kind = disposition.get("kind")
        detail = disposition.get("outcomeId") or ", ".join(disposition.get("reasons") or [])
        lines.append("• node `{}` → *{}* `{}`".format(node.get("node"), kind, detail))
        for feed in node.get("factFeeds") or []:
            lines.append(
                "    ◦ fact feed from `{}` at `{}` — injected: *{}*".format(
                    feed.get("from"), feed.get("pointer"), feed.get("injected")
                )
            )
        for feed in node.get("evidenceFeeds") or []:
            lines.append(
                "    ◦ evidence feed from `{}` for `{}` — state: *{}*".format(
                    feed.get("from"), feed.get("requirement"), feed.get("state")
                )
            )
    for handoff in payload.get("handoffs") or []:
        target = handoff.get("target") or {}
        lines.append(
            "• handoff from `{}` → *{}* ({}), triggered by {}".format(
                handoff.get("node"),
                target.get("name"),
                target.get("kind"),
                ", ".join("`" + t + "`" for t in handoff.get("triggeredBy") or []),
            )
        )
    return "*The walk*\n" + "\n".join(lines) if lines else "*The walk* — (no nodes reported)"
