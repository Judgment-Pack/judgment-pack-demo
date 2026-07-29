#!/usr/bin/env bash
# Runs every time OpenHands begins working with this project. Ensures the pinned
# judgment-pack release is available to the agent. The demo image already bakes
# the binary into /usr/local/bin, so this is a no-op there; in any other runtime
# (a fresh sandbox container, a cloud runtime) it installs to ~/.local/bin.
set -euo pipefail

JP_VERSION="0.7.0"

if command -v jpack >/dev/null 2>&1; then
  jpack version
  exit 0
fi

case "$(uname -m)" in
  x86_64) ARCH=amd64 ;;
  aarch64 | arm64) ARCH=arm64 ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

URL="https://github.com/Judgment-Pack/judgment-pack-runtime/releases/download/v${JP_VERSION}/judgment-pack_${JP_VERSION}_linux_${ARCH}.tar.gz"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$HOME/.local/bin"
curl -fsSL -o "$TMP/jp.tar.gz" "$URL" || wget -qO "$TMP/jp.tar.gz" "$URL"
tar -xzf "$TMP/jp.tar.gz" -C "$TMP" jpack
install -m 0755 "$TMP/jpack" "$HOME/.local/bin/"

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc" ;;
esac

"$HOME/.local/bin/jpack" version
