"""Use case 2 — Author a policy live.

A policy someone wrote in English becomes a reviewed, validated, registered
artifact while they watch, and then judges a case.

The division of labor is the entire point and it is visible in this file:
the model DRAFTS (labor), `jpack spec validate` decides whether the draft is a
pack (gatekeeper), `jpack packs validate` decides whether the project accepts
it, and `jpack experimental evaluate` decides what it concludes. If the
validator refuses, the refusal is shown verbatim and the model is asked to
repair — at most three rounds, then we stop and say so.
"""

from __future__ import annotations

import json
import os
import re

from .. import blocks, content
from ..model import CANNED_DRAFT, extract_json
from ..runtime import RuntimeUnavailable, diagnostics_summary
from .base import FlowResult, continue_bar, flush, reply, step_button

ID = "author"
TITLE = "Author a policy live"
SUMMARY = "English policy in, validated pack out, then it judges."
# The project, plus the pack this flow registered into it. Both are
# rebuildable after a restart: the project is re-copied and the drafted pack
# document persisted with the session, so it is re-registered from the same
# bytes rather than re-imagined (bot/reconcile.py).
NEEDS = ("project", "registered-pack")

MAX_ROUNDS = 3

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")

INTRO = (
    "*2 · Author a policy live*\n\n"
    "Paste a policy — a paragraph of plain English is enough — and I will have the model draft "
    "it as a judgment pack. Then the validator, not the model, decides whether what came back "
    "is a pack at all. You will see the refusals if there are any: that loop *is* the demo.\n\n"
    "No policy handy? Take the canned one:\n>>> " + content.CANNED_POLICY
)


def _slug(value, fallback="drafted-policy"):
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value if ID_PATTERN.match(value) else fallback


def _envelope_problem(envelope):
    """Why this draft reply cannot drive the flow, or None when it can.

    The envelope has to carry a pack AND the case that pack will judge: a
    reply with no facts would leave the flow removing "a fact" from nothing
    and narrating a case it never had.
    """
    if not isinstance(envelope, dict):
        return "no JSON object came back"
    if not isinstance(envelope.get("pack"), dict) or not envelope["pack"]:
        return "no pack document in the envelope"
    if not isinstance(envelope.get("facts"), dict) or not envelope["facts"]:
        return "no facts document for the scenario"
    return None


def _free_decision_id(project, decision_id):
    """Never register over a pack the project already declares.

    A drafted pack that happened to pick `vendor-onboarding` would otherwise
    silently replace the reviewed one in this session's copy — the exact class
    of quiet substitution the demo argues against.
    """
    try:
        with open(os.path.join(project, "jpack.json")) as handle:
            declared = json.load(handle).get("packs") or {}
    except (OSError, ValueError):
        return decision_id
    if decision_id not in declared:
        return decision_id
    for suffix in range(2, 50):
        candidate = "{}-{}".format(decision_id, suffix)
        if candidate not in declared:
            return candidate
    return decision_id + "-draft"


def _drop_last_leaf(facts):
    """Remove exactly one fact — the last leaf in sorted depth-first order.

    Used for the unknown probe: nothing else about the case changes, so
    whatever the pack does next is the pack's own wiring speaking.
    """
    trail = []

    def walk(node, path):
        for key in sorted(node):
            value = node[key]
            if isinstance(value, dict):
                walk(value, path + [key])
            else:
                trail.append(path + [key])

    if not isinstance(facts, dict):
        return facts, None
    walk(facts, [])
    if not trail:
        return facts, None
    target = trail[-1]
    reduced = json.loads(json.dumps(facts))
    node = reduced
    for key in target[:-1]:
        node = node[key]
    node.pop(target[-1], None)
    # Prune the parent object if the removal emptied it.
    if not node and len(target) > 1:
        parent = reduced
        for key in target[:-2]:
            parent = parent[key]
        parent.pop(target[-2], None)
    return reduced, "/" + "/".join(target)


def _draft_blocks(draft_text):
    return blocks.code(draft_text, limit=2400)


