#!/usr/bin/env bash
# Install the pinned judgment-pack release into the devcontainer.
set -euo pipefail

JP_VERSION="${JUDGMENT_PACK_VERSION:-0.6.0}"
case "$(uname -m)" in
  x86_64) ARCH=amd64 ;;
  aarch64 | arm64) ARCH=arm64 ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

URL="https://github.com/Judgment-Pack/judgment-pack-runtime/releases/download/v${JP_VERSION}/judgment-pack_${JP_VERSION}_linux_${ARCH}.tar.gz"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

curl -fsSL -o "$TMP/jp.tar.gz" "$URL"
tar -xzf "$TMP/jp.tar.gz" -C "$TMP" judgment-pack jpack
sudo install -m 0755 "$TMP/judgment-pack" "$TMP/jpack" /usr/local/bin/

judgment-pack version
judgment-pack spec test-conformance --quiet
echo "judgment-pack ${JP_VERSION} installed; conformance corpus passes on this machine."
