"""
The cognition engine.

Responsibilities
----------------
* Estimate token counts (uses tiktoken if present, else a char ratio).
* Crop the conversation to fit the context window WITHOUT ever dropping the
  system message, removing whole messages from the oldest end.
* Build a correctly-worded request body for an OpenAI-compatible endpoint,
  passing sampler keys verbatim.
* Stream the response, surfacing token deltas and a final metadata block.
* Strip <think>...</think> spans so reasoning is never replayed into history.
"""

from __future__ import annotations

import json
import re
import time
import requests

try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:  # tiktoken optional
    _ENC = None


# --------------------------------------------------------------------------
# Token accounting
# --------------------------------------------------------------------------
def estimate_tokens(text: str, chars_per_token: float = 4.0) -> int:
    """Best-effort token count for a single string."""
    if not text:
        return 0
    if _ENC is not None:
        try:
            return len(_ENC.encode(text))
        except Exception:
            pass
    ratio = chars_per_token if chars_per_token and chars_per_token > 0 else 4.0
    return max(1, int(round(len(text) / ratio)))


# Rough budget charged per attached image when accounting for context fullness.
# Vision models bill images by tile; this is a deliberately conservative single
# figure so the fullness meter never under-reports a picture-heavy prompt.
IMAGE_TOKEN_COST = 765


def message_tokens(msg: dict, chars_per_token: float = 4.0) -> int:
    """Token cost of one chat message incl. a small role/formatting overhead."""
    content = msg.get("content", "")
    if isinstance(content, list):
        # Vision-style content: a list of {type: text|image_url, …} parts.
        total = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                total += estimate_tokens(part.get("text", "") or "", chars_per_token)
            elif part.get("type") == "image_url":
                total += IMAGE_TOKEN_COST
        return total + 4
    return estimate_tokens(content or "", chars_per_token) + 4


def count_prompt_tokens(messages: list[dict], chars_per_token: float = 4.0) -> int:
    total = sum(message_tokens(m, chars_per_token) for m in messages)
    return total + 3  # priming tokens for the assistant turn


# --------------------------------------------------------------------------
# Thinking-tag utilities
# --------------------------------------------------------------------------
def _think_regex(open_tag: str, close_tag: str) -> re.Pattern:
    return re.compile(
        re.escape(open_tag) + r".*?" + re.escape(close_tag),
        re.DOTALL | re.IGNORECASE,
    )


def strip_think(text: str, open_tag="<think>", close_tag="</think>") -> str:
    """Remove every complete think span; also drop a dangling, unclosed one."""
    if not text:
        return text
    text = _think_regex(open_tag, close_tag).sub("", text)
    # A think block that never closed (e.g. truncated generation).
    open_pos = text.lower().find(open_tag.lower())
    if open_pos != -1 and close_tag.lower() not in text.lower():
        text = text[:open_pos]
    return text.strip()


def extract_think(text: str, open_tag="<think>", close_tag="</think>") -> str:
    """Pull out the reasoning content for optional display."""
    if not text:
        return ""
    spans = re.findall(
        re.escape(open_tag) + r"(.*?)" + re.escape(close_tag),
        text,
        re.DOTALL | re.IGNORECASE,
    )
    out = "\n".join(s.strip() for s in spans).strip()
    # Include a dangling unclosed reasoning block too.
    low = text.lower()
    op = low.find(open_tag.lower())
    if op != -1 and close_tag.lower() not in low:
        tail = text[op + len(open_tag):].strip()
        out = (out + "\n" + tail).strip() if out else tail
    return out


def has_think(text: str, open_tag="<think>") -> bool:
    return bool(text) and open_tag.lower() in text.lower()


# --------------------------------------------------------------------------
# Context cropping
# --------------------------------------------------------------------------
def _user_content(text: str, images: list, settings: dict):
    """
    Build the `content` for a user turn.

    With attached images and the default (chat-completions) path this becomes
    the OpenAI vision array — a text part followed by one `image_url` part per
    image. Raw /v1/completions (custom Jinja template) cannot carry images, so
    there we fall back to the plain text string.
    """
    if not images or settings.get("use_custom_template", False):
        return text or ""
    parts = []
    if text:
        parts.append({"type": "text", "text": text})
    for url in images:
        if isinstance(url, str) and url:
            parts.append({"type": "image_url", "image_url": {"url": url}})
    # If everything fell out (no text, no valid images) keep the text string.
    return parts or (text or "")


