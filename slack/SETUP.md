# Setting it up

Two things need a human. Everything else is a tag and a click.

You need: a Slack workspace you can install apps into, a Google Cloud project with billing,
`gcloud`, and a Gemini API key.

---

## The order, in one line

**A project → `deploy.sh` once → `setup-wif.sh` → tag releases.**

That order is not a style preference. `deploy.sh` creates the three Secret Manager secrets, the
Artifact Registry repository and the Firestore database — the resources a release only *names* and
never creates — and it produces the first URL, which is what the Slack app needs before it can be
created at all. `setup-wif.sh` then builds the federation and **verifies those resources exist**,
refusing to hand back a green checkmark that leads to a failing first release.

## The two human moments

Neither can be automated, and both are quick. They are the only places a person is genuinely
required — the rest of this page is machinery that runs itself.

### 1. Mint a Slack configuration token (once, ever)

<https://api.slack.com/apps> → **Your App Configuration Tokens** (bottom of the page) → pick the
workspace → **Generate**. You get a pair: an access token (`xoxe.xoxp-…`) and a refresh token.

That pair is what lets `slack/deploy/slack-app.py` create the app and re-point its three URLs
without anybody pasting anything. Access tokens expire after twelve hours; the refresh token mints
new ones, and **each rotation replaces the refresh token too**, so whatever holds it has to be
updated. Keep both in a password manager, and put them in GitHub only if you want releases to wire
Slack for you.

With the tokens in hand — and a service already running, from `deploy.sh` — the app itself is one
command:

```bash
python3 slack/deploy/slack-app.py create \
  --url https://<your-service>.run.app \
  --signing-secret-out ./signing-secret \
  --refresh-out ./slack-refresh-token \
  --gcp-project <YOUR_PROJECT_ID>
```

`--refresh-out` is not optional decoration: if the access token has expired (twelve hours), the
command rotates, and Slack kills the old refresh token the instant it issues a new one. Without a
file to put it in, the tool now **refuses to rotate** rather than burning your credential. After a
run that rotated, copy `./slack-refresh-token` into your password manager (and into the GitHub
secret, if you set one) and delete the file.

It substitutes all three URLs into [`app_manifest.yml`](app_manifest.yml), creates the app, writes
the signing secret to a mode-600 file (never to your terminal, never to a log) and adds it to
Secret Manager. Slack fires a verification challenge at the URL during that call, so **the service
must already be running** — which is why the release workflow does this after the smoke test, and
why the manual path deploys first.

### 2. Install the app to the workspace (once per workspace)

api.slack.com/apps → your app → **Install App** → **Install to Workspace** → **Allow**.

Only a workspace admin can grant consent, and no API can grant it for them. What comes back is the
**Bot User OAuth Token** (`xoxb-…`) — copy it from **OAuth & Permissions** and put it in Secret
Manager as `SLACK_BOT_TOKEN`:

```bash
printf %s 'xoxb-…' | gcloud secrets versions add SLACK_BOT_TOKEN --data-file=- --project <PROJECT_ID>
```

(`deploy.sh` prompts for it with the echo off if the secret does not exist yet. A full OAuth
redirect flow could capture it instead of a copy-paste — it is the same token either way, and for
one workspace the copy is honest and shorter.)

Then roll a revision so the running instance picks the secret up — Cloud Run resolves `:latest`
at instance start, so a new version alone changes nothing:

```bash
gcloud run services update <SERVICE> --region <REGION> --revision-suffix="$(date -u +%Y%m%d-%H%M%S)"
```

---

## Wire GitHub releases

One bootstrap, then every deployment is `git tag` → your click → live.

### Bootstrap (once per project)

```bash
gcloud auth login
gcloud config set project <YOUR_PROJECT_ID>
./slack/deploy/deploy.sh        # first: secrets, registry, database, first revision
./slack/deploy/setup-wif.sh     # then: the federation
```

`setup-wif.sh` builds Workload Identity Federation so GitHub Actions can deploy **without a
service-account key**: a pool; an OIDC provider conditioned on this repository **and** the
`production` environment claim; three service accounts with disjoint jobs — a deployer that can
submit a build and roll a revision, a builder the build runs as, and a runtime the container runs
as (three secrets and Firestore, nothing else); the Artifact Registry repository; the Cloud Build
staging buckets; and the principalSet binding. It also **removes** the grants earlier versions of
these scripts gave the project's default compute account, and it verifies that the secrets and the
database exist — exiting non-zero with the exact command if they do not.

### GitHub → Settings → Environments → **production**

Three things, and the third is the one people skip:

1. Create the environment.
2. **Required reviewers**: add yourself. That is the click.
3. **Deployment branches and tags** → *Selected branches and tags* → add a **tag** rule:
   `slack-v*`.

