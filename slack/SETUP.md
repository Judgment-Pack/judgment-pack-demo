# Setting it up — 8 steps, about 15 minutes

Two of these steps only a human can do (creating the Slack app, holding the keys). The rest is
one script. Do them in this order; nothing here is ambiguous, and nothing is optional.

You need: a Slack workspace you can install apps into, a Google Cloud project with billing (the
demo fits comfortably in free-tier-scale usage), `gcloud` installed, and a Gemini API key.

---

## 1. Create the Slack app from the manifest

Open <https://api.slack.com/apps> → **Create New App** → **From a manifest** → pick your
workspace → paste the whole of [`app_manifest.yml`](app_manifest.yml) → **Create**.

Slack **challenges the Event Subscriptions request URL** the moment you save, and nothing answers
at `example.invalid` yet, so expect it to refuse. The recovery, in order:

1. Delete the single line marked `DELETE-IF-REFUSED` in the manifest — it is
   `request_url: https://example.invalid/slack/events` under `settings.event_subscriptions` — and
   paste again. The `bot_events` list stays.
2. If it still refuses, delete the whole `event_subscriptions:` block (four more lines) and paste
   again. You will re-add the URL *and* the three bot events in step 7.1.

The interactivity and slash-command URLs are not challenged, so those placeholders can stay until
step 7.

## 2. Install it and copy the bot token

**Install App** (left sidebar) → **Install to Workspace** → **Allow**.

Copy the **Bot User OAuth Token** — it starts `xoxb-`. Keep it in your clipboard manager or a
password manager for two minutes; you paste it in step 6. Do not put it in a file, a shell
history, or a chat message.

## 3. Copy the signing secret

**Basic Information** → **App Credentials** → **Signing Secret** → **Show** → copy.

This is what proves a request came from Slack. The app refuses to start without it.

## 4. Get a Gemini API key

<https://aistudio.google.com/apikey> → create a key in the same Google account that owns the
cloud project. This key buys narration and drafting only: every disposition in the demo is
produced by the `jpack` binary in the container, with no key and no network.

## 5. Point gcloud at your project

```bash
gcloud auth login          # required: an expired token cannot be refreshed by a script
gcloud config set project <YOUR_PROJECT_ID>
```

`gcloud auth login` opens a browser. Every step below fails at the door without it, by design —
the scripts check for a usable token before they create anything.

Every setting the deployment has — project, region, service name, the model, the instance
shape — lives in one gitignored file. Copy it once and edit what you care about; an exported
variable still overrides it, and with no file at all you get the defaults:

```bash
cp slack/deploy/deploy.env.example slack/deploy/deploy.env
```

**Prefer a project of its own?** One command creates it, attaches billing, records the id in
that file, and deploys — this demo runs drafted packs and a signing gateway per user, so its
own billing line and a single `gcloud projects delete` to erase it are worth the minute:

```bash
./slack/deploy/new-project.sh                 # id defaults to jpack-slack-demo-<yymmdd>
./slack/deploy/new-project.sh my-own-id       # or name it yourself
```

It needs an account allowed to create projects and attach billing (many organizations reserve
both). If yours does, ask for an empty project and use step 5 as written. With more than one
open billing account it stops and lists them; re-run with `BILLING_ACCOUNT=<name>`. Then it
hands off to `deploy.sh` — so skip step 6.

## 6. Deploy

From the repository root:

```bash
./slack/deploy/deploy.sh
```

It enables the five APIs it needs (Cloud Run, Cloud Build, Secret Manager, Artifact Registry and
**Firestore**), creates an Artifact Registry repository, then asks for the three secrets **one at
a time, with input hidden**, and stores them in Secret Manager:

| prompt | what to paste |
|---|---|
| `SLACK_BOT_TOKEN` | the `xoxb-…` token from step 2 |
| `SLACK_SIGNING_SECRET` | the signing secret from step 3 |
| `GEMINI_API_KEY` | the key from step 4 |

A secret already in Secret Manager is left alone, so re-running is safe. If you cannot type at
the prompt — CI, or a key already sitting in a file — point `<NAME>_FILE` at it instead:

```bash
printf %s "$(grep '^GEMINI_KEY=' .env | cut -d= -f2-)" > /tmp/gk && chmod 600 /tmp/gk
GEMINI_API_KEY_FILE=/tmp/gk ./slack/deploy/deploy.sh
shred -u /tmp/gk
```

A file, not an environment variable holding the value: `ps` and `/proc/<pid>/environ` show the
second one to every process on the machine.

Next it sets up **session state**: a Native-mode Firestore database if the project has none, read
and write access for the service account, and the TTL policy that deletes abandoned session
documents. What that buys is narrow and worth being precise about — a user's *progress* (where
they are, what they have finished) survives a restart or a redeploy. It does **not** make the demo
multi-instance: the scratch copy of the project and the live signing desk belong to whichever
container is running, so `--min-instances=1 --max-instances=1` stays, and a restart mid-run
rebuilds what it can and tells the user plainly what it cannot (`slack/DESIGN.md`).

If the TTL step cannot run automatically, the script prints the one command to run yourself — it
is idempotent, so running it anyway is harmless:

```bash
gcloud firestore fields ttls update expires_at \
  --collection-group=slack-demo-sessions --enable-ttl --project <YOUR_PROJECT_ID>
```

Without that policy nothing breaks — the app ignores expired documents and its own sweeper still
deletes scratch directories and reaps desks — but the documents accumulate forever. Confirm it:

