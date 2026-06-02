"""
Model Context Protocol (MCP) client + server manager.

OMNIBRAIN talks to MCP servers exactly the way LM Studio (or Claude Desktop)
does: a stored JSON document under the ``mcpServers`` key describes how to spawn
each server, e.g.

    {
      "mcpServers": {
        "playwright": { "command": "npx", "args": ["@playwright/mcp@latest"] }
      }
    }

Each server is a child process we speak JSON-RPC 2.0 to over its stdio, using the
newline-delimited framing the MCP stdio transport mandates (one JSON message per
line, no embedded newlines). The handshake is::

    -> initialize
    <- (capabilities)
    -> notifications/initialized
    -> tools/list
    <- (the tools this server exposes)

and a call is ``tools/call`` with ``{name, arguments}``.

Design notes
------------
* Servers do NOT start automatically — the Tools tab has a START button per
  server (with a live status indicator), so the cost of spawning ``npx`` is only
  paid when the user asks. ``status`` reflects stopped / starting / running /
  error at all times.
* Each server owns one reader thread that demultiplexes responses by request id;
  requests block on a per-id Event with a timeout, so a hung server can never
  wedge a generation forever.
* Everything degrades gracefully: if a command is missing, a server dies, or a
  call times out, the manager records an error and the chat simply proceeds
  without that tool rather than crashing.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time

# Protocol revision we advertise. Servers negotiate down if they are older.
PROTOCOL_VERSION = "2024-11-05"

# A tool call should not block a generation indefinitely; browser automation can
# legitimately take a while, so this is generous.
DEFAULT_CALL_TIMEOUT = 120.0
HANDSHAKE_TIMEOUT = 45.0


def _content_to_text(content) -> str:
    """Flatten an MCP tool result's ``content`` array into a single string.

    Tool messages on an OpenAI-compatible endpoint carry a plain string, so each
    structured part is rendered to text: text parts verbatim, images noted by
    size (the base64 itself would bloat the context), resources as compact JSON.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content)[:4000] if content else ""
    parts = []
    for c in content:
        if not isinstance(c, dict):
            parts.append(str(c))
            continue
        t = c.get("type")
        if t == "text":
            parts.append(c.get("text", "") or "")
        elif t == "image":
            data = c.get("data", "") or ""
            parts.append(f"[image {c.get('mimeType', 'image')} · {len(data)} b64 chars]")
        elif t == "resource":
            res = c.get("resource", {})
            txt = res.get("text")
            parts.append(txt if txt else json.dumps(res)[:2000])
        else:
            parts.append(json.dumps(c)[:1000])
    return "\n".join(p for p in parts if p).strip()


