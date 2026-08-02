# judgment-pack demo

A clone-and-go sandbox for [Judgment Packs](https://github.com/Judgment-Pack/judgment-pack-runtime):
one `docker compose up` gives you a browser workspace — chat, file explorer, editor, terminal —
with the judgment-pack runtime pre-wired as an MCP server and a verified sample project ready to
evaluate. **Bring your own key**: you pick the model provider and paste your API key into the UI;
the judgment-pack runtime never sees a key, opens no network connection, and stores nothing.

Everything here is non-normative demo material. The evaluator's conformance claim is stated, in
full and only, in the runtime release's `CONFORMANCE.md`; nothing in this repository restates it.

## Quick start (browser sandbox)

Prerequisites: Docker (Docker Desktop on Windows/macOS).

```bash
cp .env.example .env      # defaults are fine
docker compose up --build
```

Open http://localhost:8000/canvas. On first run, open **Settings**, choose your model provider,
and paste your API key (BYOK — any supported provider works; a frontier-class model gives the
best results in the scripted asks below). Then open the `judgment-pack-quickstart` project in the
file browser.

The judgment-pack MCP server registers itself on first boot: the `mcp-seed` oneshot container
([scripts/seed-mcp.sh](scripts/seed-mcp.sh)) writes it into agent-canvas settings through the
API, pointed at the quickstart project. Verify under **Settings → MCP** — the entry reads
`jpack` / command `jpack` / args `mcp`.

### Where packs live

Packs are plain JSON files on your disk — never in a database. The file browser's root is the
`PACKS_DIR` directory from your `.env` (default: this repo's [projects/](projects/)). Point
`PACKS_DIR` at any directory you prefer; every folder inside it becomes a project in the
sandbox, and everything the agent writes lands there, visible in the file explorer and in your
own file manager.

## The three-minute thesis (no AI involved)

In the sandbox terminal (or any shell with the binary on PATH):

```bash
cd /projects/judgment-pack-quickstart

# A full facts document decides:
jpack experimental evaluate packs/data-request-intake-triage.pack.json \
  --facts full-facts.json --evidence evidence.json
# → disposition: outcome proceed, with a trace of exactly which rules fired

# Delete a load-bearing fact and re-run — it ESCALATES instead of guessing:
jq 'del(.request.completeness)' full-facts.json > partial-facts.json
jpack experimental evaluate packs/data-request-intake-triage.pack.json \
  --facts partial-facts.json --evidence evidence.json
# → disposition: unresolved (unknown), handoff requested to "Intake reviewer",
#   trace showing each rule that went unknown and escalated
```

Same inputs, same bytes, on any machine — that's the conformance claim doing its job. The
`trace` in every payload is the pack-grounded reasoning: which exception and rule evaluated to
what, which unknowns were escalating, which were ignored.

## The demo script (what to ask the agent)

1. *"List the judgment packs in this project and describe what each decides."* → `list_packs` →
   one pack, `intake-triage`, with its evidence requirements and source hints.
2. *"Evaluate the intake-triage pack with these facts …"* (paste a matrix row's `facts` and
   `evidenceAvailability` from [the matrix](projects/judgment-pack-quickstart/packs/data-request-intake-triage.matrix.json))
   → `experimental_evaluate` with `pack_id` → a disposition with the pack's identity echoed and
   the rule-by-rule trace.
3. **The escalation test**: same request with the completeness facts removed, asking the agent to
   evaluate *honestly, reporting anything it cannot source as unknown*. Correct behavior:
   `unresolved`, reasons `["unknown"]`, handoff requested — the model didn't guess; the pack
   refused to.
4. **Authoring**: say *"author a judgment pack for this policy: …"* — the workspace's
   [authoring microagent](projects/judgment-pack-quickstart/.openhands/microagents/authoring.md)
   guides the agent through the create → validate → fix loop to exit-0, writing the pack and its
   prepared-facts ledger as real files you can watch appear in the file explorer. Ask it to add
   matrix rows and run `jpack packs test` to lock the behavior in.

## Other clients (same server, your tools)

This repo also carries ready-made wiring for native MCP clients — the binary must be on the
PATH ([releases](https://github.com/Judgment-Pack/judgment-pack-runtime/releases)):

- **Claude Code**: [.mcp.json](.mcp.json) is picked up when you open this repo; MCP prompts
  appear as slash commands (`/mcp__jpack__author_pack`, after the server name in [.mcp.json](.mcp.json)).
- **VS Code / Copilot**: [.vscode/mcp.json](.vscode/mcp.json), prompts as `/mcp.jpack.…`.
- **GitHub Codespaces / devcontainer**: open this repo in a Codespace — the
  [devcontainer](.devcontainer/devcontainer.json) installs the pinned release and proves the
  conformance corpus on creation.
- Any other client: see the runtime's
  [MCP client guide](https://github.com/Judgment-Pack/judgment-pack-runtime/blob/main/docs/mcp-clients.md).
  Clients that don't surface MCP prompts can use the pasted method file in
  [prompts/author_pack-prompt.txt](prompts/author_pack-prompt.txt).

## Trust boundary

- **Your key stays yours.** The model runs in your sandbox with your key; the judgment-pack
  runtime never sees either (it is keyless, network-free, and stateless).
- **Reads are jailed.** The server reads files only through a reader rooted at the project's own
  directory; a path that leaves it is refused.
- **Nothing here authorizes anything.** A disposition is the §8.3 portable result of applying a
  pack to facts; whether to act on it is yours (JPS §3.5).
- **The screening desk holds the only signing key.** The attestation gateway's seed and seal
  registry live in a mount the sandbox never sees ([attestation/README.md](attestation/README.md));
  the agent can request attestations and even tamper with the store, but it cannot sign,
  re-pin, or touch the anchor. What that proves is byte-lineage, not truth.

## Troubleshooting

- **Tools missing in chat** — check Settings → MCP for the `jpack` entry; re-run the
  seeding with `docker compose run --rm mcp-seed` (idempotent), or add the entry in the UI.
- **`list_packs` answers empty** — it says where it looked; the server's `JPACK_CONFIG` must
  name your project's `jpack.json` by its container path (under `/projects/…`). Edit the entry
  in Settings → MCP, or re-seed with `JPACK_CONFIG=/projects/<your-project>/jpack.json
  docker compose run --rm -e JPACK_CONFIG mcp-seed`.
- **Binary problems** — `docker compose exec openhands jpack version` must report the
  pinned release; `jpack spec test-conformance --quiet` (exit 0) proves the published
  corpus passes inside your container, offline.
- Versions are pinned in [.env.example](.env.example) and re-verified weekly by
  [drift CI](.github/workflows/drift.yml).

## The enterprise demo

[`projects/enterprise-demo`](projects/enterprise-demo) is the presentable demo: a vendor-onboarding
decision with a sanctions-screening hard stop (cited to the public OFAC list), an expense policy,
and the intake-triage pack — each with a byte-exact instance matrix. The MCP server is seeded
against this project by default. [`DEMO.md`](projects/enterprise-demo/DEMO.md) is the 6-minute
script: browse, present the pack as a grounded table, the clean approval,
the forced reject, and the escalation the honesty rule exists for.

### Attested inputs (Act 4)

The demo also runs the [reference attestation gateway](https://github.com/Judgment-Pack/judgment-pack-gateway)
as a *screening desk* beside the sandbox (same network namespace, `127.0.0.1:8787`, published
nowhere). `attest` acquires a sanctions screening through it, verifies the store against the
sealed registry under an out-of-band-pinned key, and derives the graph inputs with the
corpus-tested rule from the
[experiments repo](https://github.com/Judgment-Pack/judgment-pack-evaluator-experiments) —
so the number the vendor-onboarding graph consumes carries a signed, chained receipt, and the
tamper beat in [DEMO.md](projects/enterprise-demo/DEMO.md) shows a forged value failing
verification instead of being believed. Both are built from pinned commits at image build
time (`GATEWAY_REF` / `DERIVATION_REF` in [.env.example](.env.example)); boundary and
recovery notes live in [attestation/README.md](attestation/README.md).
