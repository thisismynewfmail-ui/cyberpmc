import time, json, sys
import socketio

URL = "http://127.0.0.1:" + (__import__("os").environ.get("OMNIBRAIN_PORT","5005"))
ok = lambda b: "PASS" if b else "FAIL"
results = []

sio = socketio.Client()
state = {"settings": None, "session": None, "sessions": None, "link": None,
         "busy": [], "tokens": [], "syncs": 0, "voices": None}

@sio.on("settings")
def _s(d): state["settings"] = d
@sio.on("session_sync")
def _ss(d): state["session"] = d; state["syncs"] += 1
@sio.on("sessions")
def _sl(d): state["sessions"] = d
@sio.on("link")
def _l(d): state["link"] = d
@sio.on("busy")
def _b(d): state["busy"].append(d["busy"])
@sio.on("gen_token")
def _t(d): state["tokens"].append(d["delta"])
@sio.on("voices")
def _v(d): state["voices"] = d

sio.connect(URL, wait_timeout=10)
time.sleep(0.6)

# 1) snapshot received
results.append(("snapshot: settings present", state["settings"] is not None))
results.append(("snapshot: session present", state["session"] is not None))
results.append(("api key masked in broadcast", state["settings"].get("api_key") == ""))

# 2) point at mock endpoint + small context to force cropping behaviour later
sio.emit("update_settings", {"endpoint": "http://127.0.0.1:5099/v1", "model": "Omnibrain-UE"})
time.sleep(0.4)
sio.emit("test_link")
time.sleep(1.0)
results.append(("link online after pointing at mock", state["link"] and state["link"]["online"]))
results.append(("models detected", bool(state["link"].get("models"))))

# 3) new session + send a message (streaming)
sio.emit("new_session", {"name": "ZZ-Test"})
time.sleep(0.4)
state["tokens"].clear()
sio.emit("send_message", {"text": "hello core"})

# wait for generation to finish (busy goes True then False)
for _ in range(100):
    time.sleep(0.1)
    if state["busy"][-2:] == [True, False] or (False in state["busy"] and True in state["busy"] and state["busy"][-1] is False):
        break

time.sleep(0.5)
msgs = state["session"]["messages"]
assistant = [m for m in msgs if m["role"] == "assistant"]
results.append(("streamed tokens received", len(state["tokens"]) > 3))
results.append(("user+assistant stored", len(msgs) >= 2))
amsg = assistant[-1] if assistant else {}
results.append(("assistant not streaming at end", amsg.get("streaming") is False))
results.append(("think detected", amsg.get("has_think") is True))
results.append(("think stripped from clean", "<think>" not in (amsg.get("clean") or "")))
results.append(("answer contains ACK", "ACK" in (amsg.get("clean") or "")))
results.append(("sampler temperature passed through", '"temperature": 1.15' in (amsg.get("clean") or "")))
results.append(("dynamic_temperature passed through", '"dynamic_temperature": true' in (amsg.get("clean") or "")))
results.append(("gen meta present", bool(amsg.get("meta", {}).get("completion_tokens"))))
results.append(("busy toggled True then False", True in state["busy"] and state["busy"][-1] is False))

# 3b) image attachment: a vision turn relays an image_url part to the endpoint
TINY_PNG = ("data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
state["tokens"].clear()
state["busy"] = []
sio.emit("send_message", {"text": "describe this", "images": [TINY_PNG, "not-a-data-url"]})
for _ in range(100):
    time.sleep(0.1)
    if state["busy"] and state["busy"][-1] is False and True in state["busy"]:
        break
time.sleep(0.5)
imsgs = state["session"]["messages"]
user_imgs = [m for m in imsgs if m["role"] == "user" and m.get("images")]
results.append(("image stored on user message", bool(user_imgs)))
results.append(("malformed image filtered out", user_imgs and len(user_imgs[-1]["images"]) == 1))
last_assistant = [m for m in imsgs if m["role"] == "assistant"][-1]
results.append(("image relayed to endpoint", "images=1" in (last_assistant.get("clean") or "")))

# 4) context cropping: shrink window hard and add several turns
sio.emit("update_settings", {"context_size": 400, "context_threshold": 70, "max_tokens": 0})
time.sleep(0.3)
for i in range(4):
    state["busy"] = []
    sio.emit("send_message", {"text": "padding message number %d " % i * 8})
    for _ in range(80):
        time.sleep(0.08)
        if state["busy"] and state["busy"][-1] is False:
            break
    time.sleep(0.2)
ctx = state["session"].get("context", {})
results.append(("context dropped > 0 after padding", ctx.get("dropped", 0) > 0))
results.append(("context still keeps messages", len(state["session"]["messages"]) > 0))

# 5) session ops
before = len(state["sessions"]["sessions"])
sio.emit("duplicate_session", {"id": state["session"]["id"]})
time.sleep(0.4)
results.append(("duplicate increased count", len(state["sessions"]["sessions"]) == before + 1))
dup_id = state["sessions"]["active_id"]
sio.emit("rename_session", {"id": dup_id, "name": "Renamed-Test"})
time.sleep(0.4)
renamed = [s for s in state["sessions"]["sessions"] if s["id"] == dup_id]
results.append(("rename applied", renamed and renamed[0]["name"] == "Renamed-Test"))
sio.emit("delete_session", {"id": dup_id})
time.sleep(0.4)
results.append(("delete reduced count", len(state["sessions"]["sessions"]) == before))

# 5b) speech / TTS surface
results.append(("voices snapshot on connect", state["voices"] is not None))
results.append(("voices payload shape", state["voices"] is not None and
                set(state["voices"].keys()) >= {"voices", "piper_available", "engine"}))
results.append(("default tts fields present", state["settings"].get("tts_engine") == "noise"
                and state["settings"].get("voice_enabled") is False))
sio.emit("update_settings", {"tts_engine": "piper", "voice_enabled": True,
                             "piper_voice": "test_voice", "noise_pitch": 440})
time.sleep(0.4)
sio.emit("list_voices")
time.sleep(0.3)
results.append(("tts settings updated", state["settings"].get("tts_engine") == "piper"
                and state["settings"].get("noise_pitch") == 440))
results.append(("voices reflects selection", state["voices"].get("selected") == "test_voice"))
# back to defaults so a re-run starts clean and audio stays off
sio.emit("update_settings", {"tts_engine": "noise", "voice_enabled": False, "piper_voice": ""})
time.sleep(0.3)

# 6) persistence on disk
import os as _os
_root = _os.path.join(_os.path.dirname(__file__), "..", "data", "state.json")
with open(_root) as f:
    disk = json.load(f)
results.append(("settings persisted to disk", disk["settings"]["endpoint"] == "http://127.0.0.1:5099/v1"))
results.append(("api key NOT written when unedited", disk["settings"].get("api_key", "") == ""))

sio.disconnect()

print("\n==================== TEST RESULTS ====================")
fails = 0
for name, cond in results:
    if not cond: fails += 1
    print(f"  [{ok(cond)}] {name}")
print("=====================================================")
print(f"  {len(results)-fails}/{len(results)} passed")
sys.exit(1 if fails else 0)
