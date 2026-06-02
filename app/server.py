"""
The terminal server.

One process holds all state; every browser/monitor on the LAN connects to it
over Socket.IO and receives the same settings and conversation in real time.
Mutations from any screen are broadcast to all screens.

`lan_visible` gates whether non-loopback clients are accepted, so visibility
can be toggled at runtime without rebinding the socket.
"""

from __future__ import annotations

import base64
import json
import os
import threading

from flask import Flask, render_template, request, send_file
from flask_socketio import SocketIO, emit

from . import config, llm, tts
from .mcp import manager as mcp_manager, build_tool_specs
from .state import StateStore

LOOPBACK = {"127.0.0.1", "::1", "localhost"}

store = StateStore()

# Stage the configured MCP servers (stopped) so they are ready to START from the
# Tools tab. Spawning is always explicit — nothing launches on boot.
mcp_manager.configure(store.get_settings().get("mcp_servers", {}))

app = Flask(
    __name__,
    static_folder="../static",
    template_folder="../templates",
)
app.config["SECRET_KEY"] = "omnibrain-cognition-core"
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

# sid -> {"addr": str}
_clients: dict[str, dict] = {}
_clients_lock = threading.Lock()

# Generation control (single active generation across all screens).
_gen_lock = threading.Lock()
_busy = False
_stop_event = threading.Event()

# Image input limits. Images arrive as `data:image/…;base64,…` URLs already
# compressed in the browser; these caps are a server-side backstop against an
# oversized or malformed payload.
MAX_IMAGES = 8
MAX_IMAGE_CHARS = 12_000_000  # ~9 MB of base64 per image


def _sanitize_images(images) -> list:
    """Keep only well-formed, reasonably-sized image data URLs."""
    if not isinstance(images, list):
        return []
    out = []
    for im in images[:MAX_IMAGES]:
        if (isinstance(im, str)
                and im.startswith("data:image/")
                and "base64," in im
                and len(im) <= MAX_IMAGE_CHARS):
            out.append(im)
    return out


# Link-test ordering: a slow result from a previous endpoint must never
# overwrite a newer one, so every probe carries a sequence number.
_link_seq = 0
_link_seq_lock = threading.Lock()


# --------------------------------------------------------------------------
# Access helpers
# --------------------------------------------------------------------------
def _is_loopback(addr: str | None) -> bool:
    return (addr or "") in LOOPBACK


def _access_allowed(addr: str | None, token: str | None) -> tuple[bool, str]:
    s = store.get_settings()
    if not s.get("lan_visible", True) and not _is_loopback(addr):
        return False, "LAN visibility disabled"
    required = s.get("access_token", "")
    if required and token != required:
        return False, "Invalid access token"
    return True, "ok"


@app.before_request
def _gate_http():
    # Static assets + the shell page load for everyone allowed on the network;
    # the access token is enforced on the socket connection.
    s = store.get_settings()
    if not s.get("lan_visible", True) and not _is_loopback(request.remote_addr):
        return ("Forbidden: this terminal is set to local-only visibility.", 403)


# --------------------------------------------------------------------------
# Broadcast helpers
# --------------------------------------------------------------------------
def _client_count() -> int:
    with _clients_lock:
        return len(_clients)


def broadcast_clients():
    socketio.emit("clients", {"count": _client_count()})


def broadcast_settings():
    socketio.emit("settings", store.public_settings())


def broadcast_voices():
    socketio.emit("voices", tts.voices_payload(store.get_settings()))


def broadcast_sessions():
    socketio.emit("sessions", {
        "sessions": store.list_sessions(),
        "active_id": store.active_id(),
    })


def active_session_payload() -> dict:
    sid = store.active_id()
    s = store.get_session(sid) or {"id": sid, "name": "", "messages": []}
    report = llm.context_report(s["messages"], store.get_settings())
    return {
        "id": s["id"],
        "name": s["name"],
        "messages": s["messages"],
        "context": report,
    }


def broadcast_active():
    socketio.emit("session_sync", active_session_payload())


def broadcast_busy():
    socketio.emit("busy", {"busy": _busy})


def mcp_state_payload() -> dict:
    """Full MCP snapshot: master switch, every server's live status + tools, the
    stored JSON config, and the active session's per-server toggles."""
    s = store.get_settings()
    session_active = store.session_mcp_active(store.active_id())
    tool_enabled = s.get("mcp_tool_enabled", {}) or {}
    servers = mcp_manager.status_payload()
    for srv in servers:
        srv["session_active"] = session_active.get(srv["name"], True)
        for t in srv["tools"]:
            t["enabled"] = tool_enabled.get(t["key"], True)
    return {
        "enabled": bool(s.get("mcp_enabled", False)),
        "servers": servers,
        "config_json": json.dumps({"mcpServers": s.get("mcp_servers", {})}, indent=2),
    }


