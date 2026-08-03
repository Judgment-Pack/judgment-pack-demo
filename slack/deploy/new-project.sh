#!/usr/bin/env bash
# Create a dedicated GCP project for the Slack demo, put billing on it, and
# hand off to deploy.sh.
#
#   ./slack/deploy/new-project.sh [PROJECT_ID]
#
# Why a project of its own: this demo runs arbitrary drafted packs and a
# signing gateway per user. A separate project means its own billing line, its
# own secrets, and one `gcloud projects delete` to remove every trace of it.
#
# Requires an account that may create projects and attach billing. If your
# organization reserves that (most do), ask for a project, then skip straight
# to deploy.sh with PROJECT_ID set — nothing below is load-bearing for the
# deployment itself.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

# The same one place deploy.sh reads: PROJECT_ID, BILLING_ACCOUNT, and the
# optional ORGANIZATION/FOLDER may all live there instead of in your shell.
DEPLOY_ENV="${DEPLOY_ENV:-${here}/deploy.env}"
# shellcheck disable=SC1090
[ -f "${DEPLOY_ENV}" ] && . "${DEPLOY_ENV}"

command -v gcloud >/dev/null || die "gcloud is not installed — https://cloud.google.com/sdk/docs/install"

# A project id is global, immutable, and 6-30 characters of [a-z][a-z0-9-].
PROJECT_ID="${1:-${PROJECT_ID:-jpack-slack-demo-$(date -u +%y%m%d)}}"
PROJECT_NAME="${PROJECT_NAME:-Judgment Pack Slack demo}"
printf '%s' "${PROJECT_ID}" | grep -qE '^[a-z][a-z0-9-]{5,29}$' \
  || die "project id ${PROJECT_ID} must be 6-30 chars, lowercase, starting with a letter"

# Fail here rather than three commands deep: every call below needs a live
# token, and a refresh cannot be prompted for in a non-interactive shell.
gcloud auth print-access-token >/dev/null 2>&1 \
  || die "gcloud has no usable credentials — run: gcloud auth login"

say "1/3 Project ${PROJECT_ID}"
if gcloud projects describe "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "  already exists (leaving it alone)"
else
  extra=()
  [ -n "${ORGANIZATION:-}" ] && extra+=(--organization "${ORGANIZATION}")
  [ -n "${FOLDER:-}" ] && extra+=(--folder "${FOLDER}")
  gcloud projects create "${PROJECT_ID}" --name="${PROJECT_NAME}" "${extra[@]}" --quiet
  echo "  created"
fi

say "2/3 Billing"
current="$(gcloud billing projects describe "${PROJECT_ID}" \
  --format='value(billingAccountName)' 2>/dev/null || true)"
if [ -n "${current}" ]; then
  echo "  already linked to ${current}"
else
  account="${BILLING_ACCOUNT:-}"
  if [ -z "${account}" ]; then
    # Only auto-pick when the choice is unambiguous: one open account.
    mapfile -t open < <(gcloud billing accounts list \
      --filter='open=true' --format='value(name)' 2>/dev/null || true)
    case "${#open[@]}" in
      0) die "no open billing account visible — link one in the console, or set BILLING_ACCOUNT" ;;
      1) account="${open[0]}" ;;
      *) printf '  more than one open billing account:\n' >&2
         gcloud billing accounts list --filter='open=true' \
           --format='table(name,displayName)' >&2
         die "set BILLING_ACCOUNT=<one of the names above> and re-run" ;;
    esac
  fi
  gcloud billing projects link "${PROJECT_ID}" --billing-account="${account}" --quiet >/dev/null
  echo "  linked to ${account}"
fi

# Remember it, so deploy.sh and every re-run land in the same project without
# anyone having to hold the id in their head or their shell.
if [ ! -f "${DEPLOY_ENV}" ]; then
  cp "${here}/deploy.env.example" "${DEPLOY_ENV}"
  echo "  wrote ${DEPLOY_ENV} from the example"
fi
if grep -qE '^PROJECT_ID=' "${DEPLOY_ENV}"; then
  tmp="$(mktemp)"
  sed "s|^PROJECT_ID=.*|PROJECT_ID=\"\${PROJECT_ID:-${PROJECT_ID}}\"|" "${DEPLOY_ENV}" > "${tmp}"
  mv "${tmp}" "${DEPLOY_ENV}"
else
  printf 'PROJECT_ID="${PROJECT_ID:-%s}"\n' "${PROJECT_ID}" >> "${DEPLOY_ENV}"
fi
echo "  ${DEPLOY_ENV} now names ${PROJECT_ID}"

say "3/3 Deploy"
echo "  handing off to deploy.sh"
PROJECT_ID="${PROJECT_ID}" exec "${here}/deploy.sh"