def build_history(messages: list[dict], settings: dict) -> list[dict]:
    """
    Turn stored session messages into clean role/content dicts for the model.

    * System messages are preserved verbatim.
    * Assistant messages have their think spans stripped (reasoning is never
      replayed into the context).
    * User messages carrying images are encoded as a vision content array.
    """
    op = settings.get("think_open_tag", "<think>")
    cl = settings.get("think_close_tag", "</think>")
    out = []
    for m in messages:
        role = m.get("role", "user")
        if role == "assistant":
            content = m.get("clean")
            if content is None:
                content = strip_think(m.get("content", ""), op, cl)
            entry = {"role": role, "content": content}
        else:
            content = _user_content(m.get("content", ""), m.get("images") or [], settings)
            entry = {"role": role, "content": content}
            if role == "user" and settings.get("username"):
                entry["name"] = settings["username"]
        out.append(entry)
    return out


def crop_to_context(system_msg: dict, history: list[dict], settings: dict):
    """
    Drop whole messages from the OLDEST end until the prompt fits the window.

    Returns (kept_messages, dropped_count, start_index, prompt_tokens) where
    `start_index` indexes into `history` (the first message still in context).
    The system message is never dropped, and at least the most recent message
    is always retained so the chat never empties out completely.
    """
    cpt = settings.get("chars_per_token", 4.0)
    ctx = max(256, int(settings.get("context_size", 8196)))
    pct = min(100, max(1, int(settings.get("context_threshold", 95)))) / 100.0

    reserve = settings.get("max_tokens", 0) or 0
    reserve = max(0, int(reserve))
    # Token budget for the *prompt* portion of the window.
    budget = int(ctx * pct) - reserve
    if budget < 256:
        budget = max(256, int(ctx * pct))

    sys_cost = message_tokens(system_msg, cpt) if system_msg else 0

    start = 0
    n = len(history)
    while start < n - 1:  # always keep the last message
        prompt = [system_msg] + history[start:] if system_msg else history[start:]
        if count_prompt_tokens(prompt, cpt) <= budget:
            break
        start += 1

    kept = ([system_msg] if system_msg else []) + history[start:]
    prompt_tokens = count_prompt_tokens(kept, cpt)
    return kept, start, prompt_tokens


def context_report(messages: list[dict], settings: dict) -> dict:
    """
    Lightweight read of where the context horizon currently sits, for the UI.
    Does not perform a request.
    """
    sys_text = settings.get("system_message", "") or ""
    system_msg = {"role": "system", "content": sys_text} if sys_text else None
    history = build_history(messages, settings)
    _, start, prompt_tokens = crop_to_context(system_msg, history, settings)
    ctx = max(1, int(settings.get("context_size", 8196)))
    return {
        "tokens": prompt_tokens,
        "context_size": ctx,
        "pct": round(100.0 * prompt_tokens / ctx, 1),
        # Map history index back to the message list (history excludes system,
        # and the session message list contains no system rows either).
        "start_index": start,
        "dropped": start,
    }


# --------------------------------------------------------------------------
# Request building
# --------------------------------------------------------------------------
def _render_template(template: str, system_message: str, messages: list[dict]) -> str:
    from jinja2 import Environment, BaseLoader
    env = Environment(loader=BaseLoader(), trim_blocks=False, lstrip_blocks=False)
    tmpl = env.from_string(template)
    return tmpl.render(
        system_message=system_message,
        messages=messages,
        add_generation_prompt=True,
    )


