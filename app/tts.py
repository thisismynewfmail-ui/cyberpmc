"""
Speech synthesis — Piper TTS backend + per-block streaming pipeline.

Two voice engines drive spoken output in OMNIBRAIN:

* "noise" — an animal-crossing-style blip synth. This is entirely client side
  (Web Audio); the server is not involved. Nothing here runs for it.
* "piper" — neural Piper TTS (``pip install piper-tts``). Synthesis happens on
  the server, one *block* (sentence/clause) at a time as the model streams, so
  the first words can be spoken while the rest of the reply is still generating.
  Each block is rendered to a small WAV and pushed to the screens to play in
  order.

Design notes
------------
* A single Piper voice is held in memory at a time. Switching voices, disabling
  voice feedback, or leaving the Piper engine unloads it and runs ``gc`` so the
  model's memory is released promptly (``unload``).
* All synthesis is serialised behind one lock, which also keeps only one model
  resident and bounds memory while previews / live blocks are produced.
* Everything degrades gracefully: if ``piper`` is not installed or a voice fails
  to load, calls raise/return empty and the UI falls back to the noise engine.
"""

from __future__ import annotations

import gc
import io
import os
import queue
import re
import threading
import wave

from . import config

# --------------------------------------------------------------------------
# Piper availability + single-voice model cache
# --------------------------------------------------------------------------
_lock = threading.RLock()
_loaded = {"id": None, "voice": None}

_PIPER_OK: bool | None = None


def piper_available() -> bool:
    """True if the ``piper`` package can be imported. Cached after first check."""
    global _PIPER_OK
    if _PIPER_OK is not None:
        return _PIPER_OK
    try:
        import piper  # noqa: F401
        _PIPER_OK = True
    except Exception:
        try:
            import piper.voice  # noqa: F401
            _PIPER_OK = True
        except Exception:
            _PIPER_OK = False
    return _PIPER_OK


def _load_piper_voice(onnx_path: str, config_path: str | None):
    # The class moved across versions; try both import paths.
    try:
        from piper import PiperVoice  # type: ignore
    except Exception:
        from piper.voice import PiperVoice  # type: ignore
    return PiperVoice.load(onnx_path, config_path=config_path)


def _safe_id(voice_id: str) -> str:
    """Strip any path components — voice ids are bare filename stems."""
    return os.path.basename(voice_id or "").strip()


def _voice_paths(voice_id: str):
    """Return (onnx_path, config_path) for a voice id, or None if missing."""
    vid = _safe_id(voice_id)
    if not vid:
        return None
    onnx = os.path.join(config.VOICES_DIR, vid + ".onnx")
    if not os.path.exists(onnx):
        return None
    for cfg in (onnx + ".json", os.path.join(config.VOICES_DIR, vid + ".json")):
        if os.path.exists(cfg):
            return onnx, cfg
    # Piper can often infer config, but we prefer an explicit one.
    return onnx, None


def _unload_locked():
    if _loaded["voice"] is not None:
        _loaded["voice"] = None
        _loaded["id"] = None
        gc.collect()


def unload():
    """Drop the resident Piper voice and free its memory."""
    with _lock:
        _unload_locked()


def _get_voice(voice_id: str):
    vid = _safe_id(voice_id)
    with _lock:
        if _loaded["id"] == vid and _loaded["voice"] is not None:
            return _loaded["voice"]
        _unload_locked()  # clean switch — release the previous model first
        paths = _voice_paths(vid)
        if not paths:
            raise FileNotFoundError(f"voice not found: {vid}")
        onnx, cfg = paths
        voice = _load_piper_voice(onnx, cfg)
        _loaded["id"] = vid
        _loaded["voice"] = voice
        return voice


# --------------------------------------------------------------------------
# Low-level synthesis
# --------------------------------------------------------------------------
def _synth_pcm(voice, text: str):
    """Return (pcm_int16_bytes, sample_rate) across Piper API generations."""
    sample_rate = 22050
    try:
        sample_rate = int(getattr(voice.config, "sample_rate", 22050))
    except Exception:
        pass

    # Newer API: synthesize(text) -> generator of AudioChunk / bytes.
    try:
        result = voice.synthesize(text)
        buf = bytearray()
        produced = False
        for chunk in result:
            produced = True
            if hasattr(chunk, "audio_int16_bytes"):
                buf += chunk.audio_int16_bytes
                sr = getattr(chunk, "sample_rate", None)
                if sr:
                    sample_rate = int(sr)
            elif isinstance(chunk, (bytes, bytearray)):
                buf += chunk
        if produced:
            return bytes(buf), sample_rate
    except TypeError:
        pass  # old signature wanted a wav file arg -> fall through
    except AttributeError:
        pass

    # Older API: synthesize_stream_raw(text) -> generator of raw PCM bytes.
    if hasattr(voice, "synthesize_stream_raw"):
        buf = bytearray()
        for chunk in voice.synthesize_stream_raw(text):
            buf += chunk
        return bytes(buf), sample_rate

    raise RuntimeError("unsupported Piper version: no known synthesize method")


