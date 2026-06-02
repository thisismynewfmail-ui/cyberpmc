#!/usr/bin/env python3
"""
A tiny stdio MCP server for the test-suite.

Speaks newline-delimited JSON-RPC 2.0 exactly like a real MCP server, exposing a
single ``echo`` tool. Used by the integration test to exercise the whole tool
loop (discover → call → fold result back) without needing ``npx`` or a network.
"""
import json
import sys

TOOLS = [{
    "name": "echo",
    "description": "Echo back the provided text, prefixed with 'echo: '.",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Text to echo"}},
        "required": ["text"],
    },
}]


def _send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _result(rid, result):
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        rid = req.get("id")

        if method == "initialize":
            _result(rid, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-mcp", "version": "1.0"},
            })
        elif method == "notifications/initialized":
            pass  # a notification — no response
        elif method == "tools/list":
            _result(rid, {"tools": TOOLS})
        elif method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                _result(rid, {
                    "content": [{"type": "text", "text": "echo: " + str(args.get("text", ""))}],
                    "isError": False,
                })
            else:
                _result(rid, {
                    "content": [{"type": "text", "text": f"unknown tool: {name}"}],
                    "isError": True,
                })
        elif rid is not None:
            _send({"jsonrpc": "2.0", "id": rid,
                   "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
