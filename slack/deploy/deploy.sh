#!/usr/bin/env bash
# Build and deploy the Slack self-serve demo to Cloud Run.
#
#   ./slack/deploy/deploy.sh
#
# What it does, in order: enables the five APIs it needs, makes an Artifact
# Registry repository if there is none (retrying, because a freshly enabled API
# can refuse the first create while it provisions), creates the three secrets in
# Secret Manager if they do not exist (reading them from your terminal WITHOUT
# echoing, never from a file it leaves behind), grants the runtime account read
# access to them AND the three roles the BUILD's identity needs, sets up
# Firestore, builds the image with Cloud Build IN A REGION (the global pool can
# queue a build indefinitely with no diagnostic), deploys one Cloud Run revision
# pinned to a single instance, VERIFIES the service is actually reachable, and
# prints the URL to paste into the Slack app.
#
# Re-running is safe: everything is idempotent, and existing secrets are left
# alone (rotate with `gcloud secrets versions add`).
#
# For an automated release instead — tag, approve, live — see
# .github/workflows/release-slack.yml and slack/deploy/setup-wif.sh. This
# script stays the manual path, and the two share every flag that matters.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${here}/../.." && pwd)"   # the repository root: the build context

# One place for every deployment setting: slack/deploy/deploy.env, copied from
# the committed .example and gitignored. Its lines are `NAME="${NAME:-value}"`,
# so an exported variable still wins over the file, and the file wins over the
# defaults below.
DEPLOY_ENV="${DEPLOY_ENV:-${here}/deploy.env}"
# shellcheck disable=SC1090
[ -f "${DEPLOY_ENV}" ] && . "${DEPLOY_ENV}"

SERVICE="${SERVICE:-judgment-pack-slack-demo}"
REGION="${REGION:-us-central1}"
# Cloud Build's GLOBAL pool is shared, and a busy one can leave a build QUEUED
# with no diagnostic and no timeout — observed twice on this project, both
# times cleared by naming a region, where the same build starts in seconds.
# Regional by default, therefore, and overridable for a project whose region
# has no Cloud Build.
CLOUDBUILD_REGION="${CLOUDBUILD_REGION:-${REGION}}"
AR_REPO="${AR_REPO:-judgment-pack}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.6-flash}"
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
MIN_INSTANCES="${MIN_INSTANCES:-1}"
MAX_INSTANCES="${MAX_INSTANCES:-1}"
CONCURRENCY="${CONCURRENCY:-20}"
CPU="${CPU:-1}"
MEMORY="${MEMORY:-512Mi}"
TIMEOUT="${TIMEOUT:-300}"
DEMO_PROJECT="${DEMO_PROJECT:-/opt/judgment-pack/enterprise-demo}"
SESSION_ROOT="${SESSION_ROOT:-/tmp}"
GATEWAY_AUTHORITY="${GATEWAY_AUTHORITY:-gateway:judgment-pack-slack}"
# Session metadata in Firestore: a user's progress survives a restart, a
# redeploy, or this instance being replaced. It does NOT make the demo
# multi-instance — the scratch project and the signing desk are local to a
# container by nature — so MIN/MAX_INSTANCES stay at 1 (slack/DESIGN.md).
STATE_BACKEND="${STATE_BACKEND:-firestore}"
FIRESTORE_COLLECTION="${FIRESTORE_COLLECTION:-slack-demo-sessions}"
SESSION_TTL_SECONDS="${SESSION_TTL_SECONDS:-7200}"
# Empty means the project's (default) database, which is what deploy.sh
# creates. Set FIRESTORE_DATABASE when the default one is Datastore mode.
FIRESTORE_DATABASE="${FIRESTORE_DATABASE:-}"
# Three identities, three jobs — never the project's default compute account,
# which usually carries roles/editor and would then be what an internet-facing
# container runs as. setup-wif.sh creates all three for the CI path; this
# script creates whichever are missing for the manual one.
RUNTIME_SA_NAME="${RUNTIME_SA_NAME:-jpack-slack-runtime}"
BUILD_SA_NAME="${BUILD_SA_NAME:-jpack-slack-builder}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# One scratch file, made unpredictably and removed on every exit path. The
# predictable /tmp/<name>.$$ this replaces was a symlink waiting to happen on
# a shared host, and needed an rm on each of six branches.
scratch="$(mktemp)"
trap 'rm -f "${scratch}"' EXIT

