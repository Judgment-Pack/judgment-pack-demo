#!/bin/sh
# Print the embedded VS Code URL (file tree + mermaid markdown preview),
# opened on the enterprise demo project. Token changes per container start.
set -eu
KEY_FILE="${API_KEY_FILE:-state/agent-canvas/api-key.txt}"
url=$(curl -fsS -H "X-Session-API-Key: $(cat "$KEY_FILE")" http://localhost:8000/api/vscode/url \
  | sed 's/.*"url": *"//; s/".*//')
echo "${url%\&folder=workspace}&folder=/projects/enterprise-demo"
