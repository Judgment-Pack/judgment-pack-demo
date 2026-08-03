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
   four packs, not five).
3. **If you rebuild the image, reset first.** The decision desk's law (Act 6) is copied
   from the build context — this checkout as it stands — so `docker compose up -d --build`
   over a dirty tree bakes whatever is in `projects/enterprise-demo` at that moment. Reset,
   then build. (The build runs `packs verify` against the baked copy once the project
   carries a reviewed-set lock, which turns this from a habit into a gate.)

## 0. Frame (30s, no typing)

Point at the left nav: "These JSON files are our procurement and finance policies — reviewed,
versioned, tested like code. The agent can read them and gather inputs, but it cannot decide:
the runtime evaluates the pack deterministically, same bytes on any machine."

## 1. Browse the portfolio (30s)

> **Prompt:** What judgment packs does this project hold? One line each.

Expect: the agent calls `list_packs` and lists `vendor-onboarding`, `sanctions-screening`,
`expense-approval`, `intake-triage` with their questions. Mention the matrices: every pack
ships its regression suite (18 rows, byte-exact).

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
evidence reported honestly (`sanctions-screening: absent` — nobody made a record, which is not
the same as being unable to check); the pack refuses to guess: **unresolved**, reasons
`missing-required-evidence` **and** `unknown`, handoff **requested → Vendor risk committee**.
Both reasons are the honest answer: the required screening evidence is absent *and* the
hard-stop condition cannot be evaluated. The narration says which condition was unknown and
that the handoff is a request recorded, not a delivery.

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
(its ADR-0015; the pinned runtime carries it) declares that seam instead:
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
   - `jpack experimental graph test` — no arguments: the project declares its graph and rows in
     `jpack.json` (configVersion 2, its ADR-0017), and the walk runs the declared suite exactly
     as `packs test` runs the matrices. Four byte-exact rows: the three stories above plus a
     committee-threshold row (screening clear, spend 600000, everything documented — escalation
     by policy, not by ignorance; `graphs/inputs-committee-threshold.json` runs it by hand if
     asked). The same determinism proof `packs test` gives, one level up, and it needs no model
     at all.
3. Ask the agent to narrate the composite from the payload: the per-node dispositions, the
   feeds (`injected` vs `not injected`), the handoffs, and the §3.5 line — the payload asserts
   nothing about the wisdom of acting.

**Act 3 fallbacks**: if the runtime predates the graph surface, `jpack experimental graph`
prints an unknown-command error — say so and fall back to Act 1's manual bridge, which is the
same story told by hand. A validation refusal names its exact defect; read it aloud — refusing
loudly is the demo.

## Act 4 — the attested input (~3 min)

Act 3 declared the seam, but the inputs file was still something someone typed. This act
acquires the screening **through the screening desk** — the reference gateway running beside
the sandbox — so the number the graph consumes carries a signed, chained receipt, and the
derivation from receipt to inputs is a corpus-tested rule, not the model. The honest bound,
said out loud: the desk proves **byte-lineage, not truth** — these bytes, from that desk,
unaltered since — and its world is a baked synthetic snapshot. What it removes is the model
from the proof path: a fabricated screening value cannot carry a valid receipt.

Pre-flight (with the usual reset): from the sandbox terminal,
`curl -fsS 127.0.0.1:8787/publickey` answers — if not:
`docker compose up -d --force-recreate gateway`.

1. **Acquire** (agent or terminal — same commands, one code path):

   > **Prompt:** Screen "Meridian Maritime Holdings SA" through the screening desk and
   > evaluate vendor onboarding from the attested result. Use attest, and narrate what the
   > desk did and did not claim.

   The agent runs `attest screen "Meridian Maritime Holdings SA" --template
   graphs/inputs-meridian-match.json`, then `attest check`, then the graph evaluation on
   `attested/screening-inputs.json`. Expect: the receipt summary (authority, keyId, digest),
   `verify … this session: ok` with the registry fetched from the key holder, the derivation
   `resolved`, and the same **reject** as the hard stop (§4) — now from an attested "2".

2. **The diff beat**: `diff attested/screening-inputs.json graphs/inputs-meridian-match.json`
   is empty. The desk route reproduced the committed fixture byte for byte — same decision,
   different provenance; only one of the two routes can prove nobody edited it afterwards.

