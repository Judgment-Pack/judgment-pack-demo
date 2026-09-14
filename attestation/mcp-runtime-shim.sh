#!/bin/sh
# A container-runtime stand-in for demo Act 8. adapter-mcp launches a
# vendor's MCP server through a container runtime (`docker run --rm --name N
# -v DIR:/secrets:ro --env-file DIR/env -i -- IMAGE ARGS`) and later asks it
# `kill N` and `inspect N`. The demo has no vendor and no runtime in the
# gateway container, so this shim answers those three verbs by running the
# stand-in ticket server in place of the image, with the env file's
# variables -- the credentials the engine handed the adapter -- and nothing
# else of the engine's environment.
set -eu
SERVER="${TICKET_MCP_SERVER:-/usr/local/libexec/ticket-mcp-server.py}"
PIDS="${TICKET_SHIM_PIDS:-/tmp/ticket-shim}"
mkdir -p "$PIDS"
verb="${1:-}"; shift || true
case "$verb" in
  run)
    name=""; envfile=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --name) name="$2"; shift 2 ;;
        --env-file) envfile="$2"; shift 2 ;;
        --) shift; break ;;
        *) shift ;;
      esac
    done
    # $1 is now the image, the rest the server's arguments: the shim maps
    # every image to the one stand-in and passes the arguments on.
    shift || true
    echo $$ > "$PIDS/$name.pid"
    if [ -n "$envfile" ]; then
      set -a; . "$envfile"; set +a
    fi
    exec env -i PATH="$PATH" TICKETS_STATE="${TICKETS_STATE:-/var/lib/tickets/vendors.json}" \
      VENDOR_API_TOKEN="${VENDOR_API_TOKEN:-}" TICKETS_EXPECTED_TOKEN="${TICKETS_EXPECTED_TOKEN:-vendor-write-2026}" \
      python3 "$SERVER" "$@"
    ;;
  kill)
    name="$1"
    if [ -f "$PIDS/$name.pid" ]; then kill "$(cat "$PIDS/$name.pid")" 2>/dev/null || true; fi
    ;;
  inspect)
    name="$1"
    if [ -f "$PIDS/$name.pid" ] && kill -0 "$(cat "$PIDS/$name.pid")" 2>/dev/null; then
      echo "[]"; exit 0
    fi
    rm -f "$PIDS/$name.pid"
    echo "Error: No such object: $name" >&2; exit 1
    ;;
  *)
    echo "ticket shim: unknown verb $verb" >&2; exit 2 ;;
esac