def broadcast_mcp():
    socketio.emit("mcp_state", mcp_state_payload())


def run_link_test():
    global _link_seq
    with _link_seq_lock:
        _link_seq += 1
        my_seq = _link_seq
    result = llm.test_link(store.get_settings())
    # Drop the result if a newer probe has since been launched.
    with _link_seq_lock:
        if my_seq != _link_seq:
            return result
    socketio.emit("link", result)
    return result


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/tts/preview/<voice_id>.wav")
def tts_preview(voice_id):
    path = tts.preview_path(voice_id)
    if not os.path.exists(path):
        return ("preview not found", 404)
    return send_file(path, mimetype="audio/wav", conditional=True)


# --------------------------------------------------------------------------
# Socket lifecycle
# --------------------------------------------------------------------------
@socketio.on("connect")
def on_connect(auth):
    addr = request.remote_addr
    token = (auth or {}).get("token") if isinstance(auth, dict) else None
    ok, reason = _access_allowed(addr, token)
    if not ok:
        emit("access_denied", {"reason": reason})
        return False  # reject the connection

    with _clients_lock:
        _clients[request.sid] = {"addr": addr}

    # Full snapshot to the newly connected screen.
    emit("settings", store.public_settings())
    emit("sessions", {"sessions": store.list_sessions(), "active_id": store.active_id()})
    emit("session_sync", active_session_payload())
    emit("voices", tts.voices_payload(store.get_settings()))
    emit("mcp_state", mcp_state_payload())
    emit("busy", {"busy": _busy})
    emit("clients", {"count": _client_count()})
    broadcast_clients()
    socketio.start_background_task(run_link_test)
    return True


@socketio.on("disconnect")
def on_disconnect():
    with _clients_lock:
        _clients.pop(request.sid, None)
    broadcast_clients()


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
@socketio.on("update_settings")
def on_update_settings(data):
    patch = data or {}
    prev = store.get_settings()
    store.update_settings(patch)
    broadcast_settings()
    # Context-affecting changes -> refresh the horizon read-out everywhere.
    broadcast_active()
    # The MCP master switch lives in settings; keep the Tools surface in sync.
    if "mcp_enabled" in patch:
        broadcast_mcp()

    new = store.get_settings()
    # If LAN visibility was just disabled, drop any non-loopback screens.
    if prev.get("lan_visible") and not new.get("lan_visible"):
        with _clients_lock:
            drop = [sid for sid, c in _clients.items() if not _is_loopback(c["addr"])]
        for sid in drop:
            socketio.emit("access_denied", {"reason": "LAN visibility disabled"}, to=sid)
            socketio.disconnect(sid)
    # Re-test the link when endpoint details change.
    if any(prev.get(k) != new.get(k) for k in config.LINK_FIELDS):
        socketio.start_background_task(run_link_test)

    # --- Speech engine bookkeeping ---
    voice_changed = prev.get("piper_voice") != new.get("piper_voice")
    engine_changed = prev.get("tts_engine") != new.get("tts_engine")
    disabled = prev.get("voice_enabled") and not new.get("voice_enabled")
    # Release the resident Piper model whenever it can no longer be in use, or
    # when a different voice is selected (clean switch + memory cleanup).
    if voice_changed or disabled or (engine_changed and new.get("tts_engine") != "piper"):
        tts.unload()
    # Stop any in-flight playback on every screen when voice output is turned
    # off or the engine changes underneath it.
    if disabled or engine_changed:
        socketio.emit("tts_clear", {})
    if voice_changed or engine_changed or prev.get("voice_enabled") != new.get("voice_enabled"):
        broadcast_voices()


@socketio.on("list_voices")
def on_list_voices(_data=None):
    emit("voices", tts.voices_payload(store.get_settings()))


@socketio.on("generate_previews")
def on_generate_previews(_data=None):
    socketio.start_background_task(_do_generate_previews)


def _do_generate_previews():
    if not tts.piper_available():
        socketio.emit("toast", {"text": "Piper TTS not installed — pip install piper-tts"})
        socketio.emit("previews_done", {"made": [], "ok": False})
        return
    pending = [v for v in tts.list_voices() if not v["has_preview"]]
    if not pending:
        socketio.emit("toast", {"text": "All voices already have a preview"})
        socketio.emit("previews_done", {"made": [], "ok": True})
        return
    socketio.emit("toast", {"text": f"Generating {len(pending)} voice preview(s)…"})
    made = tts.generate_missing_previews()
    broadcast_voices()
    socketio.emit("previews_done", {"made": made, "ok": True})
    socketio.emit("toast", {"text": f"Voice previews ready ({len(made)} new)"})