def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    bio = io.BytesIO()
    with wave.open(bio, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return bio.getvalue()


def synthesize_to_wav(text: str, voice_id: str):
    """Render ``text`` with the given voice -> (wav_bytes, sample_rate)."""
    text = (text or "").strip()
    if not text:
        return b"", 0
    with _lock:  # serialise: one model, one synth at a time
        voice = _get_voice(voice_id)
        pcm, sr = _synth_pcm(voice, text)
    if not pcm:
        return b"", sr
    return _pcm_to_wav(pcm, sr), sr


# --------------------------------------------------------------------------
# Voice discovery + previews
# --------------------------------------------------------------------------
def preview_path(voice_id: str) -> str:
    return os.path.join(config.VOICE_PREVIEW_DIR, _safe_id(voice_id) + ".wav")


def list_voices() -> list[dict]:
    """Every installed voice: {id, name, has_preview}."""
    out = []
    vdir = config.VOICES_DIR
    if not os.path.isdir(vdir):
        return out
    for fn in sorted(os.listdir(vdir)):
        if not fn.endswith(".onnx"):
            continue
        vid = fn[: -len(".onnx")]
        out.append({
            "id": vid,
            "name": vid.replace("_", " ").replace("-", " "),
            "has_preview": os.path.exists(preview_path(vid)),
        })
    return out


def voices_payload(settings: dict) -> dict:
    voices = list_voices()
    for v in voices:
        v["preview_url"] = f"/tts/preview/{v['id']}.wav" if v["has_preview"] else ""
    return {
        "voices": voices,
        "piper_available": piper_available(),
        "selected": settings.get("piper_voice", ""),
        "engine": settings.get("tts_engine", "noise"),
        "voice_enabled": bool(settings.get("voice_enabled", False)),
    }


def generate_missing_previews() -> list[str]:
    """Render a sample for every voice that has no preview yet. Returns new ids."""
    made: list[str] = []
    os.makedirs(config.VOICE_PREVIEW_DIR, exist_ok=True)
    for v in list_voices():
        if v["has_preview"]:
            continue
        try:
            wav, _sr = synthesize_to_wav(config.PREVIEW_TEXT, v["id"])
            if wav:
                with open(preview_path(v["id"]), "wb") as f:
                    f.write(wav)
                made.append(v["id"])
        except Exception as e:  # noqa: BLE001
            print(f"[tts] preview failed for {v['id']}: {e}")
    return made


# --------------------------------------------------------------------------
# Per-block streaming pipeline
# --------------------------------------------------------------------------
_MD_RE = re.compile(r"[*_`#>~|]+")
_WS_RE = re.compile(r"\s+")
_BOUNDARY = set(".!?…\n")
_AFTER = set(" \n\t\"'”’)]}")


def _clean_for_speech(text: str) -> str:
    """Strip markdown noise so the voice doesn't read asterisks and hashes."""
    text = _MD_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _first_boundary(s: str) -> int:
    """Index of the first confidently-complete clause end, or -1."""
    for i, ch in enumerate(s):
        if ch == "\n":
            return i
        if ch in _BOUNDARY:
            if ch == "…":
                return i
            nxt = s[i + 1] if i + 1 < len(s) else ""
            if nxt and nxt in _AFTER:  # avoid splitting "3.14" mid-number
                return i
    return -1


def _strip_think_nostrip(text: str, op: str, cl: str) -> str:
    """Remove complete + dangling think spans WITHOUT trimming whitespace.

    Keeping the surrounding whitespace makes the cleaned text grow monotonically
    as a stream arrives, so a simple prefix-diff yields the newly speakable text.
    """
    pat = re.compile(re.escape(op) + r".*?" + re.escape(cl), re.DOTALL | re.IGNORECASE)
    text = pat.sub("", text)
    low = text.lower()
    pos = low.find(op.lower())
    if pos != -1 and cl.lower() not in low:
        text = text[:pos]
    return text


def _holdback(s: str, tags) -> int:
    """Length of the longest suffix of ``s`` that is a proper prefix of a tag.

    Those trailing characters might still grow into a think tag as more of the
    stream arrives, so they must be held back rather than spoken.
    """
    sl = s.lower()
    best = 0
    for tag in tags:
        tl = tag.lower()
        for k in range(min(len(s), len(tag) - 1), 0, -1):
            if sl[-k:] == tl[:k]:
                if k > best:
                    best = k
                break
    return best


class _ThinkFilter:
    """Streaming filter that yields only speakable (non-reasoning) text.

    Reasoning spans are removed, and any trailing fragment that could still
    become a think tag is withheld until the next delta resolves it — so a
    half-arrived ``<thi`` is never spoken.
    """

    def __init__(self, open_tag: str, close_tag: str, skip: bool):
        self.op = open_tag or "<think>"
        self.cl = close_tag or "</think>"
        self.skip = skip
        self._raw = ""
        self._emitted = 0  # chars of the cleaned stream already returned

    def feed(self, delta: str) -> str:
        if not self.skip:
            return delta
        self._raw += delta
        clean = _strip_think_nostrip(self._raw, self.op, self.cl)
        safe = len(clean) - _holdback(clean, (self.op, self.cl))
        if safe <= self._emitted:
            self._emitted = min(self._emitted, len(clean))
            return ""
        new = clean[self._emitted:safe]
        self._emitted = safe
        return new

    def flush(self) -> str:
        """End of stream: emit whatever is left, dropping any partial tag tail."""
        if not self.skip:
            return ""
        clean = _strip_think_nostrip(self._raw, self.op, self.cl)
        end = len(clean) - _holdback(clean, (self.op, self.cl))
        new = clean[self._emitted:end] if end > self._emitted else ""
        self._emitted = len(clean)
        return new


class TTSStreamer:
    """
    Accumulates streamed deltas, flushes complete clauses as they arrive, and
    synthesises each one on a single worker thread so audio is produced (and
    emitted) strictly in order. ``emit_audio(seq, wav_bytes, sample_rate)`` is
    invoked for every rendered block.
    """

    MAXLEN = 220  # force a flush if no clause boundary appears for this long

    def __init__(self, settings: dict, emit_audio):
        self.voice_id = settings.get("piper_voice", "")
        self.emit_audio = emit_audio
        self._filter = _ThinkFilter(
            settings.get("think_open_tag", "<think>"),
            settings.get("think_close_tag", "</think>"),
            settings.get("tts_skip_think", True),
        )
        self._buf = ""
        self._seq = 0
        self._stopped = False
        self._q: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    # -- producer side (called from the generation thread) --
    def feed(self, delta: str):
        if self._stopped:
            return
        new = self._filter.feed(delta)
        if not new:
            return
        self._buf += new
        self._drain()

    def _drain(self):
        while True:
            if len(self._buf) >= self.MAXLEN and _first_boundary(self._buf) == -1:
                cut = self._buf.rfind(" ", 0, self.MAXLEN)
                if cut <= 0:
                    cut = self.MAXLEN
                self._enqueue(self._buf[:cut])
                self._buf = self._buf[cut:]
                continue
            i = _first_boundary(self._buf)
            if i == -1:
                break
            self._enqueue(self._buf[: i + 1])
            self._buf = self._buf[i + 1:]

    def _enqueue(self, block: str):
        text = _clean_for_speech(block)
        if not text:
            return
        self._seq += 1
        self._q.put((self._seq, text))

    def finish(self):
        """Flush the final partial clause and let the worker drain + exit."""
        if self._stopped:
            return
        tail = self._filter.flush()
        if tail:
            self._buf += tail
        rest = _clean_for_speech(self._buf)
        self._buf = ""
        if rest:
            self._seq += 1
            self._q.put((self._seq, rest))
        self._q.put(None)

    def stop(self):
        """Abort: drop anything still queued and stop the worker."""
        self._stopped = True
        try:
            while True:
                self._q.get_nowait()
                self._q.task_done()
        except queue.Empty:
            pass
        self._q.put(None)

    def close(self):
        try:
            self._worker.join(timeout=12)
        except Exception:
            pass

    # -- consumer side --
    def _run(self):
        while True:
            item = self._q.get()
            if item is None:
                self._q.task_done()
                break
            seq, text = item
            if not self._stopped:
                try:
                    wav, sr = synthesize_to_wav(text, self.voice_id)
                    if wav and not self._stopped:
                        self.emit_audio(seq, wav, sr)
                except Exception as e:  # noqa: BLE001
                    print(f"[tts] synth failed: {e}")
            self._q.task_done()
