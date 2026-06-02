# OMNIBRAIN // COGNITION TERMINAL

A LAN-syncable chat terminal for any **OpenAI-compatible** endpoint (e.g.
text-generation-webui / oobabooga, llama.cpp server, vLLM, LM Studio…), wrapped
in an augmented-cyberpunk HUD. Every monitor or PC that opens the terminal
mirrors the **same** conversation and settings in real time — one shared
cognition core, many screens.

```
omnibrain/
├── python.py            ← launcher  (python python.py)
├── requirements.txt
├── app/
│   ├── config.py        factory defaults (endpoint, samplers, context, speech…)
│   ├── llm.py           engine: tokens, context cropping, think handling, streaming
│   ├── tts.py           speech: Piper TTS, per-block synth pipeline, voice cache
│   ├── state.py         thread-safe persistent store (data/state.json)
│   └── server.py        Flask + Socket.IO sync, access gating, streaming relay
├── static/
│   ├── css/style.css    the theme
│   └── js/app.js        client: sync, streaming render, animated core, voice
├── templates/index.html
├── voices/              Piper voice models (.onnx + .onnx.json) + previews/
├── data/                created at runtime (state.json) — your saved state
└── tests/               mock endpoint + integration test
```

## Quick start

```bash
pip install -r requirements.txt
python python.py
```

Then open **http://localhost:5005**. Override the port with
`OMNIBRAIN_PORT=8080 python python.py`.

By default the terminal talks to `http://10.0.0.113:5000/v1` with model
`Omnibrain-UE` and a blank API key. Change any of this under **SPEECH → LINK**
and press **TEST CONNECTION** — the core lights up when the link is live.
(The redone **SPEECH** tab holds both the voice settings and the cognition-engine
configuration, under a `◈ COGNITION ENGINE` divider.)

## Sharing across screens (LAN sync)

The server always binds to the LAN; the **NETWORK → LAN VISIBILITY** switch
decides whether remote machines are accepted (toggle it off for local-only).
On any other device on the same network open:

```
http://<this-machine-ip>:5005
```

Every connected screen shows the identical chat, active session, system prompt,
samplers, and settings. A change on one screen appears on all of them instantly.
Set an optional **ACCESS TOKEN** to require a shared secret to connect.

## How the engine behaves

- **Context window** (default `8196`): when fullness crosses the **crop
  threshold** (default `95%`) the *oldest whole messages* fall out of context
  one at a time, in the background. The system prompt is always pinned and the
  most recent message is always kept — conversations continue indefinitely with
  a clean cut at message boundaries (never mid-message). The terminal shows the
  live fullness meter and how many messages are currently past the horizon.
- **System prompt**: sent as the `system` role and applied by the chat
  template; never dropped by cropping.
- **Samplers**: every parameter is passed to the endpoint **verbatim**
  (`temperature`, `dynatemp_low/high/exponent`, `top_p`, `top_k`, `min_p`,
  `xtc_probability`, `dry_multiplier`, `frequency_penalty`,
  `repetition_penalty_range`, `dynamic_temperature`, `temperature_last`, …).
  Use **LOAD DEFAULTS** for the recommended profile, the **EXTRA PARAMETERS**
  box for anything else (also passed verbatim), or flip *use endpoint/model
  sampler defaults* to omit them entirely and let the model decide.
- **Thinking models**: text inside `<think>…</think>` is shown as a collapsible
  **COGNITION TRACE**. Turn **SHOW THINKING** off to hide it (a `◇` marker still
  flags that reasoning happened). Reasoning is stripped from history after every
  turn, so past thinking is never replayed into context. **ENABLE THINKING** off
  appends a configurable directive (default `/no_think`).
- **Generation info** (tokens, tok/s, elapsed, finish reason) sits under each
  reply — hidden by default, toggle under **SPEECH → DISPLAY**.
- **Prompt format**: default mode posts the `messages` array to
  `/v1/chat/completions` (the server applies its own template). Flip *use custom
  Jinja template* to render a template locally and post raw to
  `/v1/completions`.

## Image input (vision)

The composer has a small **image icon above the send button**. Click it (or
just **paste** an image into the input) to attach one or more pictures to the
next message — up to 8 per turn. Each image is **compressed in the browser
before it is sent**: the longest edge is scaled down to `image_max_dim`
(default `1280` px) and the result re-encoded as JPEG at `image_quality`
(default `0.82`), so the socket payload and the model's context cost both stay
small. Set `image_max_dim` to `0` to keep the original resolution.

Attached images appear as removable thumbnails in the composer tray before
transmit, then render inline in the transcript (click one to open it full
size). The default `/v1/chat/completions` path forwards them in the standard
OpenAI **vision** format (an `image_url` content part per image), so any
vision-capable endpoint receives them correctly. Each image is charged a flat
budget against the context-fullness meter. Raw `/v1/completions` (custom Jinja
template) mode cannot carry images, so there the text is sent on its own.

The control can be hidden entirely with `image_input_enabled: false` in
`data/state.json`.

## Speech / voice output

The **SPEECH** tab drives spoken output and offers two interchangeable engines,
selected with **VOICE ENGINE**:

- **NOISE SYNTH** — an animal-crossing-style blip per character as the reply
  streams in. It runs entirely in the browser (Web Audio), so there is zero
  synthesis latency. Tune the waveform, blip rate, base pitch and pitch jitter.
- **PIPER TTS** — neural speech via [Piper](https://github.com/rhasspy/piper)
  (`pip install piper-tts`). Each clause is synthesised **per block** on the
  server as the model streams, so the first sentence is spoken while the rest is
  still generating — the fastest possible first word. Reasoning inside the think
  tags is never spoken.

**ENABLE VOICE FEEDBACK** is the master switch and *only* gates playback —
turning it off stops audio immediately; switching engine or voice cleanly stops
playback and unloads any resident Piper model (`gc`), so memory is released
promptly. Only one Piper voice is held in memory at a time.

### Adding Piper voices

Drop a voice's `.onnx` and `.onnx.json` files into `voices/` (see
`voices/README.md`), then press **GENERATE PREVIEWS** — every voice that does
not yet have a sample gets one rendered and placed next to it, ready to ▶
audition. Pick a voice, enable voice feedback, and the core speaks. If Piper is
not installed the tab says so and the noise engine remains available.

## Sessions

Under **SESSIONS** you can create, switch, rename, duplicate, and delete chats.
Saving is automatic and persistent. Selecting a session switches every screen to
it. Per-message **COPY** / **DELETE** live on hover in the transcript.

## Testing

```bash
# terminal 1 — a fake streaming endpoint
python tests/mock_endpoint.py
# terminal 2 — the app pointed at it
OMNIBRAIN_PORT=5005 python python.py
# terminal 3 — the checks
OMNIBRAIN_PORT=5005 python tests/integration_test.py
```

The suite covers state sync, streaming, think-stripping, verbatim sampler
pass-through, context cropping, session ops, and on-disk persistence.

## Notes

- All state lives in `data/state.json` (atomic writes). Delete it to reset to
  factory defaults.
- The API key is never broadcast to clients or written until you set one.
- Token counts use `tiktoken` if installed, otherwise a configurable
  characters-per-token estimate.
- The bundled dev server is fine for a LAN. For a hardened deployment, run
  behind a production WSGI/ASGI server.