3. **Tamper — the peak.** The store is mounted read-write in the sandbox on purpose:

   ```
   attest tamper --match-count 0     # forge a "clear" into the stored artifact
   attest check                      # artifact-mismatch → WITHHELD, evidence unknown
   jpack experimental graph evaluate graphs/vendor-onboarding.graph.json \
     --inputs attested/screening-inputs.json
   ```

   Expect: **unresolved (unknown)**, no fact injected, both handoffs — the forged "clear"
   could not carry a valid receipt, so the derivation refused to assert anything. *"Act 1's
   honesty was a rule the model follows. This one is arithmetic."*

4. **Optional, irreversible variant**: `attest rollback` deletes the newest signed receipt —
   the sealed registry still promises it existed (`tail-rollback`), and nothing can re-create
   its signature. Use this beat for a sharp audience member who says "just re-run it".

**Act 4 fallbacks**: desk unreachable → `docker compose up -d --force-recreate gateway`
(the expected trigger is a host/WSL restart; bare `restart` and plain `up -d` do not fix it).
`session is sealed` → just re-run `attest screen`; every run mints a fresh session. Store
poisoned from rehearsals → `./scripts/reset-demo.sh` (wipes store + registry together,
identity survives; it also discards uncommitted edits under this project, by design). A
verification failure you didn't stage → read the findings aloud; refusing loudly is the demo.
Full boundary notes: `attestation/README.md` in the repo root.

## Act 4b — the same question, with and without the desk (~2.5 min)

One question — *may Meridian be onboarded?* — answered by three routes that produce the same
decision bytes with different provenance, then the beat that separates them: the **same
forgery** against each route. The scenario matrix (every row rehearsable on its own):

| # | scenario | route | do | expect |
|---|----------|-------|----|--------|
| 1 | records honest | file (§4) | prompt: evaluate `requests/meridian-maritime.md` | **reject** (hard stop) |
| 2 | record forged | file | edit the record (below), re-prompt §4 | **approve — believed** |
| 3 | store honest | desk (Act 4) | `attest screen` + `check` + evaluate | **reject**, receipt shown |
| 4 | store forged | desk | `attest tamper` + `check` + evaluate | **unresolved — withheld** |
| 5 | desk stopped | desk | `docker compose stop gateway`, then `attest screen` | loud failure, no silent fallback |
| 6 | desk absent | file | with the gateway still stopped, re-run row 1 | reject — file route needs no desk |

**The forgery contrast (rows 2 vs 4)** — run them back to back:

1. Forge the file record:
   `sed -i 's/- Result: .*/- Result: 0 matches — CLEAR/' evidence/meridian-maritime-ofac-screening.md`
   then the §4 prompt again. Expect **approve**, narrated honestly from the forged record —
   the clerk is honest, the record lied, and *nothing in this route can tell the difference*.
   Say: *"the honesty rule governs the model. It cannot govern the file."*
   Restore before moving on: `git checkout -- evidence/` (or `./scripts/reset-demo.sh`).
2. Forge the desk's store the same way (`attest tamper --match-count 0`, then `attest check`
   and the evaluation): **unresolved (unknown)**, both handoffs. Same forgery, opposite
   result. Say: *"one route believed the forgery; one refused it. The difference is not a
   better model — it is a receipt."*

**The desk-absent scenarios (rows 5-6)**: `docker compose stop gateway`, then
`attest screen …` — it fails loudly (`cannot reach the gateway`, with the recovery command)
instead of silently falling back to the file route. That refusal is the behavior: *no
attestation* is a stated answer, never an assumed one. Row 6 shows the file route still
works without the desk — the demo degrades to exactly what it was before Act 4, and says
nothing about provenance. Bring the desk back: `docker compose start gateway` (it was
stopped, not orphaned — after a host restart use the force-recreate line instead).

**Act 4b fallbacks**: rehearse row 2 last if you are worried about resetting — it dirties a
tracked file, and `reset-demo.sh` restores it (along with everything else uncommitted here).
If the agent hesitates to re-evaluate the edited record, that is the microagent's honesty
rules working as designed — tell it the record is the source and to report what it states;
the beat lands either way, because faithful transcription of a forged record is the exact
gap the desk closes.

## Act 5 — the decision on the record (~2 min)

