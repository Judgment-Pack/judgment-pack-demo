#!/usr/bin/env bash
# One-time bootstrap so GitHub Actions can deploy without a service-account key.
#
#   ./slack/deploy/deploy.sh        # FIRST: secrets, registry, database, revision
#   ./slack/deploy/setup-wif.sh     # THEN: the federation for releases
#
# ORDER MATTERS. deploy.sh creates the three Secret Manager secrets, the
# Artifact Registry repository and the Firestore database — the resources a
# release only NAMES. This script verifies they exist and refuses to pretend
# otherwise: a bootstrap that hands back a green checkmark and a first release
# that dies on a missing secret is worse than a script that says which command
# to run.
#
# What Workload Identity Federation buys: a workflow proves which repository —
# and which gated environment — it is running in, and exchanges that proof for
# a Google credential that lives minutes. The alternative is a JSON key in a
# GitHub secret: a permanent credential, copied into a system neither side
# audits, that nobody rotates. This script never creates one.
#
# What it makes, all idempotent:
#   • a workload identity pool and an OIDC provider for token.actions.github…,
#     conditioned on BOTH the repository and the `production` environment
#     claim — which GitHub emits only for a job that cleared that environment's
#     protection rules, so the approval click is enforced by federation rather
#     than by workflow text anyone with push access could edit;
#   • three service accounts with disjoint jobs: a DEPLOYER that can submit a
#     build and roll a revision, a BUILDER the build runs as, and a RUNTIME
#     the container runs as, holding nothing but the three secrets and
#     Firestore;
#   • the staging buckets Cloud Build uploads source to, so neither robot needs
#     permission to CREATE buckets;
#   • the principalSet binding that ties the repository to the deployer;
#   • and the REMOVAL of every grant older versions of these scripts gave the
#     project's default compute account.
#
# It prints exactly what to paste into GitHub, and which values are fine as
# plain Variables rather than Secrets.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DEPLOY_ENV="${DEPLOY_ENV:-${here}/deploy.env}"
# shellcheck disable=SC1090
[ -f "${DEPLOY_ENV}" ] && . "${DEPLOY_ENV}"

REPO="${REPO:-Judgment-Pack/judgment-pack-demo}"
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
CLOUDBUILD_REGION="${CLOUDBUILD_REGION:-${REGION}}"
AR_REPO="${AR_REPO:-judgment-pack}"
SERVICE="${SERVICE:-judgment-pack-slack-demo}"
POOL="${POOL:-github}"
PROVIDER="${PROVIDER:-github-oidc}"
ENVIRONMENT="${ENVIRONMENT:-production}"
DEPLOYER="${DEPLOYER:-jpack-slack-deployer}"
BUILDER="${BUILDER:-jpack-slack-builder}"
RUNTIME="${RUNTIME:-jpack-slack-runtime}"
STATE_BACKEND="${STATE_BACKEND:-firestore}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

command -v gcloud >/dev/null || die "gcloud is not installed — https://cloud.google.com/sdk/docs/install"
# Checked at the door, like every other script here: each call below needs a
# live token, and a refresh cannot be prompted for once we are six API calls in
# with half the federation built.
gcloud auth print-access-token >/dev/null 2>&1 \
  || die "gcloud has no usable credentials — run: gcloud auth login"
[ -n "${PROJECT_ID}" ] || die "no project set — run: gcloud config set project <PROJECT_ID>
  (or put PROJECT_ID in slack/deploy/deploy.env)"
# Tightened to the characters GitHub actually allows in an owner or a repo
# name. The looser check this replaced accepted values that, interpolated into
# the CEL condition below, produced a provider trusting EVERY repository — the
# precise opposite of what the condition is for.
printf '%s' "${REPO}" | grep -qE '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$' \
  || die "REPO must be owner/name using [A-Za-z0-9._-], not ${REPO}"