Why the third matters: the federation trusts a token that carries
`environment=production`, and GitHub only puts that claim in a token for a job that cleared this
environment's rules. Without the tag rule, any ref could reach the environment and ask for the
click; with it, only a release tag can. **The approval is enforced by Google's side of the trust,
not by the workflow file** — which anyone with push access could otherwise edit.

**Variables** (none is a credential; a workflow log shows them anyway):

| variable | value | required |
|---|---|---|
| `WIF_PROVIDER` | `projects/<number>/locations/global/workloadIdentityPools/github/providers/github-oidc` | yes |
| `DEPLOY_SA` | `jpack-slack-deployer@<project>.iam.gserviceaccount.com` | yes |
| `RUNTIME_SA` | `jpack-slack-runtime@<project>.iam.gserviceaccount.com` — what the container runs as | yes |
| `BUILD_SA` | `jpack-slack-builder@<project>.iam.gserviceaccount.com` — what the build runs as | yes |
| `PROJECT_ID` | your GCP project id | yes |
| `REGION` | `us-central1` | no (default `us-central1`) |
| `CLOUDBUILD_REGION` | where Cloud Build runs | no (defaults to `REGION`) |
| `SERVICE` | `judgment-pack-slack-demo` | no |
| `AR_REPO` | `judgment-pack` | no |
| `SLACK_APP_ID` | `A0123456789` — set it and releases re-point the app's URLs | no |
| `STATE_BACKEND`, `FIRESTORE_COLLECTION`, `FIRESTORE_DATABASE`, `SESSION_TTL_SECONDS` | session state | no |
| `MIN_INSTANCES`, `MAX_INSTANCES`, `CONCURRENCY`, `CPU`, `MEMORY`, `TIMEOUT`, `GEMINI_MODEL`, `DEMO_PROJECT`, `SESSION_ROOT`, `GATEWAY_AUTHORITY` | the service shape | no — defaults match `deploy.sh` |

**Repository secrets** — Settings → Secrets and variables → **Actions** (not the environment; see
below). All three optional:

| secret | what it is |
|---|---|
| `SLACK_CONFIG_TOKEN` | the access half of the configuration token pair |
| `SLACK_CONFIG_REFRESH_TOKEN` | the refresh half — a rotation replaces it |
| `GH_ADMIN_TOKEN` | a token with `secrets: write` on this repository. Without it a release will **not** rotate the Slack pair, because a rotation it cannot store would kill your credential; the wiring is skipped with instructions instead |

They are **repository** secrets on purpose. An environment secret can only be read by a job that
declares that environment, and any job declaring `production` would stop for its own approval —
so the wiring job would cost a second click on every release. The trade, stated so it is a choice:
repository secrets are readable by any workflow in this repository. For a demo's
app-configuration tokens that is acceptable; for the three runtime secrets it would not be, which
is why those live in **Secret Manager** and the workflow only names them.

The Slack-wiring job is skipped entirely — queueing, gate and all — unless the `SLACK_APP_ID`
variable is set. No app id, nothing to re-point, no job.

### Release

```bash
git tag slack-v0.3.0
git push origin slack-v0.3.0
```

Then, in order:

1. **feedback** — the unit suite and a local build of the image with the demo run inside it. Fast,
   and honestly labelled: this image is thrown away.
2. **the gate** — GitHub waits for your click. Until it comes, no OIDC token carries the
   `environment` claim, so no Google credential exists for this run at all.
3. **build and prove** — Cloud Build builds regionally, then runs the whole demo *inside the
   image it just built*. Only if that passes is the image pushed, so what lands in the registry
   has already run four use cases against the real binaries. The **digest** is what gets deployed.
4. **dark deploy** — the revision starts with **no traffic**, under a `candidate` tag.
5. **prove the candidate** — its own URL must answer the banner, report `state=… ok` rather than
   `DEGRADED` on `/healthz`, and refuse an unsigned `POST /slack/events` with 401.
6. **promote** — only now does traffic move. A failure at any earlier point leaves the previous
   revision serving every Slack request.
7. **confirm and wire** — the live URL is re-checked from outside, and if `SLACK_APP_ID` is set the
   app's URLs are re-pointed.

---

## Appendix: the manual path

Everything above automates this. It still works, and it is what to fall back to when GitHub is not
in the picture.

<details>
<summary>Eight steps, about fifteen minutes</summary>

### 1. Create the Slack app from the manifest

<https://api.slack.com/apps> → **Create New App** → **From a manifest** → pick your workspace →
paste the whole of [`app_manifest.yml`](app_manifest.yml) → **Create**.

