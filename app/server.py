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
import os
import threading

from flask import Flask, render_template, request, send_file
from flask_socketio import SocketIO, emit

from . import config, llm, tts
from .state import StateStore

LOOPBACK = {"127.0.0.1", "::1", "localhost"}

store = StateStore()

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
# Sessions
# --------------------------------------------------------------------------
@socketio.on("new_session")
def on_new_session(data=None):
    name = (data or {}).get("name") if isinstance(data, dict) else None
    store.new_session(name)
    broadcast_sessions()
    broadcast_active()


@socketio.on("load_session")
def on_load_session(data):
    if store.set_active((data or {}).get("id")):
        broadcast_sessions()
        broadcast_active()


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


@socketio.on("delete_session")
def on_delete_session(data):
    if store.delete_session((data or {}).get("id")):
        broadcast_sessions()
        broadcast_active()


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

    socketio.start_background_task(_run_generation, sid, pid, text, images, settings)


def _emit_tts_audio(seq, wav_bytes, sample_rate):
    socketio.emit("tts_audio", {
        "seq": seq,
        "sample_rate": sample_rate,
        "audio": base64.b64encode(wav_bytes).decode("ascii"),
    })


def _run_generation(sid, pid, text, images, settings):
    global _busy
    buf = []

    # Spin up the Piper per-block speech pipeline only when it is actually in
    # use; the noise engine is handled entirely client-side.
    tts_stream = None
    if (settings.get("voice_enabled")
            and settings.get("tts_engine") == "piper"
            and settings.get("piper_voice")
            and tts.piper_available()):
        socketio.emit("tts_clear", {})  # reset playback ordering for this turn
        tts_stream = tts.TTSStreamer(settings, _emit_tts_audio)

    def on_delta(delta):
        buf.append(delta)
        # Keep the in-memory placeholder current for late-joining screens
        # without thrashing the disk on every token.
        store.buffer_message(sid, pid, "".join(buf))
        socketio.emit("gen_token", {"session_id": sid, "message_id": pid, "delta": delta})
        if tts_stream is not None:
            tts_stream.feed(delta)

    try:
        # Request history excludes the just-added user turn + placeholder;
        # the user turn is re-supplied via `extra_user` so the thinking
        # directive can be applied to it.
        sess = store.get_session(sid)
        req_history = sess["messages"][:-2] if sess else []
        result = llm.stream_completion(
            req_history, settings, text, on_delta,
            stop_flag=_stop_event.is_set,
            user_images=images,
        )
        store.update_message(sid, pid, {
            "content": result["raw"],
            "clean": result["clean"],
            "think": result["think"],
            "has_think": result["has_think"],
            "meta": result["meta"],
            "streaming": False,
        })
    except Exception as e:  # noqa: BLE001
        store.update_message(sid, pid, {
            "content": "".join(buf),
            "clean": "".join(buf),
            "streaming": False,
            "error": True,
            "error_detail": str(e)[:300],
            "meta": {"finish_reason": "error"},
        })
        socketio.emit("toast", {"text": f"Link error: {str(e)[:80]}"})
        socketio.start_background_task(run_link_test)
    finally:
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