def _validate_loop(deps, turn, pack, policy_text, body):
    """Draft → validate → repair, at most MAX_ROUNDS validations."""
    draft = pack
    for round_number in range(1, MAX_ROUNDS + 1):
        rendered = json.dumps(draft, indent=2, sort_keys=True)
        ran = deps.runtime.spec_validate_text(rendered)
        payload = ran.payload or {}
        status = payload.get("status", "unknown")
        if ran.ok and status == "valid":
            body.append(
                blocks.section(
                    ":white_check_mark: *Round {}: `jpack spec validate` says valid.* The "
                    "validator is offline, deterministic, and has no idea a model wrote this.".format(
                        round_number
                    )
                )
            )
            return draft, True
        body.append(
            blocks.section(
                ":octagonal_sign: *Round {}: the validator refused the draft* (`status: {}`). "
                "Every diagnostic names its location and its fix:".format(round_number, status)
            )
        )
        body.append(blocks.code(diagnostics_summary(payload) if payload else ran.message()))
        if round_number == MAX_ROUNDS:
            body.append(
                blocks.section(
                    "*Three rounds, still refused — so we stop.* That is the honest end of this "
                    "run: an invalid document does not become a pack because we ran out of "
                    "patience. The diagnostics above are the work list."
                )
            )
            return draft, False
        repaired = deps.model.draft(
            turn.user_id,
            policy_text,
            feedback=diagnostics_summary(payload) if payload else ran.message(),
            previous=rendered,
        )
        if not repaired.ok:
            body.append(blocks.context(repaired.note or content.MODEL_UNAVAILABLE))
            return draft, False
        repair = extract_json(repaired.text) or {}
        # A repair may come back as the bare pack rather than the envelope.
        # Merge, so the scenario and facts from the first reply survive: the
        # case this pack is about is not something to re-invent silently.
        merged = dict(turn.session.data.get("author_envelope") or {})
        if isinstance(repair.get("pack"), dict):
            merged.update({k: v for k, v in repair.items() if v is not None})
        else:
            merged["pack"] = repair
        draft = merged.get("pack") or repair
        turn.session.data["author_envelope"] = merged
    return draft, False


def register_pack(runtime, project, decision_id, pack):
    """Write one pack into a project and declare it. Returns True on success.

    Separate from the flow's own step because `bot/reconcile.py` calls it too:
    after a restart the drafted pack document is still in the persisted
    session, so it is re-registered from the SAME BYTES into the rebuilt
    scratch copy. That is a rebuild, not a re-imagining — and `packs validate`
    still rules on the result either way.
    """
    path = os.path.join("packs", decision_id + ".pack.json")
    absolute = os.path.join(project, path)
    os.makedirs(os.path.dirname(absolute), exist_ok=True)

    config_path = os.path.join(project, "jpack.json")
    # Snapshot before touching anything: if the amendment cannot be declared,
    # a half-registered project is worse than no registration at all — it is
    # drifted from its own lock, so EVERY later evaluation in this session,
    # including the untouched packs use cases 1, 3 and 4 run, refuses.
    with open(config_path, "rb") as handle:
        config_before = handle.read()
    config = json.loads(config_before.decode("utf-8"))

    with open(absolute, "w") as handle:
        json.dump(pack, handle, indent=2)
        handle.write("\n")
    config.setdefault("packs", {})[decision_id] = {
        "path": path,
        "description": (pack.get("decision") or {}).get("question", pack.get("title", "")),
        "expectedVersion": pack.get("version", "0.1.0"),
    }
    # Written whole, then moved into place: a reader must never catch this
    # file mid-truncation, and a project's declarations are exactly the thing
    # the demo says nobody should find in a half-written state.
    staging = config_path + ".staging"
    with open(staging, "w") as handle:
        json.dump(config, handle, indent=1)
        handle.write("\n")
    os.replace(staging, config_path)

    # The project pins its reviewed set, and registering a pack moves it. The
    # runtime refuses to decide under law that left that set — correctly — so
    # the amendment is DECLARED here rather than worked around. `packs lock`
    # reviews nothing and approves nothing; it records, in a file a reviewer
    # can diff, that the law changed on purpose.
    if runtime.has_lock(project):
        locked = runtime.packs_lock(project)
        if not locked.ok:
            # Put the project back exactly as it was, so the rest of the
            # session still decides. The refusal is reported; the wreckage is
            # not left behind to make every other use case refuse too.
            with open(config_path, "wb") as handle:
                handle.write(config_before)
            try:
                os.remove(absolute)
            except OSError:
                pass
            return False
    return True