Slack **challenges the Event Subscriptions request URL** the moment you save, and nothing answers
at `example.invalid` yet, so expect it to refuse. The recovery, in order:

1. Delete the single line marked `DELETE-IF-REFUSED` in the manifest — it is
   `request_url: https://example.invalid/slack/events` under `settings.event_subscriptions` — and
   paste again. The `bot_events` list stays.
2. If it still refuses, delete the whole `event_subscriptions:` block (four more lines) and paste
   again. You will re-add the URL *and* the three bot events in step 7.1.

(`slack-app.py create` avoids all of this by creating the app after the service is live.)

### 2. Install it and copy the bot token

**Install App** → **Install to Workspace** → **Allow**. Copy the **Bot User OAuth Token**
(`xoxb-…`); you paste it in step 6.

### 3. Copy the signing secret

**Basic Information** → **App Credentials** → **Signing Secret** → **Show** → copy. This is what
proves a request came from Slack; the app refuses to start without it.

### 4. Get a Gemini API key

<https://aistudio.google.com/apikey>. This key buys narration and drafting only: every disposition
comes from the `jpack` binary in the container, with no key and no network.

### 5. Point gcloud at your project

```bash
gcloud auth login          # required: an expired token cannot be refreshed by a script
gcloud config set project <YOUR_PROJECT_ID>
cp slack/deploy/deploy.env.example slack/deploy/deploy.env
```

Every setting lives in that one gitignored file; an exported variable still overrides it.

**Prefer a project of its own?** `./slack/deploy/new-project.sh` creates it, attaches billing,
records the id, and hands off to `deploy.sh` — so skip step 6.

### 6. Deploy

```bash
./slack/deploy/deploy.sh
```

It enables the five APIs (Cloud Run, Cloud Build, Secret Manager, Artifact Registry, Firestore),
creates the Artifact Registry repository (**retrying for a minute at a time** — a freshly enabled
API can refuse the first create while it provisions), asks for the three secrets one at a time with
input hidden, grants the build's own identity the three roles a new project's compute account
lacks, sets up Firestore and its TTL policies, **builds regionally** (the global Cloud Build pool
can queue a build indefinitely with no diagnostic), deploys, and then **verifies the service is
actually reachable** — see the org-policy note below.

| prompt | what to paste |
|---|---|
| `SLACK_BOT_TOKEN` | the `xoxb-…` token from step 2 |
| `SLACK_SIGNING_SECRET` | the signing secret from step 3 |
| `GEMINI_API_KEY` | the key from step 4 |

A secret already in Secret Manager is left alone, so re-running is safe. If you cannot type at the
prompt, point `<NAME>_FILE` at a file holding the value — a file, not an environment variable:
`ps` and `/proc/<pid>/environ` show the second one to every process on the machine.

The backend writes **three** Firestore collections: `slack-demo-sessions`, `-events` (Slack event
ids, so a retry is recognized after a restart) and `-limits` (per-user budgets). Each needs its own
TTL policy — policies are per collection group, and `deploy.sh` enables all three. While an instance
is running the app deletes its own expired session documents; the policy is what deletes them when
nothing is running, and it is the **only** thing that ever deletes the other two.

```bash
for g in slack-demo-sessions slack-demo-sessions-events slack-demo-sessions-limits; do
  gcloud firestore fields ttls list --collection-group="$g" --project <YOUR_PROJECT_ID>
done
```

`STATE_BACKEND=memory` in `deploy.env` runs without a database at all.

**What it costs:** `--min-instances=1 --no-cpu-throttling` bills one 1-vCPU/512Mi instance
continuously — tens of dollars a month at list price, inside a credits budget and *not* inside the
free tier. Both flags are load-bearing: the app answers Slack in milliseconds and does the real
work after that 200, and each session's screening desk is a long-lived process. To pause without
deleting anything: `gcloud run services update <SERVICE> --region <REGION> --min-instances=0`.

### 7. Paste the URL into three fields

The **same URL** in all three places (or run `slack-app.py update-url --app <id> --url <url>`):

1. **Event Subscriptions** → **Request URL** → paste → **Verified ✓**, then confirm the three bot
   events: `team_join`, `app_home_opened`, `message.im`. **Save Changes**.
2. **Interactivity & Shortcuts** → **Request URL** → paste → **Save Changes**.
3. **Slash Commands** → `/jpack` → **Request URL** → paste → **Save**.

### 8. Say hello

Slack → **Judgment Pack Demo** under **Apps**. The **Home** tab shows the menu and your progress;
the **Messages** tab gets the welcome. Try `/jpack`, and DM the bot `menu`.

</details>

---

## Checks when something is wrong