```bash
gcloud firestore fields ttls list --collection-group=slack-demo-sessions --project <YOUR_PROJECT_ID>
```

To run without a database at all, set `STATE_BACKEND=memory` in `slack/deploy/deploy.env`: the app
behaves exactly as it did before this was added, and forgets every session when a revision rolls.

Then it builds the image with Cloud Build (the build fails loudly if the pinned runtime does not
pass its own conformance corpus, if the gateway commit disagrees with its frozen corpus, or if the
derivation rule disagrees with its own — about 5 minutes the first time) and deploys one Cloud Run
revision, pinned to exactly one instance with CPU always allocated.

**What that costs, stated plainly:** `--min-instances=1 --no-cpu-throttling` means one instance
with 1 vCPU and 512Mi is billed continuously, not per request — on the order of a few tens of
dollars a month at list price, comfortably inside a credits budget and *not* inside the free tier.
(Firestore is the cheap half: a session document is a few kilobytes and a turn costs a handful of
reads and writes, so a demo workspace stays inside the free tier without trying.)
Both flags are required by the design rather than chosen for speed: the app answers Slack in
milliseconds and does the real work after that 200 (throttled CPU would slow exactly the part that
matters), and each session's screening desk is a long-lived process that must keep running between
clicks. To pause the demo without deleting anything:
`gcloud run services update judgment-pack-slack-demo --region us-central1 --min-instances=0`
— sessions in flight pause rather than vanish: with the Firestore backend their progress is
still in the collection when the next click wakes the service, and the flow they were inside is
rebuilt or restarted per `slack/DESIGN.md`.

It finishes by printing a URL like:

```
    https://judgment-pack-slack-demo-xxxxxxxx-uc.a.run.app/slack/events
```

Re-running the script later is safe: existing secrets are left alone.

## 7. Paste that URL into three fields

Back at <https://api.slack.com/apps> → your app. The **same URL** goes in all three places:

1. **Event Subscriptions** → toggle on → **Request URL** → paste → it must say **Verified ✓**.
   Then open **Subscribe to bot events** and confirm all three are listed — adding any that are
   missing (they will be, if you took either fallback in step 1):
   `team_join`, `app_home_opened`, `message.im`. **Save Changes**.
2. **Interactivity & Shortcuts** → toggle on → **Request URL** → paste → **Save Changes**.
3. **Slash Commands** → `/jpack` → **Edit** → **Request URL** → paste → **Save**.

If Slack asks you to **reinstall** the app after any of these, do it — the scopes are unchanged;
Slack just wants to re-confirm.

## 8. Say hello

Open Slack → find **Judgment Pack Demo** under **Apps** → the **Home** tab shows the menu, your
progress, and the About surface; the **Messages** tab gets you the welcome and the same buttons.
Try `/jpack` in any channel, and send the bot a DM saying `menu`.

New members of the workspace now get the welcome automatically when they join.

---

## Checks when something is wrong

| symptom | check |
|---|---|
| Slack says "Your URL didn't respond" | `curl -s <URL>/` should print `judgment-pack slack demo: up`. If it does not, the revision failed to start — see the logs line below. |
| The app never answers a button | Interactivity URL (step 7.2) is not set, or the service is scaled to zero — it must be `--min-instances=1`, which the script sets. |
| "refusing to start without SLACK_SIGNING_SECRET" in the logs | The secret is missing or empty in Secret Manager. Add a version and roll a revision — see *Rotating a key* below; a new version alone changes nothing. |
| Every Slack request suddenly 401s after you rotated the signing secret | The running instance still holds the old value: secret references resolve at instance start. Roll a revision (below). |
| Narrations are missing but decisions still appear | Exactly the designed behavior: the model is unavailable or the per-user hourly budget is spent. The dispositions are unaffected — they never needed it. |
| "refusing to start … cannot reach Firestore collection" in the logs | The database or the IAM binding is missing. Re-run `deploy.sh` (both steps are idempotent), or set `STATE_BACKEND=memory` to run without one. The refusal is deliberate: a demo told to remember must not silently forget. |
| A user says the demo "started over" after a deploy | Expected, and it should have said so in one line. Use cases 1 and 2 resume; 3 and 4 restart, because a signing desk's receipts and a decision book cannot be rebuilt by a new container. |
| A use case shows a refusal in a code block | Read it. A refusal is an answer here, and it is reported verbatim on purpose. |

Logs:

```bash
gcloud run services logs tail judgment-pack-slack-demo --region us-central1
```

### Rotating a key later (requires a new revision)

Cloud Run resolves a `:latest` secret reference **when an instance starts**, so a new secret
version changes nothing until a revision rolls. Both commands, always together:

```bash
printf '%s' "<new value>" | gcloud secrets versions add GEMINI_API_KEY --data-file=-
gcloud run services update judgment-pack-slack-demo \
  --region us-central1 --revision-suffix="$(date -u +%Y%m%d-%H%M%S)"
```

Rotating `SLACK_SIGNING_SECRET` without the second command is the one that hurts: Slack signs with
the new secret, the running instance still checks against the old one, and every request 401s.

## Running it without Slack first

You do not need any of the above to see the whole thing work:

```bash
python3 -m pytest slack/
JPACK_BIN=$(which jpack) GATEWAY_BIN=$(which gateway) python3 slack/bot/dryrun.py --script
```

The dry run drives the same four flows on a terminal with a canned model, running the real
binaries. It says exactly what is missing if a path is not set.
