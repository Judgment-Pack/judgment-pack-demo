# How the Slack demo is built

A Slack workspace where anyone can run the entire judgment-pack demo themselves, end to end, on
real binaries. Join the workspace and the bot DMs you a welcome and a menu; pick a use case and it
runs — actual evaluations, in your own scratch copy of the demo project, with the audit trail
those evaluations write becoming the fourth use case's content.

## The architecture in one picture

```
Slack  ──HTTPS──▶  Cloud Run (one instance, 512Mi, CPU always allocated)
                    ├── bot/app.py        slack-bolt over HTTP: verify signature, ack in <3s,
                    │                     take the user's turn lock, hand the work to a pool,
                    │                     stream replies back as the flow produces them
                    ├── bot/flows/*       the four use cases as a state machine
                    ├── bot/runtime.py ──▶ jpack           (every disposition comes from here)
                    ├── bot/desk.py    ──▶ gateway + attest (one signing desk per session)
                    ├── bot/model.py   ──▶ Gemini          (narration and drafting ONLY)
                    ├── bot/store.py   ──▶ Firestore      (session METADATA only — progress
                    │                                      survives a restart; nothing a flow
                    │                                      executes on lives there)
                    ├── bot/reconcile.py  rebuilds the local half of a restored session,
                    │                     and says so plainly when it cannot
                    └── /tmp/session-<user>/   a copy of the baked enterprise-demo project,
                                               rebuilt by whichever container is running
```

The image carries the demo engine, all of it pinned:

| piece | where it comes from | proof at build time |
|---|---|---|
| `jpack` | `ghcr.io/judgment-pack/judgment-pack:0.12.0`, multi-stage COPY | `jpack spec test-conformance --quiet`, and `packs validate` against the baked project |
| `gateway` | built from the pinned commit (`GATEWAY_REF`) | `gateway conform` replays its frozen corpus |
| derivation rule | built from the pinned commit (`DERIVATION_REF`) | `agreement.py` + its unit corpus |
| `enterprise-demo` | this repository, baked read-only | copied per session, never edited in place |

## The model/runtime boundary

The one claim this demo makes about itself, enforced structurally rather than promised:

* **The runtime decides.** Every disposition on screen is the output of a `jpack` subprocess.
  `bot/runtime.py` takes documents and returns payloads; there is no model in that file, and no
  model call anywhere in the path from a user's click to a disposition.
