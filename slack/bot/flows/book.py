"""Use case 4 — The decision book.

Nothing to enable and nothing to stage: this project declares an audit
directory, so every evaluation the user has already run in this session — the
vendor judgments, the newborn pack, the graph walks behind the attested
screening — is already a line in their own `audit/evaluations.jsonl`.

The closing beat is a replay: take the newest record, feed its facts and
evidence back to the runtime, and get the same disposition. The replay is
itself a decision, so it writes its own line, and the book audits itself.
"""

from __future__ import annotations

import json

from .. import blocks
from ..runtime import RuntimeUnavailable
from .base import FlowResult, continue_bar, reply

ID = "book"
TITLE = "The decision book"
SUMMARY = "The audit trail your own choices wrote, replayed."

INTRO = (
    "*4 · The decision book*\n\n"
    "This project declares an audit directory, so every evaluation you have run in this session "
    "was recorded as one JSON line the moment it completed. Nothing was enabled for this use "
    "case; the book was being written the whole time.\n\n"
    "It is not a log. A log tells you something happened. This tells you *what was decided, "
    "under which exact bytes, from which inputs* — everything needed to run it again."
)

EMPTY = (
    "*Your book is empty* — you have not run an evaluation in this session yet, and I am not "
    "going to show you somebody else's.\n\n"
    "Run use case 1 (or 2, or 3) first: every disposition you produce lands here, and then this "
    "use case has something honest to read."
)

MEMBERS = [
    ("run", "the id of the run that produced it — one run can write several records, and a graph walk writes one per node plus the composite"),
    ("at", "when the record was appended, in UTC"),
    ("surface", "which surface decided: `experimental evaluate`, `experimental graph evaluate`, or the MCP tool"),
    ("pack.id / pack.version", "the pack's own identity, read off the document that was evaluated"),
    ("pack.digest", "the SHA-256 of that document's exact bytes — not the path it lived at, the bytes"),
    ("inputs.facts / inputs.evidence", "the documents as they reached the engine, with `evidenceSupplied` keeping an omitted document distinct from an empty one"),
    ("disposition", "the result in its canonical form — the same bytes the payload carried"),
    ("reviewed", "present only when the project keeps a reviewed-set lock: `true` when every document applied was declared and matched the lock, `false` for a draft. This project keeps no lock, so you will see the member ABSENT rather than false — an honest silence instead of a claim nobody checked"),
]


def _subject(record):
    """What this record decided about: a pack, or a whole graph's headline."""
    pack = record.get("pack") or {}
    if pack.get("id"):
        return pack["id"].rsplit("/", 1)[-1]
    graph = record.get("graph") or {}
    if graph.get("id"):
        return graph["id"] + " (composite headline)"
    return "?"


def _summarize(record):
    disposition = record.get("disposition") or {}
    detail = disposition.get("outcomeId") or ", ".join(disposition.get("reasons") or [])
    return "`{}` · {} · {} → *{}* {}".format(
        (record.get("run") or "?")[:8],
        record.get("surface", "?"),
        _subject(record),
        disposition.get("kind"),
        "`" + detail + "`" if detail else "",
    )


def record_shape(record):
    """Which of the three record shapes this is.

    A single-pack evaluation carries a `pack` and no `graph`. A graph NODE
    carries both — the pack it evaluated and the graph that walked it. Only
    the composite headline carries a `graph` with no `pack`. The caption has
    to say whichever is true of the record printed underneath it.
    """
    has_pack = bool((record.get("pack") or {}).get("id"))
    has_graph = bool((record.get("graph") or {}).get("id"))
    if has_pack and has_graph:
        return "graph-node"
    if has_pack:
        return "single-pack"
    if has_graph:
        return "composite"
    return "unknown"


CAPTIONS = {
    "single-pack": (
        "*The newest single-pack record, whole* — one evaluation of one pack, with everything "
        "needed to run it again:"
    ),
    "graph-node": (
        "*The newest record is a graph NODE, whole* — every record you have is from a graph walk, "
        "so this one carries both the `pack` it evaluated and the `graph` that walked it. (A walk "
        "also writes a `graph-composite` headline, which carries a `graph` and no `pack`.)"
    ),
    "composite": (
        "*The newest record is a graph-composite headline, whole* — the walk's own result: a "
        "`graph` member, the composite disposition, and no `pack`, because no single document "
        "produced it."
    ),
    "unknown": "*The newest record, whole:*",
}


def _newest_evaluation(records, shapes=("single-pack",)):
    for record in reversed(records):
        if record.get("kind") == "evaluation" and record_shape(record) in shapes:
            return record
    return None


def _decision_id_for(deps, project, pack_id):
    ran = deps.runtime.packs_list(project)
    for entry in ((ran.payload or {}).get("packs") or []):
        if entry.get("packId") == pack_id:
            return entry.get("id")
    return None


def handle(turn, deps):
    session = turn.session
    if turn.action == "start" and session.step == 0:
        return _show_book(turn, deps)
    return _replay(turn, deps)


