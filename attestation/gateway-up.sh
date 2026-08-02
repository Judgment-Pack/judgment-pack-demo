#!/bin/sh
# Entrypoint of the gateway container. Provisions the signing identity on
# first boot, then serves. The seed and the seal registry live under /private
# (never mounted into the sandbox); the pin under /gateway-pin (read-only in
# the sandbox); the store under /gateway (read-write in the sandbox — that is
# the tamper surface, and the only one).
#
# The pin is written from keygen's own stdout — the provisioning ceremony —
# never from GET /publickey: fetching the key from the gateway you are about
# to audit proves consistency, not authenticity.
set -eu

SEED=/private/gateway.seed
PIN=/gateway-pin/pinned.pubkey
AUTHORITY="${GATEWAY_AUTHORITY:-gateway:enterprise-demo}"
# The decision desk's law. Exported rather than passed inline so the source
# program sees it however the gateway execs it.
DESK_PROJECT="${DESK_PROJECT:-/usr/local/share/desk/enterprise-demo}"
export DESK_PROJECT

# Seed and pin are one artifact: there is no offline pubkey-from-seed command,
# so a surviving half cannot regenerate the other. Refuse loudly rather than
# serve an identity the verifier cannot check (or re-pin over a live one).
if [ -f "$SEED" ] && [ ! -f "$PIN" ]; then
  echo "gateway-up: seed exists but the pin is missing." >&2
  echo "  Mint a fresh identity: rm gateway-state/private/gateway.seed and recreate this container." >&2
  exit 1
fi
if [ ! -f "$SEED" ] && [ -f "$PIN" ]; then
  echo "gateway-up: pin exists but the seed is missing." >&2
  echo "  Mint a fresh identity: rm gateway-state/pin/pinned.pubkey and recreate this container." >&2
  exit 1
fi

if [ ! -f "$SEED" ]; then
  out="$(gateway keygen "$SEED")"
  key="$(echo "$out" | sed -n 's/^publicKey //p')"
  # Refuse a pin that is not a key: an unparseable keygen format must fail
  # here, at provisioning, not later as an eternally unverifiable identity.
  case "$key" in
    *[!0-9a-f]* | '') key='' ;;
  esac
  if [ "${#key}" -ne 64 ]; then
    rm -f "$SEED"
    echo "gateway-up: could not parse publicKey from keygen output; identity not provisioned" >&2
    exit 1
  fi
  printf '%s\n' "$key" > "$PIN.tmp"
  mv "$PIN.tmp" "$PIN"
  chmod 0444 "$PIN"
  echo "gateway-up: provisioned new identity, keyId $(echo "$out" | sed -n 's/^keyId *//p')"
fi

# Two desks, one identity. The screening desk attests an input; the decision
# desk attests a judgment, evaluating against the pristine project copy baked
# into this container at image build. DESK_PROJECT is passed through when set
# so a deployment can point the desk at another reviewed tree; the default is
# the baked one.
exec gateway serve /gateway/store "$SEED" "$AUTHORITY" /private/registry.jsonl \
  --source "ofac-screening=python3 /usr/local/libexec/ofac-screening-source.py" \
  --source "decision-desk=python3 /usr/local/libexec/decision-desk-source.py" \
  --port 8787
