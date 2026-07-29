---
name: judgment-pack-authoring
type: knowledge
triggers:
  - judgment pack
  - author a pack
  - encode a policy
  - jpack
---

# Authoring a Judgment Pack in this workspace

Encode ONE policy decision as a Judgment Pack, working in a validate-and-fix loop
against the jpack MCP tools (or the `jpack` CLI in the terminal).
Write pack documents as real files in this project's `packs/` directory and
register them in `jpack.json` so `list_packs` and `packs test` see them.

Ask for the policy text or decision description if you do not already hold it.

Work in this order:

1. ORIENT. Call `get_schema` for the format reference and `list_examples` for the
   bundled valid fixtures; fetch the one closest to your decision with
   `get_example` and use its SHAPE (not its content) as scaffolding. Fixtures are
   version-pinned conformance cases, not templates.

2. SCOPE. One pack = one decision ("may X be done?"), with the outcomes the
   policy itself names. Agent procedure ("confirm before acting") is not a
   decision; leave it out. If the policy names a case that must go to a human,
   that is the escalation machinery, not an outcome.

3. STRUCTURE for the resolution model. Two rules that fire together naming
   DIFFERENT outcomes are a conflict and the result is unresolved — there is no
   rule priority. Two shapes work well:
   - Detector style: every rule detects one outcome (say, a violation → deny);
     the opposite outcome is reachable only as fallbackOutcome.
   - Carve-out style: encode "if A then deny, otherwise allow when B" with a
     force-outcome or escalate EXCEPTION for A (exceptions outrank rules) and
     rules for B.
   Pick one deliberately; mixing affirmative and negative rules invites
   unresolved conflicts.

4. CONDITIONS. Facts are addressed by RFC 6901 JSON Pointer into one facts
   document. Ordered comparisons (greater-than family) are defined only over
   decimal STRINGS matching -?(0|[1-9][0-9]*)(\.[0-9]+)? — a JSON number on
   either side yields unknown, silently. The format has no arithmetic, no
   date/time comparison, and no quantifier over arrays: any such value must be
   prepared upstream and supplied as a fact. Keep a PREPARED-FACTS ledger file
   next to the pack: every fact that is computed or concluded rather than stated
   by the requester, and for each, whether producing it requires applying the
   policy itself (flag those loudly — they are decision logic living outside the
   pack).

5. UNCERTAINTY is the point. `onUnknown: escalate` on a rule means "a missing
   fact here must stop the decision"; `onUnknown: ignore` means "this rule simply
   does not fire". Gate rules with an always-present boolean condition where you
   can. Never invent a fact value: a value you cannot source is `unknown`, and
   the pack — not you — decides what unknown does.

6. EVIDENCE. `evidenceRequirements` with `required: true` block every evaluation
   that arrives without an evidence document — use it only for evidence the
   decision truly cannot proceed without.

7. LOOP. `validate` the draft; every diagnostic names its location and the fix;
   repair and repeat to exit 0. Then evaluate against 2–3 realistic facts
   documents (`experimental_evaluate`, or add matrix rows and run
   `jpack packs test`) and check the dispositions match intent —
   including one probe with a load-bearing fact REMOVED, which should escalate,
   not guess.

8. RECORD. Keep interpretation decisions (ambiguous policy text, the reading you
   chose, why) and the prepared-facts ledger next to the pack. They are the audit
   trail of everything the pack itself cannot say.

(Method guidance from the judgment-pack runtime, non-normative: following it
does not make a pack conformant, and only the `validate` tool / `spec validate`
decides conformance. The documents you produce belong to this workspace; the
runtime stores nothing and decides nothing.)
