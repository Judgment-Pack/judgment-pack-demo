#!/bin/sh
# Regenerate every pack's diagrams/<id>.md: the deterministic Mermaid source
# in a fenced block. GitHub renders the fence as a diagram; mermaid-capable
# chat clients draw it inline. Uses the pinned runtime image; RUNTIME_BIN
# overrides with a local binary.
set -eu
proj="projects/enterprise-demo"
version="${JUDGMENT_PACK_VERSION:-0.6.2}"
ids="$(python3 -c "import json; print(' '.join(sorted(json.load(open('$proj/jpack.json'))['packs'])))")"
for id in $ids; do
  if [ -n "${RUNTIME_BIN:-}" ]; then
    mmd="$( (cd "$proj" && "$RUNTIME_BIN" packs diagram --id "$id") )"
  else
    mmd="$(docker run --rm -v "$PWD/$proj":/project:ro "ghcr.io/judgment-pack/judgment-pack:$version" packs diagram --id "$id")"
  fi
  {
    echo "# $id — pack diagram"
    echo
    echo "Deterministic rendering of the reviewed pack (\`judgment-pack packs diagram\`)."
    echo "GitHub draws the fence below as a flowchart."
    echo
    echo '```mermaid'
    echo "$mmd"
    echo '```'
  } > "$proj/diagrams/$id.md"
  echo "rendered $proj/diagrams/$id.md"
done
