"""Use case 1 — Judge a vendor.

Three onboarding requests through one reviewed pack, in the order that makes
the point: the clean approval, the hard stop, and the escalation the honesty
rule exists for.

Every disposition here is produced by `jpack experimental evaluate` in the
user's own scratch copy of the project. The facts documents are transcriptions
of what the request files state — no fact is invented, and the one fact nobody
recorded (Aurora's screening status) is OMITTED rather than guessed, which is
precisely why the third case escalates.
"""

from __future__ import annotations

import os

from .. import blocks
from ..runtime import RuntimeUnavailable
from .base import FlowResult, continue_bar, flush, reply

ID = "judge"
TITLE = "Judge a vendor"
SUMMARY = "A clean approval, a hard stop, and an honest escalation."
# What this flow needs from the machine it runs on, for bot/reconcile.py: a
# scratch copy of the project, which any container can re-copy. Nothing else
# — each case builds its own facts — so a restart mid-flow resumes.
NEEDS = ("project",)

INTRO = (
    "*1 · Judge a vendor*\n\n"
    "These four JSON files are a fictional company's procurement and finance policies — "
    "reviewed, versioned, and tested like code. I can read them and gather inputs; I cannot "
    "decide. The `jpack` runtime evaluates the pack and produces the disposition, the same "
    "bytes on any machine.\n\n"
    "Three requests, in order. Nothing is pre-recorded: each one runs now, in a scratch copy "
    "of the project that belongs to you."
)

CASES = [
    {
        "key": "northwind",
        "title": "The clean approval — Northwind Analytics Ltd",
        "request_file": "requests/northwind-analytics.md",
        "prose": (
            "The request says: new vendor onboarding, annual spend USD 84,000, submission "
            "complete, W-9 received. Two records exist under `evidence/` — a dated OFAC "
            "screening that came back with 0 matches, and the tax form. Every fact below is "
            "transcribed from those files; the spend is the decimal string `\"84000\"`, "
            "because a JSON number compares as unknown by design."
        ),
        "facts": {
            "request": {"type": "new-vendor-onboarding"},
            "submission": {"completeness": "complete"},
            "vendor": {"taxFormStatus": "received", "sanctionsScreening": {"status": "clear"}},
            "engagement": {"annualSpendUsd": "84000"},
        },
        "evidence": {"sanctions-screening": "present", "tax-form": "present"},
        "closing": (
            "The rule `approve-standard` fired because all six of its conditions held. Not one "
            "of them was my opinion."
        ),
        "next_label": "Now the hard stop →",
    },
    {
        "key": "meridian",
        "title": "The hard stop — Meridian Maritime Holdings SA",
        "request_file": "requests/meridian-maritime.md",
        "prose": (
            "Same pack, same shape of request — annual spend USD 120,000, submission complete, "
            "tax form received. But the screening record under `evidence/` reads *2 potential "
            "matches — MATCH, pending review*. Watch what the spend and the completeness stop "
            "mattering."
        ),
        "facts": {
            "request": {"type": "new-vendor-onboarding"},
            "submission": {"completeness": "complete"},
            "vendor": {"taxFormStatus": "received", "sanctionsScreening": {"status": "match"}},
            "engagement": {"annualSpendUsd": "120000"},
        },
        "evidence": {"sanctions-screening": "present", "tax-form": "present"},
        "closing": (
            "Nobody *decided* to reject. The exception `sanctions-match-hard-stop` forces the "
            "outcome whatever else is true, and the trace shows exactly why. A policy that can "
            "be argued with is not a policy."
        ),
        "next_label": "Now the escalation →",
    },
    {
        "key": "aurora",
        "title": "The escalation — Aurora Fabrication GmbH",
        "request_text": (
            "A new request just came in: onboard *Aurora Fabrication GmbH*, "
            "new-vendor-onboarding, annual spend 45,000 USD, submission complete, W-9 received. "
            "*There is no screening record.* Nobody has run one."
        ),
        "prose": (
            "This is the moment the whole product exists for. There is no screening record in "
            "`evidence/`, so the screening status pointer is *omitted* — not guessed, not "
            "defaulted to clear — and the evidence is reported honestly as `absent`: nobody made "
            "a record, which is not the same as being unable to check."
        ),
        "facts": {
            "request": {"type": "new-vendor-onboarding"},
            "submission": {"completeness": "complete"},
            "vendor": {"taxFormStatus": "received"},
            "engagement": {"annualSpendUsd": "45000"},
        },
        "evidence": {"sanctions-screening": "absent", "tax-form": "present"},
        "closing": (
            "Two reasons, both honest and neither ranked above the other: the required screening "
            "evidence is `missing-required-evidence`, *and* the hard-stop condition could not be "
            "evaluated, so it is `unknown`. A model asked to fill that hole would have written "
            "`clear` and been believed. The pack refused to.\n\n"
            "Ask it whether to onboard Aurora anyway and the honest answer is that the payload "
            "asserts nothing about the wisdom of acting — the committee the pack names decides. "
            "*The honesty is the product.*"
        ),
        "next_label": None,
    },
]


def _request_quote(project, case):
    if case.get("request_text"):
        return blocks.section("_The request, as stated:_\n" + case["request_text"])
    path = os.path.join(project, case.get("request_file", ""))
    try:
        with open(path) as handle:
            body = handle.read().strip()
    except OSError:
        return None
    return blocks.code(body)


def handle(turn, deps):
    session = turn.session
    if turn.action == "start" and session.step == 0:
        return FlowResult(
            replies=[
                reply(
                    [
                        blocks.header("1 · Judge a vendor"),
                        blocks.section(INTRO),
                        continue_bar(ID, "case", "Run the clean approval →"),
                    ],
                    text=TITLE,
                )
            ]
        )

    index = min(session.step, len(CASES) - 1)
    case = CASES[index]
    session.step = index + 1
    done = session.step >= len(CASES)

    try:
        project = deps.runtime.ensure_project(session)
    except RuntimeUnavailable as error:
        return FlowResult(
            replies=[reply(blocks.error_blocks("The demo project is unavailable", str(error)))],
            done=True,
            failed=True,
        )

    body = [blocks.header(case["title"]), blocks.section(case["prose"])]
    quote = _request_quote(project, case)
    if quote:
        body.append(quote)

    ran = deps.runtime.evaluate(
        project,
        "vendor-onboarding",
        case["facts"],
        case["evidence"],
        label=case["key"],
    )
    if not ran.ok or not ran.payload:
        body.extend(blocks.error_blocks("The evaluation refused", ran.message()))
        if not done:
            body.append(continue_bar(ID, "case", case["next_label"] or "Continue →"))
        # Ending on a refusal is ending without the demo: not a completion.
        return FlowResult(replies=[reply(body, text=case["title"])], done=done, failed=done)

    payload = ran.payload
    body.extend(
        blocks.disposition_blocks(
            payload,
            title="The runtime's answer",
            facts=case["facts"],
            evidence=case["evidence"],
        )
    )
    # The disposition goes to Slack now — before the model is called at all.
    pending = flush(deps, [reply(body, text=case["title"])])

    narration = deps.model.narrate(
        turn.user_id,
        payload,
        pack=deps.runtime.read_pack(project, "vendor-onboarding"),
        facts=case["facts"],
        evidence=case["evidence"],
    )
    tail = blocks.narration_blocks(narration.text, deps.model.name, note=narration.note)
    tail.append(blocks.section(case["closing"]))
    if not done:
        tail.append(continue_bar(ID, "case", case["next_label"]))
    return FlowResult(replies=pending + [reply(tail, text="Narration")], done=done)