| symptom | check |
|---|---|
| Slack says "Your URL didn't respond" | `curl -s <URL>/` should print `judgment-pack slack demo: up`. If curl gets a **403 from Google's front end** rather than an answer from the app, the public invoker binding is missing — see the org-policy row below. |
| `deploy.sh` exits with "deployed, but unreachable" | An org policy (`iam.allowedPolicyMemberDomains`) refused the `allUsers` invoker binding. `--allow-unauthenticated` reports success anyway, which is why this is checked rather than assumed. The script prints the override policy, the re-bind command, and the propagation note; apply it, wait a minute or two, and re-run. |
| A Cloud Build sits in QUEUED forever | The global pool. `deploy.sh` and the workflow both build regionally (`CLOUDBUILD_REGION`); if you overrode it back to global, that is the cause. |
| The first build on a new project fails reading its own source | The compute service account has no roles on a fresh project. `deploy.sh` and `setup-wif.sh` both grant `storage.objectViewer`, `logging.logWriter` and `artifactregistry.writer`; re-run either. |
| `repositories create` refused right after enabling the API | Provisioning lag. `deploy.sh` retries three times, sixty seconds apart, and says so. |
| The app never answers a button | The Interactivity URL is not set, or the service is scaled to zero — it must be `--min-instances=1`. |
| "refusing to start without SLACK_SIGNING_SECRET" | The secret is missing or empty. Add a version **and roll a revision** — see *Rotating a key*. |
| Every Slack request 401s after you rotated the signing secret | The instance still holds the old value: secret references resolve at instance start. Roll a revision. |
| Narrations missing, decisions still appearing | By design: the model is unavailable or the hourly budget is spent. Dispositions never needed it. |
| "cannot use Firestore collection" in the logs | The database is missing, or the service account lacks `roles/datastore.user` (a read-only binding fails on purpose — the probe writes as well as reads). If it names a Datastore-mode database, that mode cannot be changed: create a named Native one and set `FIRESTORE_DATABASE`, or set `STATE_BACKEND=memory`. |
| "I cannot reach the place your progress is kept" | Firestore reads are failing. The turn is served and **nothing is written** for that user until a read succeeds. `curl -s <URL>/` reports `state=firestore DEGRADED`. |
| A user says the demo "started over" after a deploy | Expected, and said in one line. Use cases 1 and 2 resume; 3 and 4 restart, because a signing desk's receipts and a decision book cannot be rebuilt by a new container. |
| The release workflow fails at "the production environment is missing…" | A variable was not set. The list is in *Wire GitHub releases*; `setup-wif.sh` prints the values. |
| `google-github-actions/auth` fails with a permission error | Usually the environment claim: check the provider condition includes `assertion.environment=='production'` **and** that the job declaring the environment is the one authenticating. Re-running `setup-wif.sh` re-applies both the mapping and the condition. |
| `setup-wif.sh` exits "bootstrap incomplete" | It found the federation fine and a missing secret or database — the things a release names but never creates. Run `./slack/deploy/deploy.sh` once, then re-run it. |
| The Slack wiring job warns that the config token is expired | It refused to rotate because nothing in that run could store the new pair. Either set `GH_ADMIN_TOKEN`, or rotate locally with `slack-app.py rotate --refresh-out … --token-out …` and update both secrets. |
| A use case shows a refusal in a code block | Read it. A refusal is an answer here, and it is reported verbatim on purpose. |

Logs:

```bash
gcloud run services logs tail judgment-pack-slack-demo --region us-central1
```

### Rotating a key later (requires a new revision)

```bash
printf '%s' "<new value>" | gcloud secrets versions add GEMINI_API_KEY --data-file=-
gcloud run services update judgment-pack-slack-demo \
  --region us-central1 --revision-suffix="$(date -u +%Y%m%d-%H%M%S)"
```

Rotating `SLACK_SIGNING_SECRET` without the second command is the one that hurts: Slack signs with
the new secret, the running instance checks against the old one, and every request 401s.

## Running it without Slack first

You do not need any of the above to see the whole thing work:

```bash
python3 -m pytest slack/
JPACK_BIN=../judgment-pack-runtime/jpack \
GATEWAY_BIN=../judgment-pack-gateway/go/gateway \
  python3 slack/bot/dryrun.py --script
```

Give both variables the **path to the binary you built**. `$(which jpack)` works when the binaries
are on your PATH under exactly those names; a build in a sibling checkout is not, and `$(which …)`
then expands to nothing, which falls back to the bare name and reports "GATEWAY_BIN is not
runnable: gateway" — the one path you did not mean.

The dry run drives the same four flows on a terminal with a canned model, running the real
binaries, with the memory state backend (no database, no credentials). The release workflow runs
this same command inside the built image, before anybody is asked to approve a deployment.