printf '%s' "${ENVIRONMENT}" | grep -qE '^[A-Za-z0-9._-]+$' \
  || die "ENVIRONMENT must be [A-Za-z0-9._-], not ${ENVIRONMENT}"

say "Project ${PROJECT_ID}, repository ${REPO}, environment ${ENVIRONMENT}"

say "1/9 APIs"
gcloud services enable \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  iam.googleapis.com \
  cloudresourcemanager.googleapis.com \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  --project "${PROJECT_ID}" --quiet

project_number="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
[ -n "${project_number}" ] || die "could not read the project number for ${PROJECT_ID}"
deployer_sa="${DEPLOYER}@${PROJECT_ID}.iam.gserviceaccount.com"
builder_sa="${BUILDER}@${PROJECT_ID}.iam.gserviceaccount.com"
runtime_sa="${RUNTIME}@${PROJECT_ID}.iam.gserviceaccount.com"
compute_sa="${project_number}-compute@developer.gserviceaccount.com"

ensure_sa() {
  local name="$1" description="$2"
  local email="${name}@${PROJECT_ID}.iam.gserviceaccount.com"
  if gcloud iam service-accounts describe "${email}" \
       --project "${PROJECT_ID}" >/dev/null 2>&1; then
    echo "  ${email} exists"
  else
    gcloud iam service-accounts create "${name}" \
      --display-name="${description}" --project "${PROJECT_ID}" --quiet
    echo "  ${email} created"
  fi
}

say "2/9 Workload identity pool ${POOL}"
if gcloud iam workload-identity-pools describe "${POOL}" \
     --location=global --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "  exists"
else
  gcloud iam workload-identity-pools create "${POOL}" \
    --location=global --display-name="GitHub Actions" \
    --project "${PROJECT_ID}" --quiet
  echo "  created"
fi

say "3/9 OIDC provider ${PROVIDER}"
# THE GATE, enforced here rather than in workflow text.
#
# GitHub puts an `environment` claim in the OIDC token only for a job that
# declares an environment AND has cleared its protection rules — so a run
# nobody approved cannot mint this credential at all. Conditioning on the
# repository alone (what this script used to do) left the approval as
# decoration: any workflow on any branch could take the same credential with
# no click.
#
# Pair it with the environment's "Deployment branches and tags" rule set to
# `slack-v*` (GitHub UI — there is no way to set it from here), and the
# credential is reachable only from a tagged release a reviewer approved.
# SETUP.md says so in the same words.
mapping="google.subject=assertion.sub"
mapping="${mapping},attribute.repository=assertion.repository"
mapping="${mapping},attribute.repository_owner=assertion.repository_owner"
mapping="${mapping},attribute.environment=assertion.environment"
mapping="${mapping},attribute.ref=assertion.ref"
condition="assertion.repository=='${REPO}' && assertion.environment=='${ENVIRONMENT}'"
if gcloud iam workload-identity-pools providers describe "${PROVIDER}" \
     --workload-identity-pool="${POOL}" --location=global \
     --project "${PROJECT_ID}" >/dev/null 2>&1; then
  # Both the mapping and the condition, every time: a provider missing
  # attribute.environment cannot be repaired by re-running a script that only
  # re-applies the condition, and "all idempotent" would be a lie.
  gcloud iam workload-identity-pools providers update-oidc "${PROVIDER}" \
    --workload-identity-pool="${POOL}" --location=global \
    --attribute-mapping="${mapping}" \
    --attribute-condition="${condition}" \
    --project "${PROJECT_ID}" --quiet >/dev/null
  echo "  exists (mapping and condition re-applied)"
else
  gcloud iam workload-identity-pools providers create-oidc "${PROVIDER}" \
    --workload-identity-pool="${POOL}" --location=global \
    --display-name="GitHub OIDC" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="${mapping}" \
    --attribute-condition="${condition}" \
    --project "${PROJECT_ID}" --quiet
  echo "  created"
fi
echo "  condition: ${condition}"