command -v gcloud >/dev/null || die "gcloud is not installed — https://cloud.google.com/sdk/docs/install"
# Checked once, here: every gcloud call below needs a live token, and an
# expired one cannot be refreshed without a prompt. Failing at the door beats
# failing six API calls in with a stack of half-made resources behind you.
gcloud auth print-access-token >/dev/null 2>&1 \
  || die "gcloud has no usable credentials — run: gcloud auth login"
[ -n "${PROJECT_ID}" ] || die "no project set: gcloud config set project <PROJECT_ID> (or export PROJECT_ID)"
[ -f "${root}/slack/Dockerfile" ] || die "cannot find slack/Dockerfile under ${root}"

say "Project ${PROJECT_ID}, region ${REGION}, service ${SERVICE}"

say "1/7 Enabling the APIs this needs (idempotent)"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  --project "${PROJECT_ID}" --quiet

say "2/7 Artifact Registry repository"
if gcloud artifacts repositories describe "${AR_REPO}" \
      --location "${REGION}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "  ${AR_REPO} exists"
else
  # Enabling an API and using it are not the same instant. On a fresh project
  # the first `repositories create` can be refused for minutes while Artifact
  # Registry finishes provisioning — a real failure that fixes itself. Three
  # tries, a minute apart, then give up with the reason rather than leaving
  # somebody to guess whether their gcloud is broken.
  created=""
  for attempt in 1 2 3; do
    if gcloud artifacts repositories create "${AR_REPO}" \
         --repository-format=docker --location="${REGION}" \
         --description="judgment-pack demo images" \
         --project "${PROJECT_ID}" --quiet 2>"${scratch}"; then
      created="yes"
      echo "  created ${AR_REPO}"
      break
    fi
    if grep -qiE "already exists|ALREADY_EXISTS" "${scratch}"; then
      created="yes"
      echo "  ${AR_REPO} exists"
      break
    fi
    if [ "${attempt}" -lt 3 ]; then
      echo "  attempt ${attempt} refused — Artifact Registry is usually still"
      echo "  provisioning right after the API is enabled; retrying in 60s"
      sed 's/^/    /' "${scratch}" >&2 || true
      sleep 60
    fi
  done
  if [ -z "${created}" ]; then
    cat "${scratch}" >&2
    die "could not create the Artifact Registry repository ${AR_REPO} in ${REGION}
  after three attempts. If the API was just enabled, wait a few minutes and re-run."
  fi
fi

# --- secrets ---------------------------------------------------------------
# Read once, from the terminal, with echo off. Never printed, never written to
# a file, never passed on a command line where `ps` could see it.
ensure_secret() {
  local name="$1" prompt="$2"
  if gcloud secrets describe "${name}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    echo "  ${name}: already in Secret Manager (leaving it alone)"
    return
  fi
  local value="" from_file="${name}_FILE"
  from_file="${!from_file:-}"
  if [ -n "${from_file}" ]; then
    # A file, never an environment variable holding the value: `ps` and
    # /proc/<pid>/environ show the second to every process on the box.
    [ -r "${from_file}" ] || die "${name}_FILE=${from_file} is not readable"
    value="$(tr -d '\r\n' < "${from_file}")"
    echo "  ${name}: read from ${from_file}"
  elif [ -t 0 ]; then
    echo
    printf '  %s\n  paste %s (input hidden): ' "${prompt}" "${name}"
    read -r -s value
    echo
  else
    die "${name} is not in Secret Manager and there is no terminal to ask.
  Either run this from a terminal, or point ${name}_FILE at a file holding it:
    printf %s '<the value>' > /tmp/${name} && chmod 600 /tmp/${name}
    ${name}_FILE=/tmp/${name} ./slack/deploy/deploy.sh && shred -u /tmp/${name}"
  fi
  [ -n "${value}" ] || die "${name} cannot be empty"
  gcloud secrets create "${name}" --replication-policy=automatic \
    --project "${PROJECT_ID}" --quiet >/dev/null
  printf '%s' "${value}" | gcloud secrets versions add "${name}" \
    --data-file=- --project "${PROJECT_ID}" --quiet >/dev/null
  unset value
  echo "  ${name}: created"
}

say "3/7 Secrets"
ensure_secret SLACK_BOT_TOKEN     "Slack app → OAuth & Permissions → Bot User OAuth Token (xoxb-…)"
ensure_secret SLACK_SIGNING_SECRET "Slack app → Basic Information → Signing Secret"
ensure_secret GEMINI_API_KEY      "Google AI Studio → API key (this powers narration and drafting only)"

project_number="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
compute_sa="${project_number}-compute@developer.gserviceaccount.com"