def _register(deps, session, project, decision_id, pack, body):
    """Write the pack into the user's project and let `packs validate` rule."""
    path = os.path.join("packs", decision_id + ".pack.json")
    locked = deps.runtime.has_lock(project)
    if not register_pack(deps.runtime, project, decision_id, pack):
        body.extend(
            blocks.error_blocks(
                "The project refused the amendment",
                "`jpack packs lock` did not accept the registered pack, so this project's "
                "reviewed set still names the documents it named before. Nothing was decided "
                "under law nobody declared.",
            )
        )
        return False

    body.append(
        blocks.section(
            "*Registered.* `{}` is now a file in your project, and `jpack.json` declares it as "
            "decision id `{}`.".format(path, decision_id)
        )
    )
    if locked:
        body.append(
            blocks.section(
                "*And the reviewed set moved, so I said so.* This project pins its documents in "
                "`jpack.lock.json`, and the runtime refuses to decide under law that left that "
                "pin — so registering a pack is an amendment that has to be declared: "
                "`jpack packs lock`, which reviews nothing and approves nothing, and writes the "
                "new digests where a reviewer can diff them. Without that line, the evaluation "
                "you are about to watch would have refused with `JPS-LOCK-VERIFY`, and it would "
                "have been right to."
            )
        )
    ran = deps.runtime.packs_validate(project)
    if ran.ok:
        summary = (ran.payload or {}).get("summary", {})
        body.append(
            blocks.section(
                ":white_check_mark: `jpack packs validate` — {} of {} packs valid, including the "
                "one that did not exist a minute ago.".format(
                    summary.get("passed", "?"), summary.get("total", "?")
                )
            )
        )
        return True
    body.extend(blocks.error_blocks("`jpack packs validate` refused the project", ran.message()))
    return False


def consumes_text(session):
    """Free text is the policy — but only at the paste step. The later steps
    ignore it, and the router's question guard must cover them."""
    return session.step == 0


def handle(turn, deps):
    session = turn.session

    if turn.action == "start" and session.step == 0:
        return FlowResult(
            replies=[
                reply(
                    [
                        blocks.header("2 · Author a policy live"),
                        blocks.section(INTRO),
                        blocks.actions(
                            [
                                step_button(ID, "canned", "Use the canned policy →"),
                                blocks.button("Back to the menu", blocks.ACTION_MENU, style=None),
                            ]
                        ),
                        blocks.context(
                            "Or just type your policy here as a message — anything you send next "
                            "is treated as the policy text, and as data, never as instructions."
                        ),
                    ],
                    text=TITLE,
                )
            ]
        )

    if session.step == 0:
        return _do_draft(turn, deps)
    if session.step == 1:
        return _do_judge(turn, deps)
    return _do_unknown(turn, deps)


def _do_draft(turn, deps):
    session = turn.session
    typed = (turn.text or "").strip()
    # The policy text persists with the session, so a restart mid-flow starts
    # this use case again from the user's OWN policy rather than quietly
    # substituting the canned one.
    remembered = session.data.get("author_policy")
    if turn.action == "canned" or (not typed and not remembered):
        policy_text, canned = content.CANNED_POLICY, True
    elif typed:
        policy_text, canned = typed, False
    else:
        policy_text, canned = remembered, bool(session.data.get("author_canned"))
    session.data["author_policy"] = policy_text
    session.data["author_canned"] = canned

    try:
        project = deps.runtime.ensure_project(session)
    except RuntimeUnavailable as error:
        return FlowResult(
            replies=[reply(blocks.error_blocks("The demo project is unavailable", str(error)))],
            done=True,
            failed=True,
        )

    body = [
        blocks.section(
            "*The policy I am encoding* (data, not instructions):\n>>> " + policy_text[:1500]
        )
    ]

    drafted = deps.model.draft(turn.user_id, policy_text)
    envelope = extract_json(drafted.text) if drafted.ok else None
    problem = _envelope_problem(envelope)
    if problem:
        body.append(
            blocks.context(
                (drafted.note or "_(The model's draft was unusable: {}.)_".format(problem))
                + "\nFalling back to a pack drafted earlier and checked in as a fixture — the "
                "validate-and-register loop below is identical, and it is the part that decides."
            )
        )
        envelope = CANNED_DRAFT
    session.data["author_envelope"] = envelope

    pack = envelope["pack"]
    decision_id = _free_decision_id(
        project, _slug(envelope.get("decisionId") or pack.get("title"))
    )
    session.data["author_decision_id"] = decision_id
    body.append(
        blocks.section(
            "*The draft* — {} outcome(s), {} rule(s), escalating to *{}*. It is a candidate, "
            "nothing more: only validation decides conformance.".format(
                len(pack.get("outcomes") or []),
                len(pack.get("rules") or []),
                ((pack.get("escalation") or {}).get("target") or {}).get("name", "—"),
            )
        )
    )
    body.append(_draft_blocks(json.dumps(pack, indent=2, sort_keys=True)))

    pack, valid = _validate_loop(deps, turn, pack, policy_text, body)
    session.data["author_pack"] = pack
    if not valid:
        return FlowResult(
            replies=[reply(body, text="The validator refused the draft")], done=True, failed=True
        )

    if not _register(deps, session, project, decision_id, pack, body):
        return FlowResult(replies=[reply(body, text="Registration refused")], done=True, failed=True)

    session.step = 1
    scenario = session.data["author_envelope"].get("scenario") or "a case for this pack"
    body.append(
        blocks.section(
            "*Now let it decide something.* The case: _{}_\n\nThe facts below are a "
            "transcription of that sentence into the pack's own pointers — labor, not "
            "judgment.".format(scenario)
        )
    )
    body.append(continue_bar(ID, "judge", "Let the newborn pack judge →"))
    return FlowResult(replies=[reply(body, text="Pack registered")])