say "4/9 Three service accounts, three jobs"
ensure_sa "${DEPLOYER}" "Slack demo — GitHub release deployer"
ensure_sa "${BUILDER}"  "Slack demo — Cloud Build identity"
ensure_sa "${RUNTIME}"  "Slack demo — Cloud Run runtime identity"

say "5/9 Roles — the smallest set each identity can work with"
# DEPLOYER: submits the build, rolls the revision, sets the invoker binding.
#   cloudbuild.builds.editor           create a build, read its log stream
#   serviceusage.serviceUsageConsumer  the quota check every gcloud call makes
#   run.admin                          deploy, and set the service's IAM policy
# Deliberately absent: artifactregistry.* (the BUILDER pushes), secretmanager.*
# (the RUNTIME reads; the deployer only names), storage at project scope.
for role in roles/cloudbuild.builds.editor roles/serviceusage.serviceUsageConsumer roles/run.admin; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${deployer_sa}" --role="${role}" \
    --condition=None --quiet >/dev/null
  echo "  deployer: ${role}"
done

# actAs, scoped to the two accounts it must actually act as — and nothing else.
# Project-wide serviceAccountUser plus run.admin is deploy-anything-as-anyone,
# and the compute default account it would have reached usually carries
# roles/editor.
for target in "${runtime_sa}" "${builder_sa}"; do
  gcloud iam service-accounts add-iam-policy-binding "${target}" \
    --member="serviceAccount:${deployer_sa}" \
    --role=roles/iam.serviceAccountUser \
    --project "${PROJECT_ID}" --quiet >/dev/null
  echo "  deployer: roles/iam.serviceAccountUser on ${target}"
done

# BUILDER: writes logs, reads the staged source, pushes ONE repository's images.
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${builder_sa}" --role=roles/logging.logWriter \
  --condition=None --quiet >/dev/null
echo "  builder: roles/logging.logWriter"

# RUNTIME: the identity an internet-facing container runs as. Three secrets and
# Firestore. Nothing else — no registry write (which would let the running
# service poison the image it is deployed from), no project-wide bucket read.
for secret in SLACK_BOT_TOKEN SLACK_SIGNING_SECRET GEMINI_API_KEY; do
  if gcloud secrets describe "${secret}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud secrets add-iam-policy-binding "${secret}" \
      --member="serviceAccount:${runtime_sa}" \
      --role=roles/secretmanager.secretAccessor \
      --project "${PROJECT_ID}" --quiet >/dev/null
    echo "  runtime: secretAccessor on ${secret}"
  fi
done
if [ "${STATE_BACKEND}" = "firestore" ]; then
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${runtime_sa}" --role=roles/datastore.user \
    --condition=None --quiet >/dev/null
  echo "  runtime: roles/datastore.user"
fi

say "6/9 Artifact Registry ${AR_REPO} in ${REGION}"
if gcloud artifacts repositories describe "${AR_REPO}" \
     --location "${REGION}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "  exists"
else
  # `builds submit --config` pushes to a repository; unlike `--tag`, it never
  # creates one. And a freshly enabled API can refuse the first create for
  # minutes while it provisions — a real failure that fixes itself.
  created=""
  ar_log="$(mktemp)"
  trap 'rm -f "${ar_log}"' EXIT
  for attempt in 1 2 3; do
    if gcloud artifacts repositories create "${AR_REPO}" \
         --repository-format=docker --location="${REGION}" \
         --description="judgment-pack demo images" \
         --project "${PROJECT_ID}" --quiet 2>"${ar_log}"; then
      created="yes"; echo "  created"; break
    fi
    if grep -qiE "already exists|ALREADY_EXISTS" "${ar_log}"; then
      created="yes"; echo "  exists"; break
    fi
    if [ "${attempt}" -lt 3 ]; then
      echo "  attempt ${attempt} refused — Artifact Registry is usually still"
      echo "  provisioning right after the API is enabled; retrying in 60s"
      sleep 60
    fi
  done
  [ -n "${created}" ] || { cat "${ar_log}" >&2; die "could not create ${AR_REPO} in ${REGION}"; }
