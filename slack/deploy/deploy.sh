#!/usr/bin/env bash
# Build and deploy the Slack self-serve demo to Cloud Run.
#
#   ./slack/deploy/deploy.sh
#
# What it does, in order: enables the four APIs it needs, makes an Artifact
# Registry repository if there is none, creates the three secrets in Secret
# Manager if they do not exist (reading them from your terminal WITHOUT
# echoing, never from a file it leaves behind), grants the service account
# read access to them, builds the image with Cloud Build from the repository
# root, deploys one Cloud Run revision pinned to a single instance, and prints
# the URL to paste into the Slack app.
#
# Re-running is safe: everything is idempotent, and existing secrets are left
# alone (rotate with `gcloud secrets versions add`).
set -euo pipefail

SERVICE="${SERVICE:-judgment-pack-slack-demo}"
REGION="${REGION:-us-central1}"
AR_REPO="${AR_REPO:-judgment-pack}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.1-pro-preview}"
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "${here}/../.." && pwd)"   # the repository root: the build context

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

command -v gcloud >/dev/null || die "gcloud is not installed — https://cloud.google.com/sdk/docs/install"
# Checked once, here: every gcloud call below needs a live token, and an
# expired one cannot be refreshed without a prompt. Failing at the door beats
# failing six API calls in with a stack of half-made resources behind you.
gcloud auth print-access-token >/dev/null 2>&1 \
  || die "gcloud has no usable credentials — run: gcloud auth login"
[ -n "${PROJECT_ID}" ] || die "no project set: gcloud config set project <PROJECT_ID> (or export PROJECT_ID)"
[ -f "${root}/slack/Dockerfile" ] || die "cannot find slack/Dockerfile under ${root}"

say "Project ${PROJECT_ID}, region ${REGION}, service ${SERVICE}"

say "1/6 Enabling the APIs this needs (idempotent)"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  --project "${PROJECT_ID}" --quiet

say "2/6 Artifact Registry repository"
if ! gcloud artifacts repositories describe "${AR_REPO}" \
      --location "${REGION}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${AR_REPO}" \
    --repository-format=docker --location="${REGION}" \
    --description="judgment-pack demo images" \
    --project "${PROJECT_ID}" --quiet
  echo "created ${AR_REPO}"
else
  echo "${AR_REPO} exists"
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

say "3/6 Secrets"
ensure_secret SLACK_BOT_TOKEN     "Slack app → OAuth & Permissions → Bot User OAuth Token (xoxb-…)"
ensure_secret SLACK_SIGNING_SECRET "Slack app → Basic Information → Signing Secret"
ensure_secret GEMINI_API_KEY      "Google AI Studio → API key (this powers narration and drafting only)"

project_number="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
runtime_sa="${RUNTIME_SA:-${project_number}-compute@developer.gserviceaccount.com}"
for secret in SLACK_BOT_TOKEN SLACK_SIGNING_SECRET GEMINI_API_KEY; do
  gcloud secrets add-iam-policy-binding "${secret}" \
    --member="serviceAccount:${runtime_sa}" \
    --role=roles/secretmanager.secretAccessor \
    --project "${PROJECT_ID}" --quiet >/dev/null
done
echo "  ${runtime_sa} may read the three secrets"

# --- build -----------------------------------------------------------------
TAG="$(date -u +%Y%m%d-%H%M%S)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE}:${TAG}"

say "4/6 Building ${IMAGE} (context: ${root})"
gcloud builds submit "${root}" \
  --config "${here}/cloudbuild.yaml" \
  --substitutions "_IMAGE=${IMAGE}" \
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
say "5/6 Deploying to Cloud Run (one instance, CPU always allocated)"
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --min-instances=1 \
  --max-instances=1 \
  --no-cpu-throttling \
  --concurrency=20 \
  --cpu=1 \
  --memory=512Mi \
  --timeout=300 \
  --set-secrets "SLACK_BOT_TOKEN=SLACK_BOT_TOKEN:latest,SLACK_SIGNING_SECRET=SLACK_SIGNING_SECRET:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest" \
  --set-env-vars "GEMINI_MODEL=${GEMINI_MODEL},DEMO_PROJECT=/opt/judgment-pack/enterprise-demo,SESSION_ROOT=/tmp,GATEWAY_AUTHORITY=gateway:judgment-pack-slack" \
  --project "${PROJECT_ID}" --quiet

URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" \
        --project "${PROJECT_ID}" --format='value(status.url)')"

say "6/6 Done. Paste this ONE url into three fields of the Slack app:"
cat <<EOF

    ${URL}/slack/events

  api.slack.com/apps → your app →
    • Event Subscriptions      → Request URL        (it must verify: "Verified ✓")
    • Interactivity & Shortcuts → Request URL
    • Slash Commands → /jpack   → Request URL

  Health check (should say "up"):   curl -s ${URL}/
  Logs:  gcloud run services logs tail ${SERVICE} --region ${REGION} --project ${PROJECT_ID}

  The service is public on purpose — Slack posts to it from the internet, and
  every request is authenticated by its signature against the signing secret
  before any handler sees it.

EOF