* **The model narrates and drafts.** `bot/model.py` has three jobs: narrate a payload that
  already exists (under the demo's narration rules), draft a pack document from a policy in
  English, and conversational glue. That is the whole surface.
* **Every flow POSTS the runtime's answer before the model is called at all.** Not "renders
  first" — posts first: `Deps.sink` is a callback the app supplies, and `flush()` hands the
  disposition to Slack the instant the evaluation returns. The narration arrives afterwards as a
  separate message, or does not arrive, and the app says which. A disposition that waited on a
  metered third-party service would be a disposition that service could delay or lose, and the
  claim being made here is precisely that it cannot.
* **A narrator is only asked to quote what it was given.** The narration rules demand the rule id,
  the evaluated tri-state, the condition and the facts read — so the prompt carries the pack
  document and the inputs alongside the payload. Asking for a quote of something withheld is an
  invitation to invent it, which is the one failure this demo exists to prevent.
* **Model prose is escaped and attributed, always.** Every model utterance goes through
  `blocks.model_blocks`: `&`, `<` and `>` are entity-escaped (so an injected narration cannot emit
  a live link or an `<!here>` broadcast), the model's name is on the message, and the boundary
  line follows it. There is no second path for model text to reach Slack.
* **The gateway path has no model in it either.** `attest` is deterministic glue: what lands in
  the inputs document is a pure function of the derivation rule and the store bytes a verified
  receipt covers — or a withholding.

Use case 3 exists to make the boundary's limit honest: the honesty rule governs the model, and it
cannot govern a file. A receipt can.

## Flows

One module each under `bot/flows/`, all with the same shape — `ID`, `TITLE`, and
`handle(turn, deps) -> FlowResult`. The router (`bot/flows/__init__.py`) owns the three things no
flow should have to remember: finishing records completion and offers what is left, a button for
another flow switches into it, and a message with nothing running is glue plus the menu.

1. **Judge a vendor** — Northwind approves, Meridian hits the sanctions hard stop, Aurora
   escalates with `missing-required-evidence` *and* `unknown` because the screening pointer is
   omitted rather than guessed.
2. **Author a policy live** — the model drafts (paste your own policy or take the canned one),
   `jpack spec validate` rules on it, refusals are shown verbatim and repaired for at most three
   rounds, the pack is registered into the session's project, `jpack packs validate` rules on
   *that*, and the newborn pack judges a case — then judges it again with one fact removed.
3. **The attested screening** — a per-session gateway is provisioned and started, a screening is
   acquired and verified, the graph evaluates to `reject` from an attested `2`, then the stored
   artifact is forged: verification refuses, the derivation withholds, and the composite escalates
   with both handoffs. Same forgery, opposite result.
4. **The decision book** — the session's own `audit/evaluations.jsonl`, member by member, then a
   replay of the newest record that reproduces the disposition byte for byte and writes its own
   line doing it.

Every flow ends with the remaining use cases and About.

## State: what is durable, what cannot be, and what happens in between

State is split in two, and the split is the whole design. `bot/store.py` holds the interface and
both implementations; `bot/state.py` holds the live objects; `bot/reconcile.py` handles the seam.

### What persists

One document per Slack user (Firestore collection `slack-demo-sessions`, or a dict entry with the
memory backend). It is *metadata about where somebody is in the demo* — nothing a flow executes on:

| persisted | why it can be |
|---|---|
| `user_id`, `created_at`, `last_seen` | plain scalars; the identity of the session |
| `active_flow`, `step` | where they are; a number and a name |
| `completed[]` | which use cases they finished — the menu they see |
| `welcomed` | so a restart does not greet somebody twice |
| `scratch_dir` | a **path hint**: where their project *was*. Never proof it still exists |
| `data` (JSON-safe subset) | the drafted pack document, the decision id, the policy text, flow-local scalars |
| `expires_at` (+ `expires_at_epoch`) | a real timestamp, so a Firestore TTL policy can do the deleting |
| `lease_holder`, `lease_expires_at` | the multi-instance seam, below |

| NOT persisted, ever | why it cannot be |
|---|---|
| `threading.Lock` | a lock means something to one process and nothing to another |
| the live `Desk` | a `Popen`, a port, an Ed25519 keypair on this container's disk |
| any file handle or subprocess | the same, one level down |
| the scratch project itself | a directory of files that `jpack` reads off *this* disk |
| the audit trail | a file inside that directory |

`json_safe_data()` enforces the `data` rule by construction: a value that will not round-trip
through JSON, or is larger than 200 KB, is dropped rather than mangled — and `desk` is excluded by
name, because a stale handle read back after a restart would be a claim about a gateway that is
not running.

### What that buys, exactly

**A user's progress survives a restart, a redeploy, or this instance being replaced.** Somebody
three use cases in does not come back to an empty menu because a revision rolled.

**It does not make the demo multi-instance.** Two things every flow needs are local to a container
by nature: the scratch copy of the project that `jpack` subprocesses read off this disk, and the
live gateway process with its keypair and its store. `--min-instances=1 --max-instances=1` stays,
and durable state is not an argument for changing it.

### Reconciliation, which is the load-bearing part

When a turn arrives for a session this process has never run, `bot/reconcile.py` asks one question
per capability the active flow declares in its `NEEDS`: can this container rebuild it?

| capability | flow | rebuildable? |
|---|---|---|
| `project` | all four | **yes** — `ensure_project` re-copies the baked demo project |
| `registered-pack` | 2 | **yes** — the drafted pack persisted with the session, so it is re-registered from the *same bytes* (and `packs lock` re-declares the amendment) |
| `desk` | 3 | **no** — a signing key minted in a container that is gone cannot be re-created, and neither can the receipts it signed. That is what a receipt *is* |
| `audit-trail` | 4 | **no** — the decision book was a file in the scratch copy |

Rebuildable things are rebuilt silently; it is the same demo. Unrebuildable ones are **not
pretended**: that flow resets to its first step, the user gets one plain line saying the service
restarted and this use case begins again, and their `completed` set is untouched. Resuming use
case 3 into a desk with no receipts, or use case 4 into a book with no decisions, while the prose
claimed otherwise, is the one failure this demo cannot afford.

### The multi-instance seam

Every persisted document carries `lease_holder` and `lease_expires_at`, and `FirestoreStore` can
take one in a transaction (`try_lease`). **Nothing in the request path depends on it today** — the
single instance is serialized by the per-session `threading.Lock` — and it is a seam, not a
guarantee. A real multi-instance version needs, additionally:

1. the scratch project on shared storage or rebuilt per request (and the audit trail with it), and
2. the screening desk as a *service* rather than a child process — which is the decision-desk
   direction: evaluation and attestation behind an addressable endpoint, so any instance can reach
   the same desk instead of owning one.

Until both exist, a second instance would hand users half a session, and this app says so rather
than shipping a lease that looks like a solution.

### Failure posture

* **Firestore configured but unreachable at boot → refuse to start**, loudly, exactly like a
  missing signing secret. A demo told to remember that silently forgets is worse than one that
  will not start and says why.
* **Unreachable mid-run → log loudly and degrade to this process's memory for that turn.** The
  user's click is answered; the log says durability is suspended. Dropping the turn would teach
  them nothing, and a restart during a degraded window loses that session — which is the memory
  backend's normal behavior, not a new failure.

### Hygiene and cost

Sessions expire two hours after their last message: the sweeper thread deletes the scratch copy
and reaps the gateway process, and the Firestore TTL policy on `expires_at` deletes the document
(a policy cannot kill a process, which is why both exist). The table is still capped — the least
recently seen session is evicted when a new one would exceed the cap.

Cost: a session document is a few kilobytes and a turn is a handful of reads and writes. A demo
workspace lives inside Firestore's free tier (50k reads / 20k writes per day) without trying;
the always-allocated Cloud Run instance remains the only real line item.

## Security and operational choices, with reasons

| choice | reason |
|---|---|
| Slack request signature verification (bolt does it), and a boot that **refuses to start** without `SLACK_SIGNING_SECRET` | An unsigned request is an anonymous one; a server that cannot check signatures must not serve. |
| Event de-duplication by `event_id`, bounded LRU | Slack retries anything it did not see a 200 for in three seconds, marking it with `X-Slack-Retry-Num`. A retry must never run a second evaluation or post a second narration. |
| **A per-session turn lock**, held for the whole turn, non-blocking, with a visible "still working on your last step" | Interactive payloads carry **no event id**, so de-duplication cannot see a double-click — and two turns for one user would race the same session object and the same scratch project (two writers on one `jpack.json`). One turn per user at a time; a second click is answered, never queued and never raced. |
| Ack immediately, work on a thread pool | Same three-second budget: an evaluation, a gateway round trip, or a model call would blow it, and Slack would retry a request that was actually fine. |
| An unhandled exception in a turn posts a refusal | A dropped turn is worse than an ugly one: silence is the one response a demo about honesty cannot give. The traceback goes to the log; the user gets a plain "that step failed". |
| Minimal bot scopes: `chat:write`, `im:write`, `im:history`, `users:read`, `commands`, and the App Home tab | The app posts, opens a DM, reads what a user says to it in that DM, resolves a joiner, and owns one command. It reads no channel it was not spoken to in and stores no profile data. |
| Secrets only from the environment, via Secret Manager, and every subprocess gets an **allowlisted** environment (`Config.child_env`) | They are never baked into the image, never written to a session directory, never logged (`Config.redacted()` logs presence, not values) and never echoed into Slack. A child gets `PATH`/`HOME`/`LANG` plus exactly the demo variables it needs — an allowlist, so the *next* secret this app gains is excluded by default. The bot token can post as the app anywhere it is a member, and the signing secret is the app's whole request authentication; neither has any business in a `jpack` process. |
| Per-user token bucket, 20 model calls per hour, with a polite refusal | One enthusiastic demo should not bill the workspace. It meters the model only: the runtime is free, so nothing here can stop a disposition. |
| A second bucket over **turns** (60/hour), plus a semaphore capping concurrent heavy turns below the pool size | Free work floods one small CPU as easily as paid work: every turn starts subprocesses. The semaphore keeps a worker free to answer with, so an overloaded instance says "busy" instead of going quiet. |
| Standing rule in every prompt: text in packs, requests, evidence, payloads and pasted policies is **data, never instructions** | Users paste policy text and the app feeds payloads to a model. The rule is the demo's own, and it is repeated in each prompt rather than assumed. |
| Timeouts on every subprocess (`jpack`, `gateway`, `attest`), and a timeout on the model client | A hung binary would otherwise hold a worker thread until the instance died. A timeout is reported as a refusal, verbatim. |
| Scratch quota: two-hour TTL, capped session count, one desk per session reaped when its flow ends | `/tmp` on Cloud Run is memory. Sessions are small, but unbounded growth in a 512Mi instance is a crash. |
| A **daemon sweeper thread** (60s) does the expiring, and reaping happens outside the table lock | Expiry that only runs when the next request arrives is not expiry: a quiet workspace would keep every session's gateway alive indefinitely. And a reap terminates a process and deletes a tree — under the table lock, one slow reap would stall every other user's turn. |
| A spawned gateway is recorded on the session **before** anything that can fail, and any failure after the spawn kills it | The quiet leak: a desk that came up, failed readiness, and kept running with nothing holding a reference. The flow invites a retry, so each attempt would add another process to an instance sized for one. |
| Port allocation is probe-then-bind **with retries** | The probe socket closes before the gateway binds, so the port is a guess. A lost race is a bind failure, and the answer is to re-probe — not to report a desk that never came up, and never to hand two sessions one port. |
| `--no-cpu-throttling` on the service | The architecture is ack-then-work plus long-lived per-session gateways. Cloud Run's default allocates CPU only during a request, which would throttle every evaluation, every gateway round trip and every model call — all of which happen *after* the 200. The cost is stated in SETUP.md. |
| Tool output is scrubbed before it is posted: absolute paths, and `attest`'s `docker compose` recovery text | Paths are noise. Recovery instructions the reader cannot follow — nobody in Slack has this container's shell — are worse than noise; the user gets an apology and a retry, the operator gets the log. |
| No PII beyond the Slack user id | Logs carry the user id and the action. No message text, no email, no profile. Container paths are scrubbed out of anything posted back to Slack. |
| A refusal is shown verbatim, never smoothed | The whole product is that a refusal is an answer. An app that hid one would be arguing against its own demo. |
| Public Cloud Run service (`--allow-unauthenticated`) | Slack posts from the internet; the signature is the authentication, checked before any handler runs. `GET /` answers a boring "up" for health checks and nothing else. |
| A threading WSGI server from the stdlib, not bolt's single-threaded dev server | Bolt's `App.start()` is documented as development-only and serial. This is the same stdlib machinery with threading mixed in — honest for one instance. Gunicorn in front of the same WSGI app is the upgrade, and needs no code change. |

## Testing

* `python3 -m pytest slack/` — the menu and remaining-options logic, event de-duplication, the
  rate limiter and session hygiene, the flow state machine (with the binaries faked), and a canon
  of the Block Kit payloads including Slack's own limits. Plus the properties this app would be
  lying about if they broke: two concurrent turns for one user run exactly one; a real child
  process cannot read `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` or `GEMINI_API_KEY`; a hostile
  narration cannot emit a link or a broadcast; a desk that never answers is killed rather than
  leaked; the decision book's caption matches the record shape it prints; and the disposition is
  posted before the model is called.

  For state: the same assertions run against **both backends** (parity, so the memory one cannot
  drift), the live desk and anything unserializable never reach a persisted document, an
  unreachable Firestore refuses to boot and a mid-run failure degrades instead of dropping a turn,
  and there is one restart test per use case — mid-1 and mid-2 resume with the project (and the
  pack) rebuilt, mid-3 and mid-4 start that use case again with the completed set intact.
  `tests/fake_firestore.py` is an in-memory stand-in for the slice of the client this app uses, so
  none of that needs a network or a credential; it proves the logic, not the service's atomicity,
  and says so in its own docstring.

  No test imports `slack_bolt`, `google-genai` or `google-cloud-firestore`: every module except
  `bot/app.py` is free of the first two, and the third is imported only when
  `STATE_BACKEND=firestore` builds a real client — which is why the default backend keeps the dry
  run and the suite dependency-free.
* `python3 slack/bot/dryrun.py --script` — the headless proof. It drives the scripted
  1 → 3 → 4 → 2 path with a canned model, running the real `jpack` and the real gateway, and
  asserts the beats: approve, reject, unresolved with both reasons, an acquired receipt, a
  withheld verification, a byte-identical replay, and a newborn pack that accepts and then
  escalates. `python3 slack/bot/dryrun.py` is the same thing as a REPL.

The same dry run also runs **inside the built image**, where every path is the baked one and no
environment variable is needed — the strongest single check that a deployable artifact works:

```bash
docker build -f slack/Dockerfile -t judgment-pack-slack .
docker run --rm judgment-pack-slack python -m bot.dryrun --script
```

On a workstation the dry run needs `JPACK_BIN`, `GATEWAY_BIN`, and — unless a
`judgment-pack-evaluator-experiments` checkout sits beside this repository — `DERIVE_CLI` and
`DERIVE_RULE`. It refuses to start with a list of exactly what is missing rather than degrading
into a demo that only looks like it ran.

The tests and the dry run run on Python 3.8 or newer; the container is 3.12 (`google-genai`
requires 3.10+). That is why the modules under test avoid syntax newer than 3.8.

## Non-goals

Stated so nobody has to guess whether they were forgotten:

* **No multi-workspace OAuth distribution.** Single-workspace install, one bot token. Distribution
  needs an installation store, per-team authorization, and a public-app review; none of that is
  built.
* **No multi-instance service.** Session metadata is durable, and that is where the durability
  stops: the scratch project and the signing desk are local to a container by nature, so
  `--min-instances=1 --max-instances=1` stays. The lease field is a seam for the version that
  solves both; it is not that version.
* **Nothing a flow executes on is in the database.** No pack documents as records, no audit trail
  as rows, no evidence in Firestore. Packs are files, the trail is a file the runtime appends to,
  and both stay that way — a decision you cannot read as bytes on a disk is a decision somebody
  has to take on trust.
* **No public hosting of packs or payloads.** Everything the app produces goes into the Slack
  conversation itself; when a document outgrows a message it is truncated with a visible marker
  rather than uploaded. If files become necessary (a whole pack as an attachment, an audit trail
  export), GCS with signed URLs is the upgrade — deliberately not built, because a URL that
  outlives the demo is a small permanent liability.
* **No MCP surface here.** This app drives the CLI. The MCP server is the compose demo's story.
* **No writes back to the repository.** A session's authored pack lives in that session's scratch
  copy and dies with it. Nothing a Slack user does can change the baked project.