def _do_judge(turn, deps):
    session = turn.session
    envelope = session.data.get("author_envelope") or CANNED_DRAFT
    decision_id = session.data.get("author_decision_id", "gifts-hospitality")
    facts = envelope.get("facts") or {}
    evidence = envelope.get("evidence")
    project = deps.runtime.ensure_project(session)

    ran = deps.runtime.evaluate(project, decision_id, facts, evidence, label="newborn")
    if not ran.ok or not ran.payload:
        return FlowResult(
            replies=[
                reply(
                    blocks.error_blocks("The newborn pack refused to evaluate", ran.message()),
                    text="Evaluation refused",
                )
            ],
            done=True,
            failed=True,
        )

    session.step = 2
    body = blocks.disposition_blocks(
        ran.payload,
        title="The pack you watched being born just made its first decision",
        facts=facts,
        evidence=evidence,
    )
    pending = flush(deps, [reply(body, text="First decision")])

    narration = deps.model.narrate(
        turn.user_id,
        ran.payload,
        pack=session.data.get("author_pack"),
        facts=facts,
        evidence=evidence,
    )
    tail = blocks.narration_blocks(narration.text, deps.model.name, note=narration.note)
    reduced, removed = _drop_last_leaf(facts)
    session.data["author_unknown_facts"] = reduced
    session.data["author_removed_pointer"] = removed
    if removed:
        tail.append(
            blocks.section(
                "*One more, and this is the beat that matters.* I will remove exactly one fact — "
                "`{}` — and change nothing else. Not replace it with a plausible value: remove "
                "it, the way a real case arrives when nobody can establish something.".format(
                    removed
                )
            )
        )
    else:
        tail.append(
            blocks.section(
                "*One more.* This scenario has no fact left to remove, so the next run evaluates "
                "the pack with an EMPTY facts document — the most honest hole there is. Whatever "
                "it does with that is its own wiring speaking."
            )
        )
    tail.append(continue_bar(ID, "unknown", "Remove the fact and re-run →"))
    return FlowResult(replies=pending + [reply(tail, text="Narration")])


def _do_unknown(turn, deps):
    session = turn.session
    decision_id = session.data.get("author_decision_id", "gifts-hospitality")
    facts = session.data.get("author_unknown_facts") or {}
    evidence = (session.data.get("author_envelope") or {}).get("evidence")
    removed = session.data.get("author_removed_pointer")
    project = deps.runtime.ensure_project(session)

    ran = deps.runtime.evaluate(project, decision_id, facts, evidence, label="newborn-unknown")
    if not ran.ok or not ran.payload:
        return FlowResult(
            replies=[reply(blocks.error_blocks("The evaluation refused", ran.message()))],
            done=True,
            failed=True,
        )

    body = blocks.disposition_blocks(
        ran.payload,
        title=(
            "The same case with `{}` unknown".format(removed)
            if removed
            else "The same pack with an empty facts document"
        ),
        facts=facts,
        evidence=evidence,
    )
    pending = flush(deps, [reply(body, text="The unknown case")])

    narration = deps.model.narrate(
        turn.user_id,
        ran.payload,
        pack=session.data.get("author_pack"),
        facts=facts,
        evidence=evidence,
    )
    tail = blocks.narration_blocks(narration.text, deps.model.name, note=narration.note)
    kind = (ran.payload.get("disposition") or {}).get("kind")
    if kind == "unresolved":
        tail.append(
            blocks.section(
                "*Policy to reviewed artifact in minutes — and the first thing it did with a hole "
                "in the facts was refuse to fill it.* Nothing in that pipeline trusted the "
                "model's judgment, only its labor."
            )
        )
    else:
        tail.append(
            blocks.section(
                "*Read that honestly:* this pack does not escalate on that missing fact — the "
                "trace above shows what it did instead. `onUnknown` is wired rule by rule, and "
                "this draft wired it that way. That is a review finding, not a bug, and it is "
                "exactly the kind of thing a reviewer catches in a diff."
            )
        )
    return FlowResult(replies=pending + [reply(tail, text="Narration")], done=True)
