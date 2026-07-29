#!/bin/sh
# Open the zoomable pack diagram in the host browser (WSL-aware).
set -eu
f="projects/enterprise-demo/diagrams/${1:-vendor-onboarding}.html"
if command -v explorer.exe >/dev/null 2>&1; then
  explorer.exe "$(wslpath -w "$f")" || true
else
  xdg-open "$f" 2>/dev/null || open "$f"
fi
