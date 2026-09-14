#!/usr/bin/env python3
"""A stand-in vendor-ticket MCP server for demo Act 8.

Speaks MCP over stdio (newline-delimited JSON-RPC) and offers two tools: a
read (`get_vendor`) and a write (`update_vendor_status`). The engine reaches
it through adapter-mcp, exactly as it would reach a vendor's own server; the
only thing stood in for is the vendor. Its book is one JSON file named by
TICKETS_STATE, mounted where the container runtime shim puts it, and a write
without the token the credentials file carries (VENDOR_API_TOKEN) is refused
by the server -- as an error result, which the engine records as the answer
it got.
"""
import json
import os
import sys

STATE = os.environ.get("TICKETS_STATE", "/var/lib/tickets/vendors.json")
TOKEN = os.environ.get("VENDOR_API_TOKEN", "")
EXPECTED = os.environ.get("TICKETS_EXPECTED_TOKEN", "vendor-write-2026")

TOOLS = [
    {
        "name": "get_vendor",
        "description": "Read one vendor's onboarding record as the ticket system holds it.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "update_vendor_status",
        "description": "Set a vendor's onboarding status; the ticket system records who asked.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "status": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["id", "status"],
        },
    },
]


def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save(book):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(book, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE)


def text(payload, is_error=False):
    result = {"content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}], "structuredContent": payload}
    if is_error:
        result["isError"] = True
    return result


def call(name, args):
    book = load()
    if name == "get_vendor":
        vendor = book.get(args.get("id", ""))
        if vendor is None:
            return text({"error": "no such vendor", "id": args.get("id")}, True)
        return text({"id": args["id"], **vendor})
    if name == "update_vendor_status":
        if TOKEN != EXPECTED:
            return text({"error": "write refused: no valid vendor token"}, True)
        vendor = book.get(args.get("id", ""))
        if vendor is None:
            return text({"error": "no such vendor", "id": args.get("id")}, True)
        previous = vendor.get("status")
        vendor["status"] = args["status"]
        if "reason" in args:
            vendor["reason"] = args["reason"]
        vendor["updates"] = vendor.get("updates", 0) + 1
        book[args["id"]] = vendor
        save(book)
        return text({"id": args["id"], "previous": previous, "status": args["status"], "updates": vendor["updates"]})
    return None


def reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            reply(None, error={"code": -32700, "message": "parse error"})
            continue
        method = req.get("method")
        id_ = req.get("id")
        params = req.get("params") or {}
        if method == "initialize":
            reply(id_, {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "ticket-standin", "version": "0.1"}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(id_, {"tools": TOOLS})
        elif method == "tools/call":
            result = call(params.get("name"), params.get("arguments") or {})
            if result is None:
                reply(id_, error={"code": -32602, "message": "unknown tool: %s" % params.get("name")})
            else:
                reply(id_, result)
        elif id_ is not None:
            reply(id_, error={"code": -32601, "message": "method not found: %s" % method})


if __name__ == "__main__":
    main()