# Two robots, two jobs, and NEITHER is the project's default compute account —
# which usually carries roles/editor, and which an unconfigured Cloud Run
# service would otherwise run as. An internet-facing container holding
# registry write could poison the very image it is deployed from.
ensure_sa() {
  local name="$1" description="$2"
  local email="${name}@${PROJECT_ID}.iam.gserviceaccount.com"
  gcloud iam service-accounts describe "${email}" --project "${PROJECT_ID}" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "${name}" --display-name="${description}" \
         --project "${PROJECT_ID}" --quiet
  printf '%s' "${email}"
}
runtime_sa="${RUNTIME_SA:-$(ensure_sa "${RUNTIME_SA_NAME}" "Slack demo — Cloud Run runtime identity")}"
build_sa="${BUILD_SA:-$(ensure_sa "${BUILD_SA_NAME}" "Slack demo — Cloud Build identity")}"
echo "  runtime: ${runtime_sa}"
echo "  build:   ${build_sa}"

# RUNTIME: the three secrets, and (below) Firestore. Nothing else.
for secret in SLACK_BOT_TOKEN SLACK_SIGNING_SECRET GEMINI_API_KEY; do
  gcloud secrets add-iam-policy-binding "${secret}" \
    --member="serviceAccount:${runtime_sa}" \
    --role=roles/secretmanager.secretAccessor \
    --project "${PROJECT_ID}" --quiet >/dev/null
done
echo "  ${runtime_sa} may read the three secrets"

# BUILD: write logs, read the staged source, push to THIS repository only. On
# a new project a build identity holds nothing at all, and the first build
# fails while reading the source it was just handed — which reads like a
# broken script and is really three missing grants.
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${build_sa}" --role=roles/logging.logWriter \
  --condition=None --quiet >/dev/null
gcloud artifacts repositories add-iam-policy-binding "${AR_REPO}" \
  --location="${REGION}" --member="serviceAccount:${build_sa}" \
  --role=roles/artifactregistry.writer --project "${PROJECT_ID}" --quiet >/dev/null
for bucket in "gs://${PROJECT_ID}_cloudbuild" "gs://${PROJECT_ID}_${CLOUDBUILD_REGION}_cloudbuild"; do
  gcloud storage buckets add-iam-policy-binding "${bucket}" \
    --member="serviceAccount:${build_sa}" --role=roles/storage.objectViewer \
    --project "${PROJECT_ID}" --quiet >/dev/null 2>&1 || true
done
echo "  ${build_sa} may write build logs, read staged source, and push to ${AR_REPO}"

# And take back what older versions of this script gave the compute default.
for role in roles/storage.objectViewer roles/logging.logWriter roles/artifactregistry.writer; do
  gcloud projects remove-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${compute_sa}" --role="${role}" \
    --condition=None --quiet >/dev/null 2>&1 \
    && echo "  removed ${role} from ${compute_sa} (it has no job here)"
done

