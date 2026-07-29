#!/bin/sh
# Register the judgment-pack MCP server in agent-canvas settings via its API.
# agent-canvas keeps MCP configuration in its settings store (written by the UI
# or this API), not in a config file, so a fresh instance self-seeds here.
# Idempotent: an instance that already has the entry is left untouched.
set -eu

URL="${OPENHANDS_URL:-http://openhands:8000}"
KEY_FILE="${API_KEY_FILE:-/state/agent-canvas/api-key.txt}"
JPACK_CONFIG="${JPACK_CONFIG:-/projects/judgment-pack-quickstart/jpack.json}"

i=0
until curl -fsS -o /dev/null "$URL/health" && [ -s "$KEY_FILE" ]; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo "seed-mcp: server or api key not ready after 120s" >&2
    exit 1
  fi
  sleep 2
done

KEY="$(cat "$KEY_FILE")"

if curl -fsS -H "X-Session-API-Key: $KEY" "$URL/api/settings" | grep -q '"judgment-pack"'; then
  echo "seed-mcp: judgment-pack already configured"
  exit 0
fi

curl -fsS -X PATCH -H "X-Session-API-Key: $KEY" -H "Content-Type: application/json" \
  "$URL/api/settings" -o /dev/null \
  -d "{\"agent_settings_diff\":{\"mcp_config\":{\"judgment-pack\":{\"transport\":\"stdio\",\"command\":\"judgment-pack\",\"args\":[\"mcp\"],\"env\":{\"JPACK_CONFIG\":\"$JPACK_CONFIG\"}}}}}"

curl -fsS -H "X-Session-API-Key: $KEY" "$URL/api/settings" | grep -q '"judgment-pack"' \
  && echo "seed-mcp: judgment-pack MCP server registered"
