# Enterprise judgment demo — the 6-minute script

The story in one line: **policy as reviewed code, judgment as a deterministic artifact, the
agent as an honest clerk — and escalation, not hallucination, when something is unknown.**

Stack: OpenHands (agent-canvas) at `localhost:8000/canvas`, this project open, the
`jpack` MCP server pre-seeded against `enterprise-demo/jpack.json`.
**Model: `gemini-3.1-pro-preview`** — check the selector on every new chat; the lite models
paraphrase instead of running tools (a rehearsal on flash-lite hand-drew its own diagram).
The conversation's built-in **Files pane** (right side) is the browse surface. Before the demo:
`docker compose up -d`, then:

1. **One window — chat**: `localhost:8000/canvas` → New Chat **with the enterprise-demo
   project selected**, so the right-hand **Files pane** shows `packs/`, `requests/`,
   `evidence/` — that pane is the file-browsing surface.
2. Run `./scripts/reset-demo.sh` (clears rehearsal leftovers — Act 2 must start with
   three packs, not four).

## 0. Frame (30s, no typing)

Point at the left nav: "These JSON files are our procurement and finance policies — reviewed,
versioned, tested like code. The agent can read them and gather inputs, but it cannot decide:
the runtime evaluates the pack deterministically, same bytes on any machine."

## 1. Browse the portfolio (30s)

> **Prompt:** What judgment packs does this project hold? One line each.

Expect: the agent calls `list_packs` and lists `vendor-onboarding`, `expense-approval`,
`intake-triage` with their questions. Mention the matrices: every pack ships its regression
suite (14 rows, byte-exact).

## 2. Present the deep pack (45s)

> **Prompt:** Show me the vendor-onboarding pack: every outcome, the conditions that
> reach it, and what escalates to whom.

Expect: the agent reads the document (`get_pack`) and renders a **markdown table** in chat
— outcomes × conditions, the sanctions hard stop, the committee threshold, the escalation
target — labeled as its reading of the pack, with what the view omits stated. The table
renders natively in the chat, and every element traces to a document member. Say: *"the
model chose the presentation; the pack supplied every fact in it — and the file in the
Files pane is the only authority."*

## 3. The clean approval (60s)

> **Prompt:** Evaluate the vendor onboarding request in requests/northwind-analytics.md.
> Use the screening and tax records in evidence/. Report anything you cannot source as
> unknown.

Expect: facts built from the request (spend as the decimal string `"84000"`), evidence
`present` from the two Northwind files, `experimental_evaluate` → **approve**, then the
trace-grounded narration: which rule fired, which conditions held, which evidence was read.

## 4. The hard stop (60s)

> **Prompt:** Now evaluate requests/meridian-maritime.md the same way.

Expect: the screening record shows a MATCH → the `sanctions-match-hard-stop` exception forces
**reject** whatever else is true. The narration quotes the exception and the screening record.
"The agent didn't decide to reject — the policy did, and the trace shows exactly why."

## 5. The escalation moment (90s — the money shot)

> **Prompt:** A new request just came in: onboard "Aurora Fabrication GmbH",
> new-vendor-onboarding, annual spend 45,000 USD, submission complete, W-9 received. There is
> no screening record yet. Evaluate it honestly.

Expect: no screening record in `evidence/` → the screening status pointer is omitted and the
evidence reported honestly; the pack refuses to guess: **unresolved**, reason `unknown`,
handoff **requested → Vendor risk committee**. The narration says which condition was unknown
and that the handoff is a request recorded, not a delivery.

Optional twist, same prompt family: give the spend as `600000` with full evidence — the
committee threshold exception escalates **directly**, even though everything is documented.
"Two different escalations: one because something is unknown, one because the policy says a
human decides above the threshold."

## 6. Close (30s)

- Ask: *Should we onboard Aurora anyway?* — the agent must answer that the payload asserts
  nothing about the wisdom of acting; the committee the pack names decides. **The honesty is
  the product.**
- Everything seen is reproducible: same inputs, same bytes, on any machine — and the runtime,
  image, and registry entry are public (`ghcr.io/judgment-pack/judgment-pack`,
  `io.github.Judgment-Pack/judgment-pack`).

## Act 2 — author a pack live (optional, ~5 min)

The encore: a new policy becomes a reviewed, validated, diagrammed artifact while they watch.

> **Prompt:** Encode this policy as a new judgment pack called gifts-hospitality:
> "Employees may accept gifts or hospitality up to 50 USD without approval, provided the
> giver is not a government official and the gift is entered in the gift register.
> Anything above 50 USD, or anything from a government official, must be declined or
> referred to Compliance. If the value or the giver's status cannot be established, refer
> to Compliance."

Expect, in order (nudge with the step name if the agent stalls):

1. A draft pack: outcomes like `accept` / `decline` / `refer-compliance`; a decimal-string
   threshold ("50"); a government-official gate; a gift-register evidence requirement;
   `onUnknown: escalate` wired to a Compliance escalation target.
