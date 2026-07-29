#!/bin/sh
# Register the jpack MCP server in agent-canvas settings via its API.
# agent-canvas keeps MCP configuration in its settings store (written by the UI
# or this API), not in a config file, so a fresh instance self-seeds here.
# Idempotent on content: an instance already pointing at this JPACK_CONFIG is
# left untouched; one pointing elsewhere is updated in place.
set -eu

URL="${OPENHANDS_URL:-http://openhands:8000}"
KEY_FILE="${API_KEY_FILE:-/state/agent-canvas/api-key.txt}"
JPACK_CONFIG="${JPACK_CONFIG:-/projects/enterprise-demo/jpack.json}"

echo "seed-mcp: waiting for agent-canvas at $URL ..."
i=0
until curl -fs -o /dev/null "$URL/health" && [ -s "$KEY_FILE" ]; do
  i=$((i + 1))
  if [ "$i" -gt 90 ]; then
    echo "seed-mcp: server or api key not ready after 180s" >&2
    exit 1
  fi
  sleep 2
done

KEY="$(cat "$KEY_FILE")"

if curl -fsS -H "X-Session-API-Key: $KEY" "$URL/api/settings" | grep -q "\"JPACK_CONFIG\": *\"$JPACK_CONFIG\""; then
  echo "seed-mcp: jpack already configured for $JPACK_CONFIG"
  exit 0
fi

curl -fsS -X PATCH -H "X-Session-API-Key: $KEY" -H "Content-Type: application/json" \
  "$URL/api/settings" -o /dev/null \
  -d "{\"agent_settings_diff\":{\"mcp_config\":{\"jpack\":{\"transport\":\"stdio\",\"command\":\"jpack\",\"args\":[\"mcp\"],\"env\":{\"JPACK_CONFIG\":\"$JPACK_CONFIG\"}}}}}"

curl -fsS -H "X-Session-API-Key: $KEY" "$URL/api/settings" | grep -q '"jpack"' \
  && echo "seed-mcp: jpack MCP server registered"
