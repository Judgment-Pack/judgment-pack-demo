# The Slack self-serve demo

Join the workspace, get a welcome, and run the whole judgment-pack demo yourself from four
buttons — real evaluations, real refusals, in your own scratch copy of the demo project.

| use case | what happens |
|---|---|
| 1 · Judge a vendor | A clean approval, a sanctions hard stop, and the escalation the honesty rule exists for. |
| 2 · Author a policy live | Your policy text → a drafted pack → the validator's verdict (refusals shown, repaired, or stopped honestly) → registration → the newborn pack judges. |
| 3 · The attested screening | A signed receipt for an acquired screening, then a forgery of the stored artifact: verification refuses and the evaluation is withheld. |
| 4 · The decision book | The audit trail your own choices wrote, member by member, and a replay that reproduces a disposition byte for byte. |

The runtime decides; the model narrates and drafts. Nothing the model says changes a disposition.

Session state is durable when it is configured to be (`STATE_BACKEND=firestore`): a user's
progress survives a restart or a redeploy, while the scratch project and the signing desk — which
belong to whichever container is running — are rebuilt, and whatever cannot be rebuilt is said out
loud rather than pretended.

- **Deploying it:** [SETUP.md](SETUP.md) — 8 human steps, about 15 minutes.
- **How it is built and why:** [DESIGN.md](DESIGN.md) — the architecture, the model/runtime
  boundary, the security choices with their reasons, what persists and what cannot, and the
  non-goals.
- **The Slack app definition:** [app_manifest.yml](app_manifest.yml).

Prove it without Slack, without an API key, on the real binaries:

```bash
python3 -m pytest slack/
# Paths to the binaries you built — `$(which jpack)` works too, when they are
# on PATH under exactly those names; a build sitting in a sibling checkout is
# not, and an empty variable silently falls back to the bare name.
JPACK_BIN=../judgment-pack-runtime/jpack \
GATEWAY_BIN=../judgment-pack-gateway/go/gateway \
  python3 slack/bot/dryrun.py --script
```