2. **The validate loop in chat** — the draft goes to the `validate` tool, diagnostics come
   back naming locations and fixes, the agent repairs. Let one failure happen visibly:
   *"the validator, not the model, is the gatekeeper."*
3. Registration: the file lands in `packs/` (watch the Files pane), `jpack.json` grows an
   entry, `packs validate` passes in the terminal.
4. A small matrix + `packs test` — the new pack has a regression suite minutes after birth.
5. Ask for the new pack as a table — same presentation, grounded in the file it just
   wrote. Close: *"policy to reviewed artifact, minutes, and nothing in that pipeline
   trusted the model's judgment — only its labor."*

### Act 2b — the newborn pack judges (~2.5 min, after the table)

6. **Record and accept**
   > **Prompt:** A vendor's account manager took our engineer to a working lunch yesterday
   > — 32 USD, the giver works for a private company. Enter it in the gift register and
   > evaluate it honestly.

   Expect: a register record appears under `evidence/` (a clerical record of what you
   stated), facts with the decimal string "32" → **accept**, both rules narrated. Say:
   *"the pack you watched being born just made its first decision."*

7. **The unknown giver** (Act 2's money shot)
   > **Prompt:** Another one: a 45 USD gift basket, but we cannot establish whether the
   > giver's employer is state-owned. It is in the register. Evaluate honestly.

   Expect: the official-status fact omitted, not guessed → **unresolved (unknown)** →
   handoff **requested → Compliance**. The policy's own sentence — "if the giver's status
   cannot be established, refer to Compliance" — happening live in a pack minutes old.

8. **Not in the register** (optional)
   > **Prompt:** One more: a 20 USD coffee gift card nobody entered in the register.
   > Evaluate it.

   Expect: the register requirement unmet → never an auto-accept; decline/refer or
   escalation per the pack's shape. Absence is a different answer than unknown.

The pack is generated live, so its exact structure varies per run — judge the beats by
policy behavior (over-50 declines, unknown status escalates, unregistered gifts never
auto-accept), not by rule names.

**Act 2 fallbacks**: if the agent writes the file before validating, ask it to validate the
document with the validate tool now and fix what it reports. If matrix expectations
mismatch on bytes, have it run one evaluation and copy the actual disposition — mismatches
are diffs, not mysteries. If time is short, stop after step 3: validated and registered is
already the story.

## Act 3 — the declared graph (~3 min)

Act 1 bridges the screening decision into vendor onboarding by hand: someone records
`/vendor/sanctionsScreening/status` as a fact. The runtime's experimental composition surface
(its ADR-0015, shipped in 0.8.0, which this demo pins) declares that seam instead:
`graphs/vendor-onboarding.graph.json` makes `sanctions-screening` its own decision node whose
outcome lands at exactly that pointer — and whose resolution state is the `sanctions-screening`
evidence — so onboarding consumes a decision, not a transcribed value.

1. Rebuild against the pinned runtime and reset as usual (the compose default is already a
   release carrying `experimental graph`).
2. In the sandbox terminal (or ask the agent to run these):
   - `jpack experimental graph validate graphs/vendor-onboarding.graph.json` — every reference
     checks out against jpack.json and the packs.
   - `jpack experimental graph explain graphs/vendor-onboarding.graph.json` — the plan:
     screening first, then onboarding, with both feeds stated. Nothing is evaluated.
   - `jpack experimental graph evaluate graphs/vendor-onboarding.graph.json --inputs
     graphs/inputs-northwind-clear.json` — screening resolves `clear`, the edge injects it,
     evidence flips to present, onboarding resolves `approve`.
   - Same with `graphs/inputs-meridian-match.json` — screening resolves `match`, the hard-stop
     exception forces `reject`, and the composite's headline says so.
   - Same with `graphs/inputs-unresolved-screening.json` — the money shot again, one level up:
     screening cannot resolve, **no fact is injected and the evidence arrives unknown**, so
     onboarding escalates through its own declared rules, and the composite aggregates BOTH
     requested handoffs (screening → Compliance, onboarding → Vendor risk committee). Nothing
     guessed, everything attributed.
3. Ask the agent to narrate the composite from the payload: the per-node dispositions, the
   feeds (`injected` vs `not injected`), the handoffs, and the §3.5 line — the payload asserts
   nothing about the wisdom of acting.

**Act 3 fallbacks**: if the runtime predates the graph surface, `jpack experimental graph`
prints an unknown-command error — say so and fall back to Act 1's manual bridge, which is the
same story told by hand. A validation refusal names its exact defect; read it aloud — refusing
loudly is the demo.

## Fallbacks

- Agent narrates without evaluating → "Call the experimental_evaluate tool with pack_id
  vendor-onboarding and show me the payload."
- Agent invents a screening status in step 5 → "Where is the screening record? Report what
  you cannot source as unknown." (The pack still protects you: a guessed `clear` is the one
  thing the honesty rule exists to prevent — restate it.)
- Presentation looks wrong or invented → "Every row must trace to a member of the pack
  document — rebuild the table from get_pack and state what it omits."
- Model/API hiccup → `packs test` in the terminal shows the 14-row byte-exact suite as the
  determinism proof without any model.