class MCPServer:
    """One MCP child process and the JSON-RPC plumbing to drive it."""

    def __init__(self, name: str, command: str, args: list | None, env: dict | None):
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = dict(env or {})
        self.status = "stopped"          # stopped | starting | running | error
        self.detail = ""
        self.tools: list[dict] = []      # [{name, description, inputSchema}]

        self._proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._io_lock = threading.Lock()   # serialise writes to stdin
        self._next_id = 0
        self._pending: dict[int, dict] = {}
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None

    # -- identity --------------------------------------------------------
    def signature(self) -> tuple:
        """Config fingerprint; a change means the server must be respawned."""
        return (self.command, tuple(self.args), tuple(sorted(self.env.items())))

    # -- lifecycle -------------------------------------------------------
    def start(self) -> bool:
        with self._lock:
            if self.status in ("starting", "running"):
                return True
            self.status = "starting"
            self.detail = "spawning process…"
            self.tools = []
        try:
            full_env = os.environ.copy()
            full_env.update(self.env)
            self._proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=full_env,
            )
        except FileNotFoundError:
            self._fail(f"command not found: {self.command}")
            return False
        except Exception as e:  # noqa: BLE001
            self._fail(f"spawn failed: {str(e)[:160]}")
            return False

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_reader.start()

        try:
            self._handshake()
        except Exception as e:  # noqa: BLE001
            self._fail(f"handshake failed: {str(e)[:160]}")
            self.stop()
            return False

        with self._lock:
            self.status = "running"
            self.detail = f"connected · {len(self.tools)} tool(s)"
        return True

    def _handshake(self):
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "omnibrain", "version": "1.0"},
        }, timeout=HANDSHAKE_TIMEOUT)
        self._notify("notifications/initialized", {})
        result = self._request("tools/list", {}, timeout=HANDSHAKE_TIMEOUT)
        tools = result.get("tools") if isinstance(result, dict) else None
        self.tools = tools or []

    def _fail(self, detail: str):
        with self._lock:
            self.status = "error"
            self.detail = detail
            self.tools = []

    def stop(self):
        with self._lock:
            proc = self._proc
            self._proc = None
            self.status = "stopped"
            self.detail = ""
            self.tools = []
            # Unblock anything still waiting on a response.
            for slot in self._pending.values():
                slot["error"] = "server stopped"
                slot["event"].set()
            self._pending.clear()
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def is_running(self) -> bool:
        return self.status == "running" and self._proc is not None and self._proc.poll() is None

    # -- JSON-RPC --------------------------------------------------------
    def _write(self, message: dict):
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("server not running")
        line = json.dumps(message, ensure_ascii=False) + "\n"
        with self._io_lock:
            proc.stdin.write(line)
            proc.stdin.flush()

    def _notify(self, method: str, params: dict):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict, timeout: float):
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            slot = {"event": threading.Event(), "result": None, "error": None}
            self._pending[rid] = slot
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not slot["event"].wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"{method} timed out after {timeout:.0f}s")
        with self._lock:
            self._pending.pop(rid, None)
        if slot["error"] is not None:
            raise RuntimeError(str(slot["error"]))
        return slot["result"]

    def _read_loop(self):
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # ignore non-JSON banner lines some launchers print
            if not isinstance(msg, dict):
                continue
            rid = msg.get("id")
            if rid is None or "method" in msg:
                # A notification or a server-initiated request — we don't drive
                # any of those, so they are safely ignored.
                continue
            with self._lock:
                slot = self._pending.get(rid)
            if not slot:
                continue
            if "error" in msg:
                err = msg["error"]
                slot["error"] = err.get("message", json.dumps(err)) if isinstance(err, dict) else str(err)
            else:
                slot["result"] = msg.get("result", {})
            slot["event"].set()
        # stdout closed: the process is gone.
        with self._lock:
            if self.status == "running":
                self.status = "error"
                self.detail = "server process exited"
                self.tools = []
            for slot in self._pending.values():
                if not slot["event"].is_set():
                    slot["error"] = "server process exited"
                    slot["event"].set()

    def _drain_stderr(self):
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for _line in proc.stderr:
            pass  # consumed so the pipe never blocks; not surfaced to the UI

    # -- tools -----------------------------------------------------------
    def call_tool(self, tool_name: str, arguments: dict, timeout: float = DEFAULT_CALL_TIMEOUT) -> dict:
        """Invoke a tool; returns {text, is_error}."""
        if not self.is_running():
            return {"text": f"[tool error] server '{self.name}' is not running", "is_error": True}
        try:
            result = self._request("tools/call", {
                "name": tool_name,
                "arguments": arguments or {},
            }, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            return {"text": f"[tool error] {str(e)[:300]}", "is_error": True}
        if not isinstance(result, dict):
            return {"text": str(result), "is_error": False}
        text = _content_to_text(result.get("content"))
        is_error = bool(result.get("isError"))
        if not text:
            text = "[tool returned no content]"
        return {"text": text, "is_error": is_error}

    def payload(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "command": self.command,
                "args": list(self.args),
                "status": self.status,
                "detail": self.detail,
                "tools": [{
                    "name": t.get("name", ""),
                    "description": (t.get("description") or "")[:400],
                    "key": f"{self.name}::{t.get('name', '')}",
                } for t in self.tools],
            }


class MCPManager:
    """Owns every configured MCP server and brokers tool calls into them."""

    def __init__(self):
        self._lock = threading.RLock()
        self._servers: dict[str, MCPServer] = {}

    def configure(self, servers_cfg: dict):
        """Reconcile the live servers with a stored ``mcpServers`` mapping.

        Unchanged running servers are left alone; servers whose command/args
        changed are respawned on next start; removed servers are stopped.
        """
        servers_cfg = servers_cfg or {}
        with self._lock:
            # Drop servers that are no longer configured.
            for name in list(self._servers):
                if name not in servers_cfg:
                    self._servers.pop(name).stop()
            for name, cfg in servers_cfg.items():
                if not isinstance(cfg, dict):
                    continue
                desired = MCPServer(name, cfg.get("command", ""), cfg.get("args"), cfg.get("env"))
                existing = self._servers.get(name)
                if existing is None:
                    self._servers[name] = desired
                elif existing.signature() != desired.signature():
                    # Config changed — stop the old one and stage the new spec.
                    existing.stop()
                    self._servers[name] = desired

    def start(self, name: str) -> bool:
        with self._lock:
            srv = self._servers.get(name)
        return srv.start() if srv else False

    def stop(self, name: str) -> bool:
        with self._lock:
            srv = self._servers.get(name)
        if not srv:
            return False
        srv.stop()
        return True

    def call(self, server: str, tool: str, arguments: dict) -> dict:
        with self._lock:
            srv = self._servers.get(server)
        if not srv:
            return {"text": f"[tool error] unknown server '{server}'", "is_error": True}
        return srv.call_tool(tool, arguments)

    def status_payload(self) -> list[dict]:
        with self._lock:
            return [self._servers[n].payload() for n in sorted(self._servers)]

    def running_servers(self) -> list[MCPServer]:
        with self._lock:
            return [s for s in self._servers.values() if s.is_running()]

    def shutdown(self):
        with self._lock:
            servers = list(self._servers.values())
        for s in servers:
            s.stop()


# Module-level singleton — one manager for the whole process (state is shared
# across every connected screen, exactly like the chat and settings).
manager = MCPManager()


def build_tool_specs(settings: dict, session_active: dict) -> tuple[list, dict]:
    """Assemble the OpenAI ``tools`` array + a name→(server,tool) routing map.

    A tool is offered to the model only when **all** of these hold:
      * the master ``mcp_enabled`` switch is on,
      * its server is running,
      * the server is active for the current session (sidebar toggle), and
      * the individual tool is enabled (Tools-tab toggle).

    Tool function names are kept verbatim (e.g. ``browser_snapshot``) so the
    model calls them by their real MCP names; the returned map routes each call
    back to the owning server.
    """
    if not settings.get("mcp_enabled", False):
        return [], {}
    tool_enabled = settings.get("mcp_tool_enabled", {}) or {}
    session_active = session_active or {}
    specs: list[dict] = []
    name_map: dict[str, tuple] = {}
    for srv in manager.running_servers():
        if session_active.get(srv.name, True) is False:
            continue
        for t in srv.tools:
            tname = t.get("name")
            if not tname:
                continue
            key = f"{srv.name}::{tname}"
            if tool_enabled.get(key, True) is False:
                continue
            specs.append({
                "type": "function",
                "function": {
                    "name": tname,
                    "description": t.get("description", "") or "",
                    "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                },
            })
            name_map[tname] = (srv.name, tname)
    return specs, name_map