# --- session state ---------------------------------------------------------
# Firestore holds session METADATA — where a user is in the demo — so a
# restart does not drop somebody mid-run. Their scratch project and their
# screening desk are rebuilt by the next container instead; what cannot be
# rebuilt (a desk's receipts, a decision book) is reported to them plainly
# rather than pretended (see reconciliation in slack/DESIGN.md).
if [ "${STATE_BACKEND}" = "firestore" ]; then
  say "4/7 Firestore (Native mode) for session state"
  existing_type="$(gcloud firestore databases describe --project "${PROJECT_ID}" \
                     --format='value(type)' 2>/dev/null || true)"
  if [ -n "${existing_type}" ]; then
    # A project gets ONE default database and its mode is permanent. A
    # Datastore-mode one cannot serve the Native client, and "it exists" would
    # be a deploy that succeeds and a service that dies at boot.
    case "${existing_type}" in
      FIRESTORE_NATIVE)
        echo "  database exists (Native mode)" ;;
      *)
        die "this project's default Firestore database is ${existing_type}, and the
  app needs Native mode. The default database's mode cannot be changed, so either:
    • create a named Native database and point the app at it:
        gcloud firestore databases create --database=slack-demo \\
          --location=${REGION} --type=firestore-native --project ${PROJECT_ID}
      then set FIRESTORE_DATABASE=\"slack-demo\" in slack/deploy/deploy.env; or
    • set STATE_BACKEND=memory in slack/deploy/deploy.env to run without one." ;;
    esac
  else
    # A project gets one default database, and creating a second time is an
    # error rather than a no-op: tolerate exactly that.
    if gcloud firestore databases create --location="${REGION}" \
         --type=firestore-native --project "${PROJECT_ID}" --quiet 2>"${scratch}"; then
      echo "  created a Native-mode database in ${REGION}"
    else
      if grep -qiE "already exists|ALREADY_EXISTS" "${scratch}"; then
        echo "  database already exists"
      else
        cat "${scratch}" >&2
        die "could not create the Firestore database"
      fi
    fi
  fi

  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${runtime_sa}" \
    --role=roles/datastore.user --quiet >/dev/null
  echo "  ${runtime_sa} may read and write session documents"

  # THREE collections, not one: sessions, the de-duplication records the
  # Events API needs, and the rate-limit buckets. Each writes an expires_at
  # for a policy that has to exist per collection group, and only the sessions
  # one is also swept by the running app — the other two have no deleter at
  # all without this.
  ttl_missing=""
  for group in "${FIRESTORE_COLLECTION}" \
               "${FIRESTORE_COLLECTION}-events" \
               "${FIRESTORE_COLLECTION}-limits"; do
    if gcloud firestore fields ttls update expires_at \
         --collection-group="${group}" --enable-ttl \
         --project "${PROJECT_ID}" --quiet >/dev/null 2>&1; then
      echo "  TTL policy on expires_at enabled for ${group}"
    else
      ttl_missing="${ttl_missing} ${group}"
    fi
  done
  if [ -n "${ttl_missing}" ]; then
    echo "  NOTE: could not set the TTL policy for:${ttl_missing}"
    echo "  Run this once per collection group (idempotent):"
    for group in ${ttl_missing}; do
      echo "    gcloud firestore fields ttls update expires_at \\"
      echo "      --collection-group=${group} --enable-ttl --project ${PROJECT_ID}"
    done
    echo "  The running app deletes expired SESSION documents itself; the policy is"
    echo "  what deletes them while no instance is running, and it is the only thing"
    echo "  that ever deletes the -events and -limits documents."
  fi
else
  say "4/7 Session state: ${STATE_BACKEND} (in this instance's memory only)"
  echo "  a restart or a redeploy drops every session mid-run — see slack/DESIGN.md"
fi

# --- build -----------------------------------------------------------------
TAG="$(date -u +%Y%m%d-%H%M%S)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE}:${TAG}"

say "5/7 Building ${IMAGE} in ${CLOUDBUILD_REGION} (context: ${root})"
# The build runs as its OWN identity, and cloudbuild.yaml proves the image by
# running the whole demo inside it before `images:` is pushed — so a build
# whose demo fails publishes nothing.
gcloud builds submit "${root}" \
  --region "${CLOUDBUILD_REGION}" \
  --config "${here}/cloudbuild.yaml" \
  --substitutions "_IMAGE=${IMAGE}" \
  --service-account "projects/${PROJECT_ID}/serviceAccounts/${build_sa}" \
  --project "${PROJECT_ID}"

# --- deploy ----------------------------------------------------------------
# Two flags are load-bearing, not thrift:
#
#   min=max=1        sessions, their scratch copies of the demo project, and
#                    their screening desks live in one process's memory and
#                    filesystem (the state limitation in slack/DESIGN.md).
#   --no-cpu-throttling
#                    this app answers Slack in milliseconds and does the real
#                    work AFTER the 200 — evaluations, gateway calls, model
#                    calls — and each session's gateway is a long-lived
#                    process. Cloud Run's default allocates CPU only during a
#                    request, which would throttle exactly the work that
#                    matters. It costs always-allocated CPU on one instance;
#                    that is the price of the architecture, stated in SETUP.md.
say "6/7 Deploying to Cloud Run (one instance, CPU always allocated)"
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --platform managed \
  --service-account "${runtime_sa}" \
  --allow-unauthenticated \
  --min-instances="${MIN_INSTANCES}" \
  --max-instances="${MAX_INSTANCES}" \
  --no-cpu-throttling \
  --concurrency="${CONCURRENCY}" \
  --cpu="${CPU}" \
  --memory="${MEMORY}" \
  --timeout="${TIMEOUT}" \
  --set-secrets "SLACK_BOT_TOKEN=SLACK_BOT_TOKEN:latest,SLACK_SIGNING_SECRET=SLACK_SIGNING_SECRET:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest" \
  --set-env-vars "GEMINI_MODEL=${GEMINI_MODEL},DEMO_PROJECT=${DEMO_PROJECT},SESSION_ROOT=${SESSION_ROOT},GATEWAY_AUTHORITY=${GATEWAY_AUTHORITY},STATE_BACKEND=${STATE_BACKEND},FIRESTORE_COLLECTION=${FIRESTORE_COLLECTION},SESSION_TTL_SECONDS=${SESSION_TTL_SECONDS},FIRESTORE_DATABASE=${FIRESTORE_DATABASE}" \
  --project "${PROJECT_ID}" --quiet

URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" \
        --project "${PROJECT_ID}" --format='value(status.url)')"

# --- is it actually reachable? ---------------------------------------------
# `--allow-unauthenticated` asks for an allUsers invoker binding. An
# organization policy (iam.allowedPolicyMemberDomains) can refuse that binding
# while the deploy still reports success — and then Google's front end answers
# every Slack request with a 403 before the container sees it. Observed live:
# a green deploy and an unreachable service. So the binding is CHECKED, and a
# missing one is a failure with the remedy, not a URL nobody can call.
#
# The check asks for the ROLE and the MEMBER together. Grepping the policy for
# "allUsers" alone passes on any allUsers binding at all — roles/run.viewer,
# say — and would report a service reachable while every request still 403s.
invoker_members="$(gcloud run services get-iam-policy "${SERVICE}" \
  --region "${REGION}" --project "${PROJECT_ID}" \
  --flatten='bindings[].members' \
  --filter='bindings.role=roles/run.invoker AND bindings.members=allUsers' \
  --format='value(bindings.members)' 2>"${scratch}")" || {
    cat "${scratch}" >&2
    die "could not read the IAM policy of ${SERVICE} — that is a permissions or API
  problem, NOT the org policy below; fix it before reading further."
  }

if [ -z "${invoker_members}" ]; then
  echo "  public invoker binding missing — trying to add it"
  if ! gcloud run services add-iam-policy-binding "${SERVICE}" \
        --region "${REGION}" --member=allUsers --role=roles/run.invoker \
        --project "${PROJECT_ID}" --quiet >/dev/null 2>"${scratch}"; then
    printf '\n%s\n' "$(cat "${scratch}")" >&2
    # Printed at column 0, deliberately: the heredoc inside this remedy needs
    # its terminator unindented, and an indented copy silently swallows both
    # gcloud commands into the YAML file.
    cat >&2 <<REMEDY

The service deployed, and Slack cannot reach it: without an allUsers invoker
binding every request is refused by Google's front end with a 403, before the
container is involved. The usual cause is the organization policy
constraints/iam.allowedPolicyMemberDomains.

Override it for THIS project, then re-bind — copy from here to the blank line:

cat > /tmp/allow-all.yaml <<'YAML'
constraint: constraints/iam.allowedPolicyMemberDomains
listPolicy:
  allValues: ALLOW
YAML
gcloud resource-manager org-policies set-policy /tmp/allow-all.yaml --project ${PROJECT_ID}
sleep 120   # policy changes take a minute or two to propagate
gcloud run services add-iam-policy-binding ${SERVICE} --region ${REGION} \
  --member=allUsers --role=roles/run.invoker --project ${PROJECT_ID}

Or simply re-run this script once the policy has propagated; everything before
this point is idempotent.

REMEDY
    die "deployed, but unreachable — see the remedy above (exiting non-zero on
  purpose: a URL nobody can call is not a successful deployment)"
  fi
  echo "  public invoker binding added"
else
  echo "  public invoker binding present"
fi

say "7/7 Done. Paste this ONE url into three fields of the Slack app:"
cat <<EOF

    ${URL}/slack/events

  api.slack.com/apps → your app →
    • Event Subscriptions      → Request URL        (it must verify: "Verified ✓")
    • Interactivity & Shortcuts → Request URL
    • Slash Commands → /jpack   → Request URL

  Health check (should say "up"):   curl -s ${URL}/
  Logs:  gcloud run services logs tail ${SERVICE} --region ${REGION} --project ${PROJECT_ID}

  Session state: ${STATE_BACKEND}$([ "${STATE_BACKEND}" = "firestore" ] && printf ' (collections %s, %s-events, %s-limits)' "${FIRESTORE_COLLECTION}" "${FIRESTORE_COLLECTION}" "${FIRESTORE_COLLECTION}")
  Confirm the three TTL policies that delete abandoned documents are on:
    for g in ${FIRESTORE_COLLECTION} ${FIRESTORE_COLLECTION}-events ${FIRESTORE_COLLECTION}-limits; do
      gcloud firestore fields ttls list --collection-group="\$g" --project ${PROJECT_ID}
    done

  The service is public on purpose — Slack posts to it from the internet, and
  every request is authenticated by its signature against the signing secret
  before any handler sees it.

EOF