This project declares an audit directory (`jpack.json`, `configVersion "3"`), so every
evaluation the audience has watched — the agent's MCP calls, the graph runs, the attested
act — is already a line in `audit/evaluations.jsonl`. Nothing to enable; the book was being
written the whole time. (`reset-demo.sh` clears it with the rest of the rehearsal state, so
the book starts at the demo's first decision.)

1. **Show the book** (Files pane or terminal):

   ```bash
   tail -1 audit/evaluations.jsonl | jq '{run, kind, surface, pack: .pack.id, digest: .pack.digest}'
   ```

   Point at the members while narrating: the run id, the surface that decided, the SHA-256
   of the exact pack bytes, and — open the full line if asked — the facts as evaluated and
   the disposition in its §8.3 canonical form. *"This is not a log line. It is the decision,
   with everything needed to run it again."*

2. **The replay** (the closing beat of the whole demo). Take the newest single-pack record
   and evaluate it back:

   ```bash
   REC=$(grep '"kind":"evaluation"' audit/evaluations.jsonl | tail -1)
   echo "$REC" | jq '.inputs.facts'    > /tmp/replay-facts.json
   echo "$REC" | jq '.inputs.evidence // {}' > /tmp/replay-evidence.json
   jpack experimental evaluate packs/vendor-onboarding.pack.json \
     --facts /tmp/replay-facts.json --evidence /tmp/replay-evidence.json
   jq -c '.disposition' audit/evaluations.jsonl | tail -2 | uniq -c
   ```

   The replay itself is a decision, so it wrote its own line — and `uniq -c` shows `2` of
   one disposition: the book just audited itself. *"Same bytes in, same judgment out, and
   the trail proves it. That is the whole product in one line."*

   (If the newest record came from a different pack, name that pack's file instead — the
   record's `pack.id` says which document it was.)

**Act 5 fallbacks**: `jq` missing → `tail -1 audit/evaluations.jsonl` raw; the line is
readable. The book empty → run any act first; a fresh reset starts an empty book. An
evaluation refusing with `JPS-AUDIT-WRITE` → something is occupying `audit/`;
`./scripts/reset-demo.sh` and rerun.

## Act 6 — the wall (~2.5 min)

Act 4b showed a forged *record* believed by one route and refused by another. This act forges
the **law itself**, exactly as it happened live: an agent met a refusal, edited the project
until the refusal stopped, evaluated, and reverted. Nothing inside the sandbox can object to
that — the agent and the law share one write domain, and a rule that lives in that domain is a
rule its addressee can rewrite.

1. **Forge the local law.** The screening pack clears a counterparty when the recorded match
   count is exactly zero, and reports a match at one or more. Redefine both — clear now means
   two, and "one or more" now means nine or more:

   ```bash
   sed -i -e 's/"value": "0"/"value": "2"/' -e 's/"value": "1"/"value": "9"/' \
     packs/sanctions-screening.pack.json
   jpack experimental evaluate --pack-id sanctions-screening \
     --facts requests/desk-facts-screening.json \
     --evidence requests/desk-evidence-screening.json
   ```

   **The lock objects first.** This project is locked (`jpack.lock.json`), so the deciding
   surface refuses the drifted pack outright — `JPS-LOCK-VERIFY`, `document-drift`, with the
   steer naming both legitimate exits. The exact live incident that motivated all of this —
   edit the law, evaluate, revert — now fails at step two. *"Silent tampering stopped being
   evaluable the day this project got its lock."*

   Note that it took two edits, not one: change only the first and both rules fire and the
   pack refuses with a conflict — the pack's own structure resists a lazy forgery too.

   **But the lock is evidence, not a wall.** The forger can do what the steer says and declare
   the amendment:

   ```bash
   jpack packs lock
   jpack experimental evaluate --pack-id sanctions-screening \
     --facts requests/desk-facts-screening.json \
     --evidence requests/desk-evidence-screening.json
   ```

   Now two matches say **clear** — honestly, from a forged, re-locked law, and the only trace
   is a lock diff in the working tree. The evaluation is honest, the trace is honest, the
   audit record is honest (`reviewed: true`, against the forger's own lock!) — every one of
   them honest about a forged law. *"Inside one write domain, the lock can only make tampering
   leave tracks. It cannot make it stop working. For that you need a copy the forger cannot
   reach."*

2. **Ask the desk the same question.** Same pack id, same facts, same evidence:

   ```bash
   attest decide sanctions-screening \
     --facts requests/desk-facts-screening.json \
     --evidence requests/desk-evidence-screening.json
   ```

   **match** — with a receipt: authority, `keyId`, `resultDigest`, and the disposition itself
   signed as the artifact. The desk evaluated against its own copy of this project, baked into
   the gateway container from the checkout the image was built from. The forged pack is two
   directories away and completely irrelevant to it.

   Show the receipted judgment: `jq . attested/decision.json` — the pack id and version, the
   facts as asked, the §8.3 disposition, the desk's own project name.

   **Stage line:** *"the copy you can edit is not the copy that judges. That is the wall."*

3. **Restore** before moving on — in the sandbox terminal, from the sandbox's own read-only
   copy of the reviewed tree (the same image bytes the desk holds, root-owned here so nothing
   in this container can edit them), law and lock together:

   ```bash
   cp /usr/local/share/desk/enterprise-demo/packs/sanctions-screening.pack.json packs/
   cp /usr/local/share/desk/enterprise-demo/jpack.lock.json .
   ```

   — an inert copy repairs the working copy, which is the act's thesis running in reverse.
   (Restoring only the pack would leave the forger's lock pinning forged bytes, and the next
   deciding evaluation would refuse. From the host, `./scripts/reset-demo.sh` restores both.
   Plain `git checkout` does not work inside the sandbox — `/projects` carries no repository.)

The honest bound, if asked: the desk's law is whatever tree the image was built from, and it
updates only by rebuilding — so it is as reviewed as your build discipline (hence the reset
before every build, above). Its receipt proves byte-lineage of the *decision*, not the truth
of the facts — those are still the caller's claims. What it removes is the one thing the sandbox
could otherwise do silently: change the law and get an answer that looks the same.

**Act 6 fallbacks**: desk unreachable → `docker compose up -d --force-recreate gateway` (bare
`restart` cannot rejoin a recreated namespace); the desk's book survives that, it is a host
mount. Forgot to restore the forged pack → repeat beat 3's `cp` in the sandbox, or
`./scripts/reset-demo.sh` from the host; `jpack packs test` in the sandbox then passes again,
which is also how you prove the restore landed. `attest decide` exiting 3 (NO DECISION) means
*this run's own* receipt or artifact failed verification — a store that lost its registry, or
one wiped under a live gateway; earlier acts' tampering cannot cause it, because each decide
mints its own session and the verdict is scoped to it. Recover with `./scripts/reset-demo.sh`
(which recreates the gateway against the empty store) and rerun. If the local evaluation in
beat 1 comes back *unresolved (conflict)* instead of clear, only the first sed landed — both
rules fire; run the second `-e` too. If it refuses another way, check `grep -n '"value"'
packs/sanctions-screening.pack.json` against the two rule thresholds.

## Fallbacks

- **Agent reports an unsupported configVersion — or reaches for an edit to `jpack.json`**
  → the running container predates the checkout (a rebuild landed but the stack kept the
  old image). Fix the sandbox, never the config: `./scripts/reset-demo.sh` first (a build
  copies the working tree into the decision desk — never bake a mid-act forgery), then
  `docker compose up -d --build`, then `docker compose up -d --force-recreate gateway`. An evaluation obtained by editing the
  project's declarations is Act 4b's forgery lesson wearing different clothes — and it
  writes no record. (Observed live: an agent sed'd the version down, evaluated, and
  reverted; the microagent now forbids it, and jpack ≥0.12.0 refusals name the upgrade.)
- **Agent says it has no MCP tools** (e.g. asked to "list your MCP tools") → it almost
  certainly has them; models are poor at enumerating their own tool set. Do not debug the
  stack — just give it work: "Call list_packs and show me what this project holds." If that
  returns the four packs, the wiring is fine. To confirm from the terminal:
  `docker compose exec openhands sh -lc 'JPACK_CONFIG=/projects/enterprise-demo/jpack.json jpack mcp'`
  and send an `initialize` + `tools/list` pair — it answers with nine tools.
- Agent narrates without evaluating → "Call the experimental_evaluate tool with pack_id
  vendor-onboarding and show me the payload."
- Agent invents a screening status in step 5 → "Where is the screening record? Report what
  you cannot source as unknown." (The pack still protects you: a guessed `clear` is the one
  thing the honesty rule exists to prevent — restate it.)
- Presentation looks wrong or invented → "Every row must trace to a member of the pack
  document — rebuild the table from get_pack and state what it omits."
- Model/API hiccup → `packs test` in the terminal shows the 18-row byte-exact suite as the
  determinism proof without any model.
- Everything comes back unresolved with every condition unknown → two causes, and the trace
  tells you which. If the trace's first line is `{"stage":"applicability","condition":"unknown"}`
  the facts are missing `/request/type` (`new-vendor-onboarding`) and the pack never became
  applicable — nothing else was even evaluated. Otherwise the facts document is probably flat
  pointer-named members instead of the nested object the pointers descend into; the worked shape
  is in `.openhands/microagents/repo.md`.
