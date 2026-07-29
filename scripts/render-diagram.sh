#!/bin/sh
# Regenerate every pack's diagram: diagrams/<id>.md (fenced mermaid with a
# viewer link) and the standalone viewer pages (layer toggles, zoom, pan)
# plus the index. Uses the pinned runtime image; RUNTIME_BIN overrides.
set -eu
proj="projects/enterprise-demo"
version="${JUDGMENT_PACK_VERSION:-0.6.2}"
ids="vendor-onboarding expense-approval intake-triage"
tmp="$(mktemp -d)"
specs=""
for id in $ids; do
  if [ -n "${RUNTIME_BIN:-}" ]; then
    (cd "$proj" && "$RUNTIME_BIN" packs diagram --id "$id") > "$tmp/$id.mmd"
  else
    docker run --rm -v "$PWD/$proj":/project:ro "ghcr.io/judgment-pack/judgment-pack:$version" \
      packs diagram --id "$id" > "$tmp/$id.mmd"
  fi
  {
    echo "# $id — pack diagram"
    echo
    echo "**Zoomable viewer:** http://localhost:8002/enterprise-demo/diagrams/$id.html (layer toggles, zoom, pan)"
    echo
    echo '```mermaid'
    cat "$tmp/$id.mmd"
    echo '```'
  } > "$proj/diagrams/$id.md"
  specs="$specs $id=$tmp/$id.mmd"
done
# shellcheck disable=SC2086
python3 scripts/build-viewer.py "$proj" $specs
rm -rf "$tmp"