fi
# Scoped to this repository, not the project: the builder pushes here and
# nowhere else.
gcloud artifacts repositories add-iam-policy-binding "${AR_REPO}" \
  --location="${REGION}" --member="serviceAccount:${builder_sa}" \
  --role=roles/artifactregistry.writer --project "${PROJECT_ID}" --quiet >/dev/null
echo "  builder: roles/artifactregistry.writer on ${AR_REPO} only"

say "7/9 Cloud Build staging buckets"
# Two, because a REGIONAL build stages regionally and a global one does not.
# Created here, by a human who may create buckets, so neither robot needs that
# power. The deployer uploads the source; the builder reads it.
for bucket in "gs://${PROJECT_ID}_cloudbuild" "gs://${PROJECT_ID}_${CLOUDBUILD_REGION}_cloudbuild"; do
  location="US"
  case "${bucket}" in *"_${CLOUDBUILD_REGION}_cloudbuild") location="${CLOUDBUILD_REGION}" ;; esac
  if gcloud storage buckets describe "${bucket}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    echo "  ${bucket} exists"
  elif gcloud storage buckets create "${bucket}" --location="${location}" \
         --project "${PROJECT_ID}" --quiet >/dev/null 2>&1; then
    echo "  ${bucket} created"
  else
    echo "  ${bucket}: could not create it (it may belong to another project, or the"
    echo "  name may be taken). Create it by hand and re-run if the build cannot stage."
    continue
  fi
  gcloud storage buckets add-iam-policy-binding "${bucket}" \
    --member="serviceAccount:${deployer_sa}" \
    --role=roles/storage.objectAdmin --project "${PROJECT_ID}" --quiet >/dev/null 2>&1 \
    && echo "    deployer may stage source here"
  gcloud storage buckets add-iam-policy-binding "${bucket}" \
    --member="serviceAccount:${builder_sa}" \
    --role=roles/storage.objectViewer --project "${PROJECT_ID}" --quiet >/dev/null 2>&1 \
    && echo "    builder may read it"
done

say "8/9 Letting ${REPO} act as ${DEPLOYER}"
principal="principalSet://iam.googleapis.com/projects/${project_number}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${REPO}"
gcloud iam service-accounts add-iam-policy-binding "${deployer_sa}" \
  --member="${principal}" \
  --role=roles/iam.workloadIdentityUser \
  --project "${PROJECT_ID}" --quiet >/dev/null
echo "  ${principal}"
echo "  → may impersonate ${deployer_sa}, and only from a job whose token carries"
echo "    environment=${ENVIRONMENT} (the provider condition, step 3)"
echo
echo "  If REPO ever changes, the OLD repository keeps its binding until you remove it:"
echo "    gcloud iam service-accounts remove-iam-policy-binding ${deployer_sa} \\"
echo "      --member='<the old principalSet>' --role=roles/iam.workloadIdentityUser \\"
echo "      --project ${PROJECT_ID}"

say "9/9 Taking back what the default compute account was given"
# Earlier versions of these scripts granted the project's default compute
# account the build roles — and that account is also what an unconfigured
# Cloud Run service runs as, so an internet-facing container held registry
# write and project-wide bucket read. The build and the runtime now have their
# own identities; this removes the old grants wherever they linger. Idempotent:
# a binding that is not there is not an error.
for role in roles/storage.objectViewer roles/logging.logWriter roles/artifactregistry.writer; do
  if gcloud projects remove-iam-policy-binding "${PROJECT_ID}" \
       --member="serviceAccount:${compute_sa}" --role="${role}" \
       --condition=None --quiet >/dev/null 2>&1; then
    echo "  removed ${role} from ${compute_sa}"
  fi
done
echo "  (nothing removed means nothing was there — which is the desired state)"