@socketio.on("test_link")
def on_test_link(_data=None):
    socketio.start_background_task(run_link_test)


@socketio.on("load_sampler_defaults")
def on_load_sampler_defaults(_data=None):
    store.update_settings({"sampling": dict(config.DEFAULT_SAMPLING)})
    broadcast_settings()
    emit("toast", {"text": "Recommended sampler profile loaded"})


# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------
@socketio.on("mcp_refresh")
def on_mcp_refresh(_data=None):
    emit("mcp_state", mcp_state_payload())


@socketio.on("mcp_set_enabled")
def on_mcp_set_enabled(data):
    store.update_settings({"mcp_enabled": bool((data or {}).get("enabled"))})
    broadcast_settings()
    broadcast_mcp()


@socketio.on("mcp_save_servers")
def on_mcp_save_servers(data):
    """Persist the LM Studio-style mcpServers JSON, then reconcile the manager."""
    raw = (data or {}).get("json", "")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except json.JSONDecodeError as e:
        emit("toast", {"text": f"Invalid JSON: {str(e)[:80]}"})
        return
    # Accept either the full {"mcpServers": {...}} document or a bare mapping.
    servers = parsed.get("mcpServers") if isinstance(parsed, dict) and "mcpServers" in parsed else parsed
    if not isinstance(servers, dict):
        emit("toast", {"text": "Expected an object of MCP servers"})
        return
    store.update_settings({"mcp_servers": servers})
    mcp_manager.configure(servers)
    broadcast_settings()
    broadcast_mcp()
    emit("toast", {"text": f"Saved {len(servers)} MCP server(s)"})


@socketio.on("mcp_start_server")
def on_mcp_start_server(data):
    name = (data or {}).get("name")
    if not name:
        return
    broadcast_mcp()  # reflect the "starting" state immediately

    def _start():
        ok = mcp_manager.start(name)
        broadcast_mcp()
        socketio.emit("toast", {"text": (f"MCP '{name}' connected" if ok
                                          else f"MCP '{name}' failed to start")})

    socketio.start_background_task(_start)


@socketio.on("mcp_stop_server")
def on_mcp_stop_server(data):
    name = (data or {}).get("name")
    if name and mcp_manager.stop(name):
        broadcast_mcp()
        emit("toast", {"text": f"MCP '{name}' stopped"})


@socketio.on("mcp_toggle_tool")
def on_mcp_toggle_tool(data):
    key = (data or {}).get("key")
    if not key:
        return
    enabled = bool((data or {}).get("enabled"))
    store.update_settings({"mcp_tool_enabled": {key: enabled}})
    broadcast_mcp()


@socketio.on("mcp_session_toggle")
def on_mcp_session_toggle(data):
    server = (data or {}).get("server")
    if not server:
        return
    enabled = bool((data or {}).get("enabled"))
    if store.set_session_mcp(store.active_id(), server, enabled):
        broadcast_mcp()


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------
@socketio.on("new_session")
def on_new_session(data=None):
    name = (data or {}).get("name") if isinstance(data, dict) else None
    store.new_session(name)
    broadcast_sessions()
    broadcast_active()
    broadcast_mcp()  # the new session's per-server toggles take effect


@socketio.on("load_session")
def on_load_session(data):
    if store.set_active((data or {}).get("id")):
        broadcast_sessions()
        broadcast_active()
        broadcast_mcp()


@socketio.on("rename_session")
def on_rename_session(data):
    if store.rename_session((data or {}).get("id"), (data or {}).get("name", "")):
        broadcast_sessions()
        broadcast_active()


@socketio.on("duplicate_session")
def on_duplicate_session(data):
    new_id = store.duplicate_session((data or {}).get("id"))
    if new_id:
        store.set_active(new_id)
        broadcast_sessions()
        broadcast_active()
        broadcast_mcp()


@socketio.on("delete_session")
def on_delete_session(data):
    if store.delete_session((data or {}).get("id")):
        broadcast_sessions()
        broadcast_active()
        broadcast_mcp()


@socketio.on("clear_session")
def on_clear_session(data):
    if store.clear_session((data or {}).get("id")):
        broadcast_sessions()
        broadcast_active()


