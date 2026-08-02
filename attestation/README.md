# The attested screening desk

The [reference gateway](https://github.com/Judgment-Pack/judgment-pack-gateway)
wired into this demo as a screening desk: the agent asks the desk to screen a
counterparty, the desk performs the screening inside its own trust domain and
signs a chained receipt for the bytes it saw, and a deterministic script — not
the model — verifies the store against the sealed registry and derives the
graph inputs `jpack` evaluates. What this buys: the agent can *ask* for a
screening and can even vandalize the store afterwards, but it cannot mint a
screening value that carries a valid receipt.

## The trust domain

Inside the gateway container, unreachable from the sandbox:

- the Ed25519 signing seed and the seal registry (`gateway-state/private/`,
  mounted only here — the registry sits beside the seed so the anchor is
  served by the key holder, never read from the store under audit);
- the source program ([ofac-screening-source.py](ofac-screening-source.py))
  and its baked world ([watchlist.json](watchlist.json)) — the sandbox holds
  copies of these files (same image), but the gateway consults its own.

In the sandbox:

- the pin (`gateway-state/pin/`, read-only) — written once at provisioning
  from `gateway keygen`'s stdout, never fetched from `/publickey` (asking the
  audited gateway for its own key proves consistency, not authenticity);
- the store (`gateway-state/public/`, read-write) — receipts and
  content-addressed artifacts. Deliberately tamperable: that is the demo.

## The decision desk

The screening desk attests an *input*. The decision desk (`attest decide`,
[decision-desk-source.py](decision-desk-source.py)) attests a *judgment*: it
runs `jpack experimental evaluate` inside the gateway container and the
disposition itself becomes the receipted artifact.

What makes it a desk is where the law lives. The gateway image bakes a copy of
the project at `/usr/local/share/desk/enterprise-demo`, laid down at build time
from the **build context** — the checkout the image was built from, not HEAD.
The sandbox holds its own working copy — editable, forgeable, and completely
inert as far as the desk is concerned. The copy you can edit is not the copy
that judges.

**The bound that wording states honestly.** The desk's law is as reviewed as the
tree the image was built from. Build from a reset checkout and that is the
reviewed tree; run `docker compose up -d --build` while a rehearsal forgery is
in place and the forgery is what gets baked. So the operational rule is: reset
before you build (`./scripts/reset-demo.sh`), and DEMO.md's pre-flight and its
rebuild fallback both say so.

The runtime's **reviewed-set lock** (runtime ADR-0019) is what upgrades that
from discipline to a verified bound. The image build already runs
`jpack packs verify --config jpack.json` against the baked copy whenever the
project carries a `jpack.lock.json`: inert today, and a hard build gate the
moment the lock is committed — a build whose baked law does not match the
reviewed set it declares fails rather than shipping a desk that judges under
law nobody reviewed.

That is the wall the runtime's own reviewed-set lock (runtime ADR-0019) names
and does not build. A lock makes an amendment explicit and recorded, which is
worth having; it cannot stop an editor who shares the law's write domain,
because the command that re-declares the reviewed set is available to whatever
can edit a pack. Moving the deciding party out of that domain is the part no
check inside it can perform.

**What the receipt binds**: the decision id the caller asked for
(`requestedDecisionId`), the pack identity and version the desk actually applied
(`packId`, `packVersion`), the facts and evidence documents as asked, and the
§8.3 disposition the desk's evaluator produced — one signed artifact, so a
disposition cannot be separated from the question it answers, and the asked-for
id is in the signed bytes rather than only in the receipt's `argumentsDigest`,
which is keyed and opaque to a public verifier.

The desk's copy declares its own audit directory, so it also keeps its own
decision book on its side of the wall. It is mounted from
`gateway-state/desk-audit/` on the **gateway service only** — never on the
sandbox — so it survives the `--force-recreate` the recovery notes push, and is
still somewhere the agent cannot reach. `reset-demo.sh` clears it with the
store: it is rehearsal state like everything else there.

**Honest bounds.** The desk's law updates only by rebuilding the image from the
reviewed tree: it is as current as the last build, and a policy change that has
not been rebuilt is a policy change the desk has not adopted. The receipt proves
byte-lineage of the *decision*, not the truth of the inputs — the facts are
still the caller's claims, and a receipted `match` over invented facts is a
faithful judgment of an invented question. And a refusal is a refusal: an
evaluation the desk's runtime declines (an unknown decision id, a malformed
document) comes back as a loud 400, never as a disposition.

## The rules the glue honors

1. The registry (anchor) is fetched from the key holder over HTTP, never read
   from the store being audited.
2. The public key is pinned out of band; `/publickey` is never called.
3. Verification is delegated to the reference `gateway verify` binary — never
   re-implemented — and the verdict is read from the JSON findings, never the
   exit code (exit 0 covers `ok:false`; non-zero means no verdict was
   reachable at all). The verdict is **scoped to this session**: every finding
   the verifier emits carries a `sessionId`, and `attest` requires all of this
   session's findings to be `ok` (and at least one to exist). Rehearsal debris
   in other sessions therefore cannot withhold a fresh screening — a
   store-wide `ok:false` beside `this session: ok` is expected, not a defect.
4. Derivation consumes only store bytes covered by a verified receipt (never
   the HTTP response the caller kept) and applies the corpus-tested rule from
   the [experiments repo](https://github.com/Judgment-Pack/judgment-pack-evaluator-experiments)
   (`derivation-rule/rules/screening.rule.json`) — the derivation is data, and
   two independent implementations agree on it byte for byte.
5. A failed verification derives `screening-record: unknown` — never `absent`,
   never a guessed count. Withholding is the answer, not an error.

## Honest bounds

The gateway proves **byte-lineage, not truth**: these bytes came from the
operator-configured desk, unaltered since. The desk's world is a baked
synthetic snapshot; the operator authored it, and nothing here evidences that
it matches the real OFAC lists. The desk also attests only *that the given
string was screened* — it does not know which vendor is under evaluation, and
the template supplies the onboarding facts, so screening a name variant (or
naming the wrong template) produces a valid receipt about the wrong question.
`attest check` prints the receipted `screenedLegalName` for exactly this
reason; the narration must quote it against the request's exact legal name. The microagent instruction to use `attest` is
a nudge the model sees, not a boundary it obeys — the boundary is that a
fabricated screening block cannot carry a valid receipt, and the graph edge
(`One requirement has one source`) refuses hand-written values at the fact
pointer the edge feeds.

Sharing the sandbox's network namespace puts the unauthenticated
`/acquire`/`/seal` surface on the same loopback as every tool the agent runs.
Nothing reachable there can forge a receipt; the worst a confused tool can do
is seal a session early, and `attest` mints a fresh session id per run.

## Recovery

- Gateway unreachable (typical after the sandbox container restarts or is
  recreated): `docker compose up -d --force-recreate gateway`. Bare `restart`
  fails once the sandbox was recreated; plain `up -d` is a no-op while the
  container still reports Up.
- Store poisoned by rehearsals: `./scripts/reset-demo.sh` wipes the store and
  registry together (a store without its registry can never verify again) and
  recreates the gateway. The identity survives resets.
- Seed or pin lost (not both): the entrypoint refuses to serve and prints the
  one command that mints a fresh identity.