def build_request(messages_store: list[dict], settings: dict,
                  extra_user: str | None = None, extra_images: list | None = None):
    """
    Returns (url, body, headers, debug) ready for a streaming POST.

    `messages_store` is the stored session list (no system row). `extra_user`,
    if provided, is appended as a fresh user turn before cropping; `extra_images`
    attaches that turn's images as a vision content array.
    """
    op = settings.get("think_open_tag", "<think>")

    # Compose the working message list.
    history = build_history(messages_store, settings)
    if extra_user is not None:
        user_text = extra_user
        # Apply the thinking directive to the latest user turn.
        if not settings.get("enable_thinking", True):
            d = settings.get("think_off_directive", "")
            if d:
                user_text = f"{user_text}\n\n{d}".strip()
        else:
            d = settings.get("think_on_directive", "")
            if d:
                user_text = f"{user_text}\n\n{d}".strip()
        entry = {"role": "user", "content": _user_content(user_text, extra_images or [], settings)}
        if settings.get("username"):
            entry["name"] = settings["username"]
        history.append(entry)

    sys_text = settings.get("system_message", "") or ""
    system_msg = {"role": "system", "content": sys_text} if sys_text else None

    kept, start, prompt_tokens = crop_to_context(system_msg, history, settings)

    base = settings.get("endpoint", "").rstrip("/")
    headers = {"Content-Type": "application/json"}
    if settings.get("api_key"):
        headers["Authorization"] = f"Bearer {settings['api_key']}"

    body = {
        "model": settings.get("model", ""),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    mt = settings.get("max_tokens", 0) or 0
    if int(mt) > 0:
        body["max_tokens"] = int(mt)

    # Sampler keys passed VERBATIM unless the user defers to endpoint defaults.
    if not settings.get("use_endpoint_sampler_defaults", False):
        for k, v in (settings.get("sampling") or {}).items():
            body[k] = v
        for k, v in (settings.get("extra_params") or {}).items():
            body[k] = v

    if settings.get("use_custom_template", False):
        url = f"{base}/completions"
        body["prompt"] = _render_template(
            settings.get("chat_template", ""), sys_text, kept
        )
    else:
        url = f"{base}/chat/completions"
        body["messages"] = kept

    debug = {
        "prompt_tokens": prompt_tokens,
        "dropped": start,
        "kept_messages": len(kept),
        "mode": "completions" if settings.get("use_custom_template") else "chat",
    }
    return url, body, headers, debug


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------
def stream_completion(messages_store, settings, user_text, on_delta, stop_flag=None,
                      user_images=None):
    """
    Drive a streaming generation.

    `on_delta(text)` is called for every content chunk. Returns a result dict
    with the full raw text, the cleaned text, reasoning, and metadata.
    `user_images`, if given, attach to the latest user turn (vision input).
    """
    url, body, headers, debug = build_request(messages_store, settings, user_text, user_images)
    op = settings.get("think_open_tag", "<think>")
    cl = settings.get("think_close_tag", "</think>")
    is_chat = not settings.get("use_custom_template", False)

    raw = []
    usage = {}
    finish_reason = None
    t0 = time.time()

    resp = requests.post(url, json=body, headers=headers, stream=True, timeout=(10, 600))
    resp.raise_for_status()

    for line in resp.iter_lines(decode_unicode=True):
        if stop_flag is not None and stop_flag():
            break
        if not line:
            continue
        if line.startswith("data:"):
            line = line[len("data:"):].strip()
        if line == "[DONE]":
            break
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue

        if chunk.get("usage"):
            usage = chunk["usage"]

        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]

        if is_chat:
            delta = (choice.get("delta") or {}).get("content")
        else:
            delta = choice.get("text")
        if delta:
            raw.append(delta)
            on_delta(delta)

    elapsed = max(1e-6, time.time() - t0)
    raw_text = "".join(raw)
    clean_text = strip_think(raw_text, op, cl)
    think_text = extract_think(raw_text, op, cl)
    had_think = has_think(raw_text, op)

    cpt = settings.get("chars_per_token", 4.0)
    completion_tokens = usage.get("completion_tokens") or estimate_tokens(raw_text, cpt)
    prompt_tokens = usage.get("prompt_tokens") or debug["prompt_tokens"]

    meta = {
        "model": settings.get("model", ""),
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "total_tokens": int(usage.get("total_tokens") or (prompt_tokens + completion_tokens)),
        "elapsed": round(elapsed, 2),
        "tokens_per_second": round(completion_tokens / elapsed, 1),
        "finish_reason": finish_reason or "stop",
        "dropped_messages": debug["dropped"],
        "mode": debug["mode"],
        "timestamp": time.time(),
    }
    return {
        "raw": raw_text,
        "clean": clean_text,
        "think": think_text,
        "has_think": had_think,
        "meta": meta,
    }


# --------------------------------------------------------------------------
# Connection test
# --------------------------------------------------------------------------
def test_link(settings: dict) -> dict:
    base = settings.get("endpoint", "").rstrip("/")
    if not base:
        return {"online": False, "detail": "No endpoint configured"}
    headers = {}
    if settings.get("api_key"):
        headers["Authorization"] = f"Bearer {settings['api_key']}"
    try:
        r = requests.get(f"{base}/models", headers=headers, timeout=5)
        if r.status_code == 200:
            models = []
            try:
                data = r.json()
                models = [m.get("id") for m in data.get("data", []) if m.get("id")]
            except Exception:
                pass
            return {"online": True, "detail": "Link established", "models": models}
        return {"online": False, "detail": f"HTTP {r.status_code}"}
    except requests.exceptions.Timeout:
        return {"online": False, "detail": "Timed out"}
    except requests.exceptions.ConnectionError:
        return {"online": False, "detail": "Connection refused"}
    except Exception as e:  # noqa: BLE001
        return {"online": False, "detail": str(e)[:120]}