def _show_book(turn, deps):
    session = turn.session
    try:
        project = deps.runtime.ensure_project(session)
    except RuntimeUnavailable as error:
        return FlowResult(
            replies=[reply(blocks.error_blocks("The demo project is unavailable", str(error)))],
            done=True,
        )
    records = deps.runtime.audit_records(project)
    if not records:
        return FlowResult(
            replies=[
                reply(
                    [blocks.header("4 · The decision book"), blocks.section(EMPTY)],
                    text="The book is empty",
                )
            ],
            done=True,
        )

    body = [blocks.header("4 · The decision book"), blocks.section(INTRO)]
    body.append(
        blocks.section(
            "*{} record(s)* in `audit/evaluations.jsonl`, oldest first — every one of them "
            "written by a choice you made:".format(len(records))
        )
    )
    body.append(blocks.section("\n".join("• " + _summarize(r) for r in records[-12:])))

    # A single-pack record teaches the members best; a graph node is the next
    # most complete. Whatever is chosen, the caption below names its shape.
    newest = (
        _newest_evaluation(records)
        or _newest_evaluation(records, shapes=("graph-node",))
        or records[-1]
    )
    body.append(blocks.section(CAPTIONS[record_shape(newest)]))
    body.append(blocks.json_code(newest, limit=2200))
    body.append(
        blocks.section(
            "*Member by member:*\n"
            + "\n".join("• `{}` — {}".format(name, gloss) for name, gloss in MEMBERS)
        )
    )
    body.append(
        blocks.section(
            "*Three ledgers, and this is the middle one.* The desk's receipts say what *entered*; "
            "this trail says what was *decided*; a project's own records say what was *done*. "
            "They are separable on purpose — someone who trusts none of the three parties can "
            "still reconcile them."
        )
    )
    body.append(continue_bar(ID, "replay", "Replay the newest decision →"))
    session.step = 1
    return FlowResult(replies=[reply(body, text="The decision book")])


def _replay(turn, deps):
    session = turn.session
    project = deps.runtime.ensure_project(session)
    # The same record the book displayed, chosen the same way: a single-pack
    # evaluation if there is one, else a graph node. A composite headline has
    # no inputs of its own and cannot be replayed.
    replayable = [
        record
        for record in deps.runtime.audit_records(project)
        if (record.get("inputs") or {}).get("facts") is not None
    ]
    records = [
        record
        for record in [
            _newest_evaluation(replayable),
            _newest_evaluation(replayable, shapes=("graph-node",)),
        ]
        if record is not None
    ]
    if not records:
        shapes = sorted(
            {record_shape(record) for record in deps.runtime.audit_records(project)}
        )
        return FlowResult(
            replies=[
                reply(
                    blocks.error_blocks(
                        "Nothing in your book can be replayed",
                        "A replay needs a record carrying the inputs as evaluated. Your book "
                        "holds only: {}. A `graph-composite` headline is the walk's own result "
                        "and has no inputs of its own — run use case 1, which writes single-pack "
                        "records, and come back.".format(", ".join(shapes) or "nothing"),
                    )
                )
            ],
            done=True,
        )
    record = records[0]
    pack = record.get("pack") or {}
    inputs = record.get("inputs") or {}
    facts = inputs.get("facts") or {}
    evidence = inputs.get("evidence") if inputs.get("evidenceSupplied") else None

    decision_id = _decision_id_for(deps, project, pack.get("id"))
    if not decision_id:
        return FlowResult(
            replies=[
                reply(
                    blocks.error_blocks(
                        "That record names a pack this project no longer declares",
                        "pack.id {} is not in jpack.json — which is itself the honest answer: the "
                        "record says which document decided, and it is gone.".format(pack.get("id")),
                    )
                )
            ],
            done=True,
        )

    origin = (
        " It was written by a graph walk (node of `{}`), so replaying it alone is one node's "
        "decision, exactly as recorded.".format((record.get("graph") or {}).get("id"))
        if record_shape(record) == "graph-node"
        else ""
    )
    body = [
        blocks.section(
            "*Replaying* record `{}` — pack `{}`, digest `{}`.{}\n\nThe facts and evidence below "
            "are copied out of the record, not retyped from the request:".format(
                (record.get("run") or "?")[:8],
                decision_id,
                (pack.get("digest") or "?")[:23] + "…",
                origin,
            )
        ),
        blocks.json_code({"facts": facts, "evidence": evidence}, limit=1600),
    ]

    ran = deps.runtime.evaluate(project, decision_id, facts, evidence, label="replay")
    if not ran.ok or not ran.payload:
        body.extend(blocks.error_blocks("The replay refused", ran.message()))
        return FlowResult(replies=[reply(body, text="Replay refused")], done=True)

    original = json.dumps(record.get("disposition"), sort_keys=True, separators=(",", ":"))
    replayed = json.dumps(ran.payload.get("disposition"), sort_keys=True, separators=(",", ":"))
    identical = original == replayed

    body.extend(blocks.disposition_blocks(ran.payload, title="The replay's disposition"))
    if identical:
        body.append(
            blocks.section(
                ":white_check_mark: *Byte for byte identical* to the disposition recorded "
                "earlier:\n```\n{}\n```\nSame inputs, same judgment out — and the trail proves "
                "it, on this machine, minutes apart. That is the whole product in one "
                "comparison.".format(blocks.truncate(replayed, 800))
            )
        )
    else:
        body.append(
            blocks.section(
                ":octagonal_sign: *The replay does not match the record.* That is a finding, not "
                "a formatting quirk — report it exactly as it stands:\n```\nrecorded: {}\nreplay:  "
                "{}\n```".format(blocks.truncate(original, 600), blocks.truncate(replayed, 600))
            )
        )
    after = deps.runtime.audit_records(project)
    body.append(
        blocks.section(
            "*And the replay wrote its own line* — the book now holds {} records, the last two "
            "carrying the same disposition. The book just audited itself.".format(len(after))
        )
    )
    return FlowResult(replies=[reply(body, text="Replayed")], done=True)
