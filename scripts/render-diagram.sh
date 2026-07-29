#!/bin/sh
# Regenerate diagrams/<id>.md and diagrams/<id>.html for the enterprise demo.
# Uses the pinned runtime image; set RUNTIME_BIN to a local binary to override.
set -eu
id="${1:-vendor-onboarding}"
proj="projects/enterprise-demo"
version="${JUDGMENT_PACK_VERSION:-0.6.0}"
mmd="$(mktemp)"
if [ -n "${RUNTIME_BIN:-}" ]; then
  (cd "$proj" && "$RUNTIME_BIN" packs diagram --id "$id") > "$mmd"
else
  docker run --rm -v "$PWD/$proj":/project:ro "ghcr.io/judgment-pack/judgment-pack:$version" \
    packs diagram --id "$id" > "$mmd"
fi
url="http://localhost:8002/enterprise-demo/diagrams/$id.html"
{
  echo "# $id — pack diagram"
  echo
  echo "**[Open the zoomable viewer]($url)** — scroll to zoom, drag to pan, double-click to fit."
  echo
  echo '```mermaid'
  cat "$mmd"
  echo '```'
} > "$proj/diagrams/$id.md"
python3 scripts/build-diagram-html.py "$id" "$mmd" > "$proj/diagrams/$id.html"
rm -f "$mmd"
echo "rendered $proj/diagrams/$id.{md,html}"