@socketio.on("delete_message")
def on_delete_message(data):
    sid = store.active_id()
    if store.delete_message(sid, (data or {}).get("id")):
        broadcast_sessions()
        broadcast_active()


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------
@socketio.on("stop_generation")
def on_stop_generation(_data=None):
    _stop_event.set()
    socketio.emit("tts_clear", {})


@socketio.on("send_message")
def on_send_message(data):
    global _busy
    text = ((data or {}).get("text") or "").strip()
    images = _sanitize_images((data or {}).get("images"))
    if not text and not images:
        return
    with _gen_lock:
        if _busy:
            emit("toast", {"text": "Cognition core is busy"})
            return
        _busy = True
    _stop_event.clear()
    broadcast_busy()

    sid = store.active_id()
    settings = store.get_settings()

    # Resolve the tool set for THIS generation up front so it stays stable for
    # the whole turn (master switch + running servers + session + per-tool gates).
    tools, name_map = build_tool_specs(settings, store.session_mcp_active(sid))

    # 1. record the user's turn (images ride along on the message record)
    user_msg = {"role": "user", "content": text}
    if images:
        user_msg["images"] = images
    store.add_message(sid, user_msg)
    broadcast_active()

    # 2. past reasoning blocks evaporate the moment a new turn begins
    store.collapse_prior_think(sid)

    # 3. assistant placeholder that will fill in as tokens arrive
    placeholder = store.add_message(sid, {"role": "assistant", "content": "", "streaming": True})
    pid = placeholder["id"]
    broadcast_active()

    socketio.start_background_task(_run_generation, sid, pid, text, images, settings,
                                   tools, name_map)


def _emit_tts_audio(seq, wav_bytes, sample_rate):
    socketio.emit("tts_audio", {
        "seq": seq,
        "sample_rate": sample_rate,
        "audio": base64.b64encode(wav_bytes).decode("ascii"),
    })


def _execute_tool_calls(sid, tool_calls, name_map):
    """Run each requested tool through its MCP server, storing a `tool` message
    per call (live "running" → final result) so the transcript shows the work.

    This is the hand-off between the two systems: the model's requested calls are
    routed to the MCP manager and their observations are written back into the
    session as `tool` messages, ready to be replayed into the next model turn.
    """
    for tc in tool_calls:
        tname = tc["name"]
        # Show the call as pending before the (possibly slow) tool runs.
        tmsg = store.add_message(sid, {
            "role": "tool",
            "tool_call_id": tc["id"],
            "name": tname,
            "content": "",
            "server": (name_map.get(tname) or ("", ""))[0],
            "status": "running",
        })
        broadcast_active()

        # Everything from here is wrapped so the "running" placeholder is ALWAYS
        # resolved — a parse slip, an unknown tool, or an unexpected error becomes
        # a tool result the model can react to, never a block stuck on "executing…".
        try:
            route = name_map.get(tname)
            if route is None:
                out = {"text": f"[tool error] unknown or disabled tool '{tname}'",
                       "is_error": True}
            else:
                # Parse the streamed argument string into an object for the call.
                raw_args = tc.get("arguments") or ""
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError as e:
                    # Surface the bad JSON back to the model instead of silently
                    # calling with no arguments (which would mask the mistake).
                    out = {"text": f"[tool error] arguments were not valid JSON "
                                   f"({str(e)[:80]}): {raw_args[:200]}", "is_error": True}
                else:
                    if not isinstance(args, dict):
                        args = {"value": args}
                    out = mcp_manager.call(route[0], route[1], args)
        except Exception as e:  # noqa: BLE001
            out = {"text": f"[tool error] {str(e)[:300]}", "is_error": True}

        store.update_message(sid, tmsg["id"], {
            "content": out["text"],
            "status": "error" if out.get("is_error") else "ok",
        })
        broadcast_active()