# --- the checks that stop a first release failing on a missing resource -----
missing_secrets=""
for secret in SLACK_BOT_TOKEN SLACK_SIGNING_SECRET GEMINI_API_KEY; do
  gcloud secrets describe "${secret}" --project "${PROJECT_ID}" >/dev/null 2>&1 \
    || missing_secrets="${missing_secrets} ${secret}"
done
firestore_missing=""
if [ "${STATE_BACKEND}" = "firestore" ]; then
  gcloud firestore databases describe --project "${PROJECT_ID}" >/dev/null 2>&1 \
    || firestore_missing="yes"
fi

if [ -n "${missing_secrets}" ] || [ -n "${firestore_missing}" ]; then
  {
    echo
    echo "The federation is built, and a release would still fail. Missing:"
    [ -n "${missing_secrets}" ] && echo "  Secret Manager:${missing_secrets}"
    [ -n "${firestore_missing}" ] && echo "  a Firestore database (STATE_BACKEND=firestore)"
    echo
    echo "A release only NAMES these; it creates none of them. One command fixes it,"
    echo "idempotently, asking for each secret with the echo off:"
    echo
    echo "  ./slack/deploy/deploy.sh"
    echo
    echo "Or by hand:"
    echo
    echo "  printf %s '<value>' | gcloud secrets create SLACK_BOT_TOKEN \\"
    echo "    --data-file=- --replication-policy=automatic --project ${PROJECT_ID}"
    echo "  gcloud firestore databases create --location=${REGION} \\"
    echo "    --type=firestore-native --project ${PROJECT_ID}"
    echo
    echo "Then re-run this script, so the runtime account is granted access to them."
  } >&2
  die "bootstrap incomplete — see above (exiting non-zero rather than reporting a
  federation that leads to a failing release)"
fi

provider_resource="projects/${project_number}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"

cat <<EOF

$(printf '\033[1m%s\033[0m' "Everything a release needs exists. Now GitHub.")

  1. Settings → Environments → New environment → ${ENVIRONMENT}
  2. Required reviewers: add yourself. This is the click.
  3. Deployment branches and tags → Selected branches and tags → add a TAG
     rule:  slack-v*
     ^ do not skip this. The federation trusts a token carrying
       environment=${ENVIRONMENT}; this rule is what stops any other ref
       reaching that environment in the first place.

  VARIABLES (none is a credential; they name resources, and a workflow log
  shows them anyway):

    WIF_PROVIDER      ${provider_resource}
    DEPLOY_SA         ${deployer_sa}
    RUNTIME_SA        ${runtime_sa}
    BUILD_SA          ${builder_sa}
    PROJECT_ID        ${PROJECT_ID}
    REGION            ${REGION}
    CLOUDBUILD_REGION ${CLOUDBUILD_REGION}
    SERVICE           ${SERVICE}
    AR_REPO           ${AR_REPO}
    SLACK_APP_ID      (optional, a repository SECRET, not a variable — secrets
                      are masked in public logs) the app id; set it and
                      each release re-points the app's URLs. Leave it unset
                      and that job skips entirely, gate included.

  REPOSITORY SECRETS (Settings → Secrets and variables → Actions), all
  optional:

    SLACK_CONFIG_TOKEN          api.slack.com/apps → App Configuration Tokens
    SLACK_CONFIG_REFRESH_TOKEN  its refresh half
    GH_ADMIN_TOKEN              a token with \`secrets: write\` on this repo.
                                Without it a release will NOT rotate the Slack
                                pair — a rotation it cannot store would kill
                                your credential.

  Repository secrets, not environment secrets, on purpose: an environment
  secret would drag the wiring job through the approval gate a second time.
  The trade is that they are readable by any workflow in this repository —
  acceptable for a demo's app-configuration tokens, and written down here so
  it is a choice rather than an accident.

  Then: git tag slack-v0.1.0 && git push origin slack-v0.1.0

EOF
