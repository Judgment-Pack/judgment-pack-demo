#!/bin/sh
# Entrypoint of the engine container (Act 8). Provisions the engine's signing
# identity and the stand-in issuer's key pair on first boot, writes the one
# configuration file from its template with the catalog binding pinned by
# digest, and serves. Seed, seal registry, the issuer's private key and the
# written configuration live under /private (never mounted into the sandbox);
# the engine's pin and the issuer's public keys under /engine-pin (read-only
# in the sandbox); the store under /engine (read-write in the sandbox: the
# tamper surface). The decision book the engine resolves a write's judgment
# in is the sandbox project's own, mounted read-only at
# /var/lib/engine/decisions.
set -eu

SEED=/private/engine.seed
PIN=/engine-pin/pinned.pubkey
ISSUER=/private/issuer
KEYS=/engine-pin/issuer-keys.json
CONFIG=/private/engine.json
TEMPLATE=/usr/local/share/engine/engine.template.json
CATALOG=/usr/local/share/engine/catalog
AUTHORITY="${ENGINE_AUTHORITY:-gateway:enterprise-demo-engine}"

# Seed and pin are one artifact, as they are for the desk (gateway-up.sh):
# a surviving half cannot regenerate the other, so refuse rather than serve
# an identity the verifier cannot check.
if [ -f "$SEED" ] && [ ! -f "$PIN" ]; then
  echo "engine-up: seed exists but the pin is missing." >&2
  echo "  Mint a fresh identity: rm engine-state/private/engine.seed and recreate this container." >&2
  exit 1
fi
if [ ! -f "$SEED" ] && [ -f "$PIN" ]; then
  echo "engine-up: pin exists but the seed is missing." >&2
  echo "  Mint a fresh identity: rm engine-state/pin/pinned.pubkey and recreate this container." >&2
  exit 1
fi
if [ ! -f "$SEED" ]; then
  out="$(gateway keygen "$SEED")"
  key="$(echo "$out" | sed -n 's/^publicKey //p')"
  case "$key" in
    *[!0-9a-f]* | '') key='' ;;
  esac
  if [ "${#key}" -ne 64 ]; then
    rm -f "$SEED"
    echo "engine-up: could not parse publicKey from keygen output; identity not provisioned" >&2
    exit 1
  fi
  printf '%s\n' "$key" > "$PIN.tmp"
  mv "$PIN.tmp" "$PIN"
  chmod 0444 "$PIN"
  echo "engine-up: provisioned new engine identity, keyId $(echo "$out" | sed -n 's/^keyId *//p')"
fi

# The stand-in identity provider: one key pair, the private half here and
# the public half where the engine (and the sandbox) can read it. A token is
# minted from the host -- `docker compose exec engine issuer mint /private/issuer
# --subject NAME` -- never from inside the sandbox, which holds no key.
if [ ! -f "$ISSUER/issuer.key" ]; then
  issuer keygen "$ISSUER"
  echo "engine-up: provisioned the stand-in issuer's key pair"
fi
cp "$ISSUER/keys.json" "$KEYS.tmp" && mv "$KEYS.tmp" "$KEYS" && chmod 0444 "$KEYS"

# The binding is pinned by the digest of the catalog file, as `gateway
# connect` would pin it.
binding="$(sha256sum "$CATALOG/tickets.json" | cut -d' ' -f1)"
sed -e "s|__AUTHORITY__|$AUTHORITY|g" -e "s|__BINDING__|$binding|g" "$TEMPLATE" > "$CONFIG.tmp"
mv "$CONFIG.tmp" "$CONFIG"

exec /usr/local/libexec/engine serve --config "$CONFIG"
