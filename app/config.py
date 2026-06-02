"""
Central configuration & default state for the OMNIBRAIN cognition terminal.

Everything here is the *factory default*. Live values are stored in
data/state.json and may be edited from the Settings panel at runtime.
"""

import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

# Bundled example MCP configuration (LM Studio-style ``mcpServers`` JSON). Used
# as the factory default so a fresh install already lists the Playwright server.
TOOLS_FILE = os.path.join(BASE_DIR, "tools.json")


def _load_default_mcp_servers() -> dict:
    try:
        with open(TOOLS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        servers = data.get("mcpServers")
        if isinstance(servers, dict):
            return servers
    except Exception:  # noqa: BLE001
        pass
    # Fallback to the canonical Playwright example if tools.json is unavailable.
    return {"playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]}}


DEFAULT_MCP_SERVERS = _load_default_mcp_servers()

# Piper TTS voice models live here (.onnx + .onnx.json), and the auto-generated
# previews for each voice land in the previews/ subfolder.
VOICES_DIR = os.path.join(BASE_DIR, "voices")
VOICE_PREVIEW_DIR = os.path.join(VOICES_DIR, "previews")
# Sample line spoken when generating a voice preview.
PREVIEW_TEXT = "Cognition core online. Voice synthesis channel is active and ready."

# A plain ChatML template used only when "use_custom_template" is enabled
# (raw /v1/completions mode). In the default mode we send the `messages`
# array to /v1/chat/completions and let the endpoint apply its own template.
DEFAULT_CHAT_TEMPLATE = (
    "{% if system_message %}<|im_start|>system\n{{ system_message }}<|im_end|>\n{% endif %}"
    "{% for message in messages %}"
    "<|im_start|>{{ message['role'] }}\n{{ message['content'] }}<|im_end|>\n"
    "{% endfor %}"
    "{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}"
)

# The sampler block. These key names are passed VERBATIM in the request body
# so an OpenAI-compatible backend (e.g. text-generation-webui) receives the
# exact parameter names it expects.
DEFAULT_SAMPLING = {
    "temperature": 1.15,
    "dynatemp_low": 0.85,
    "dynatemp_high": 1.3,
    "dynatemp_exponent": 1.01,
    "top_p": 0.75,
    "top_k": 10,
    "min_p": 0.05,
    "xtc_probability": 0.3,
    "dry_multiplier": 0.9,
    "frequency_penalty": 0.05,
    "repetition_penalty_range": 1152,
    "dynamic_temperature": True,
    "temperature_last": False,
}

DEFAULT_SETTINGS = {
    # ---- Link / endpoint ----
    "endpoint": "http://10.0.0.113:5000/v1",
    "api_key": "",
    "model": "Omnibrain-UE",
    "username": "User",

    # ---- Context window management ----
    "context_size": 8196,          # total context window in tokens
    "context_threshold": 95,       # begin cropping at this % of fullness
    "chars_per_token": 4.0,        # token estimator ratio (fallback)

    # ---- System prompt ----
    "system_message": (
        "You are OMNIBRAIN, the cognition core of a private terminal. "
        "You are precise, candid, and helpful."
    ),

    # ---- Generation ----
    "max_tokens": 1024,            # response cap; also reserved from context
    "stream": True,

    # ---- Sampling ----
    "use_endpoint_sampler_defaults": False,  # if True, omit samplers entirely
    "sampling": dict(DEFAULT_SAMPLING),
    "extra_params": {},            # advanced JSON passthrough (verbatim keys)

    # ---- Thinking / reasoning models ----
    "enable_thinking": True,       # if False, append the "off" directive
    "show_thinking": True,         # reveal the <think> block in the UI
    "think_open_tag": "<think>",
    "think_close_tag": "</think>",
    "think_off_directive": "/no_think",
    "think_on_directive": "",

    # ---- Speech / TTS ----
    # Which voice engine drives spoken output:
    #   "noise" — the animal-crossing-style streaming blip synth (client-side)
    #   "piper" — neural Piper TTS, synthesised per block on the server
    "tts_engine": "noise",
    "voice_enabled": False,         # master playback toggle (ONLY gates playback)
    "tts_volume": 0.85,             # 0..1 output gain (both engines)
    "tts_skip_think": True,         # never speak <think> reasoning aloud
    # -- noise engine --
    "noise_waveform": "square",     # square | sine | triangle | sawtooth
    "noise_pitch": 320,             # base blip frequency (Hz)
    "noise_pitch_variance": 90,     # random ± jitter per blip (Hz)
    "noise_speed": 2,               # emit one blip every N non-space characters
    # -- piper engine --
    "piper_voice": "",              # selected voice id (filename stem, no .onnx)
    "piper_length_scale": 1.0,      # speaking rate (lower = faster)

    # ---- Image input (vision) ----
    # Images are compressed in the browser before transmission: the longest
    # edge is scaled down to `image_max_dim` and the result re-encoded as JPEG
    # at `image_quality`. This keeps both the socket payload and the model's
    # context cost in check. Set image_max_dim to 0 to keep the original size.
    "image_input_enabled": True,    # show the attach-image control in the composer
    "image_max_dim": 1280,          # longest edge after client-side downscale (px)
    "image_quality": 0.82,          # JPEG re-encode quality (0..1)

    # ---- Display ----
    "show_generation_info": False,  # per-message gen stats, hidden by default

    # ---- Prompt format ----
    "use_custom_template": False,   # use Jinja template + /v1/completions
    "chat_template": DEFAULT_CHAT_TEMPLATE,

    # ---- Network (LAN sync) ----
    "lan_visible": True,            # accept connections from the LAN
    "access_token": "",            # optional shared secret (blank = none)

    # ---- MCP tools (Model Context Protocol) ----
    # The whole feature is opt-in: with mcp_enabled False no tools are ever sent
    # and generation is a single plain turn, exactly as before. Turn it on (Tools
    # tab) to let the model call tools from running MCP servers.
    "mcp_enabled": False,
    # LM Studio-style stored config: { name: {command, args, env?} }. Saved as a
    # JSON document from the Tools tab; servers are spawned on demand.
    "mcp_servers": dict(DEFAULT_MCP_SERVERS),
    # Per-tool master enable, keyed "server::tool". Absent key defaults to True.
    "mcp_tool_enabled": {},
    # Safety cap on the think→call→observe loop within a single user turn.
    "mcp_max_iterations": 8,
}

# Fields that, when changed, should re-test the endpoint link.
LINK_FIELDS = {"endpoint", "api_key", "model"}

DEFAULT_STATE = {
    "settings": dict(DEFAULT_SETTINGS),
    "active_id": None,
    "sessions": {},
}

# Server bind. The host is always 0.0.0.0 so the LAN *can* reach it; the
# `lan_visible` setting gates whether non-loopback clients are accepted at
# runtime (see server.access control). Port is overridable via env.
HOST = "0.0.0.0"
PORT = int(os.environ.get("OMNIBRAIN_PORT", "5005"))