def _run_generation(sid, pid, text, images, settings, tools=None, name_map=None):
    global _busy
    name_map = name_map or {}
    # A mutable cursor so the streaming callback always targets the current
    # assistant turn — across every hop of the tool loop.
    cur = {"pid": pid, "buf": []}

    # Spin up the Piper per-block speech pipeline ONCE for the whole turn (it is
    # not reset between tool hops, so spoken output is never cut off mid-reply);
    # the noise engine is handled entirely client-side.
    tts_stream = None
    if (settings.get("voice_enabled")
            and settings.get("tts_engine") == "piper"
            and settings.get("piper_voice")
            and tts.piper_available()):
        socketio.emit("tts_clear", {})  # reset playback ordering for this turn
        tts_stream = tts.TTSStreamer(settings, _emit_tts_audio)

    def on_delta(delta):
        cur["buf"].append(delta)
        # Keep the in-memory placeholder current for late-joining screens
        # without thrashing the disk on every token.
        store.buffer_message(sid, cur["pid"], "".join(cur["buf"]))
        socketio.emit("gen_token", {"session_id": sid, "message_id": cur["pid"], "delta": delta})
        if tts_stream is not None:
            tts_stream.feed(delta)

    max_iters = max(1, int(settings.get("mcp_max_iterations", 8) or 8))

    try:
        for turn in range(max_iters):
            first = (turn == 0)
            cur["buf"] = []
            # History excludes the current (streaming) placeholder, which is
            # always the last stored message. On the first hop the user turn is
            # re-supplied via `extra_user` so the thinking directive applies; on
            # later hops the prior calls + tool results are already in history.
            sess = store.get_session(sid)
            hist = sess["messages"][:-1] if sess else []
            if first:
                req_history = hist[:-1]   # drop the user turn (re-supplied below)
                extra_user, extra_images = text, images
            else:
                req_history = hist
                extra_user, extra_images = None, None

            result = llm.stream_completion(
                req_history, settings, extra_user, on_delta,
                stop_flag=_stop_event.is_set,
                user_images=extra_images,
                tools=tools or None,
            )

            stopped = _stop_event.is_set()
            tool_calls = result["tool_calls"] if (tools and not stopped) else []

            patch = {
                "content": result["raw"],
                "clean": result["clean"],
                "think": result["think"],
                "has_think": result["has_think"],
                "meta": result["meta"],
                "streaming": False,
            }
            if tool_calls:
                patch["tool_calls"] = tool_calls
            store.update_message(sid, cur["pid"], patch)
            broadcast_active()

            if not tool_calls:
                break  # a normal final answer — the turn is complete

            # --- transition: run the tools, fold results back into context ---
            _execute_tool_calls(sid, tool_calls, name_map)

            if turn == max_iters - 1:
                socketio.emit("toast", {"text": f"Tool loop cap reached ({max_iters})"})
                break

            # Open a fresh placeholder for the model's next turn and continue.
            placeholder = store.add_message(sid, {"role": "assistant", "content": "", "streaming": True})
            cur["pid"] = placeholder["id"]
            broadcast_active()

    except Exception as e:  # noqa: BLE001
        store.update_message(sid, cur["pid"], {
            "content": "".join(cur["buf"]),
            "clean": "".join(cur["buf"]),
            "streaming": False,
            "error": True,
            "error_detail": str(e)[:300],
            "meta": {"finish_reason": "error"},
        })
        socketio.emit("toast", {"text": f"Link error: {str(e)[:80]}"})
        socketio.start_background_task(run_link_test)
    finally:
        # Safety net: never leave a tool block wedged on "executing…". If the
        # turn ended (error, stop, or otherwise) while a tool message was still
        # marked running, finalize it so the transcript settles.
        _finalize_running_tools(sid)
        if tts_stream is not None:
            # Honour a user stop by dropping queued speech; otherwise speak the
            # final partial clause before the worker drains and exits.
            if _stop_event.is_set():
                tts_stream.stop()
            else:
                tts_stream.finish()
            tts_stream.close()
        with _gen_lock:
            _busy = False
        broadcast_busy()
        broadcast_active()
        broadcast_sessions()


def _finalize_running_tools(sid):
    """Resolve any lingering ``status == "running"`` tool messages in a session.

    ``_execute_tool_calls`` already finalizes every call it makes, so this only
    fires if the generation thread unwound before a tool message was updated —
    in which case we close it out as an error rather than leaving the UI showing
    "executing…" forever."""
    sess = store.get_session(sid)
    if not sess:
        return
    for m in sess["messages"]:
        if m.get("role") == "tool" and m.get("status") == "running":
            store.update_message(sid, m["id"], {
                "content": m.get("content") or "[tool error] interrupted before completion",
                "status": "error",
            })


def run(host=None, port=None):
    host = host or config.HOST
    port = port or config.PORT
    s = store.get_settings()
    vis = "LAN-visible" if s.get("lan_visible", True) else "local-only"
    print("=" * 62)
    print("  OMNIBRAIN // COGNITION TERMINAL")
    print(f"  Local screen : http://localhost:{port}")
    print(f"  LAN screens  : http://<this-machine-ip>:{port}   [{vis}]")
    print(f"  Endpoint     : {s.get('endpoint')}  (model: {s.get('model')})")
    print("=" * 62)
    socketio.run(app, host=host, port=port, allow_unsafe_werkzeug=True)
