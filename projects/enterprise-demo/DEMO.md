# Enterprise judgment demo — the 6-minute script

The story in one line: **policy as reviewed code, judgment as a deterministic artifact, the
agent as an honest clerk — and escalation, not hallucination, when something is unknown.**

Stack: OpenHands (agent-canvas) at `localhost:8000/canvas`, this project open, the
`judgment-pack` MCP server pre-seeded against `enterprise-demo/jpack.json`. Before the demo:
`docker compose up -d`, open the project, confirm the left file tree shows `packs/`,
`requests/`, `evidence/`, `diagrams/`.

## 0. Frame (30s, no typing)

Point at the left nav: "These JSON files are our procurement and finance policies — reviewed,
versioned, tested like code. The agent can read them and gather inputs, but it cannot decide:
the runtime evaluates the pack deterministically, same bytes on any machine."

## 1. Browse the portfolio (30s)

> **Prompt:** What judgment packs does this project hold? One line each.

Expect: the agent calls `list_packs` and lists `vendor-onboarding`, `expense-approval`,
`intake-triage` with their questions. Mention the matrices: every pack ships its regression
suite (14 rows, byte-exact).

## 2. Visualize the deep pack (45s)

> **Prompt:** Show me the vendor-onboarding pack as a diagram.

Expect: the agent runs `judgment-pack packs diagram --id vendor-onboarding`, writes
`diagrams/vendor-onboarding.md`, and pastes the mermaid fence in chat. **Open
`diagrams/vendor-onboarding.html` in a browser** (it renders offline) — walk the flow:
sanctions hard-stop exception, the committee spend threshold, the `reads` edges into the
screening evidence. "This diagram is generated from the pack, deterministically — it cannot
drift from what was reviewed."

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

## Fallbacks

- Agent narrates without evaluating → "Call the experimental_evaluate tool with pack_id
  vendor-onboarding and show me the payload."
- Agent invents a screening status in step 5 → "Where is the screening record? Report what
  you cannot source as unknown." (The pack still protects you: a guessed `clear` is the one
  thing the honesty rule exists to prevent — restate it.)
- Diagram fence doesn't render in chat → the pre-built `diagrams/vendor-onboarding.html` is
  the visual; it needs no network.
- Model/API hiccup → `packs test` in the terminal shows the 14-row byte-exact suite as the
  determinism proof without any model.
