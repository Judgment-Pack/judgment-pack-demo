#!/bin/sh
# Reset the enterprise demo to its committed state: remove packs authored in
# rehearsals and restore tracked files. State (settings, conversations) is
# untouched. Run before each live demo.
set -eu
if [ -n "$(git status --short projects/enterprise-demo)" ]; then
  echo "discarding these uncommitted changes under projects/enterprise-demo:"
  git status --short projects/enterprise-demo
fi
git checkout -- projects/enterprise-demo
git clean -fd projects/enterprise-demo
# The attestation store and its seal registry are rehearsal state too. They are
# wiped TOGETHER — a store without its registry (or the reverse) can never
# verify again — and the gateway is recreated so its in-memory session map
# matches the empty store. The identity (seed + pin) survives resets.
rm -rf gateway-state/public/store gateway-state/private/registry.jsonl
if command -v docker >/dev/null 2>&1 && [ -n "$(docker compose ps -q gateway 2>/dev/null)" ]; then
  docker compose up -d --force-recreate gateway
else
  echo "note: gateway not running; its store was wiped on disk only"
fi
echo "demo reset: workspace restored"
