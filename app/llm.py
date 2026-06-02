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
    total = 0
    if isinstance(content, list):
        # Vision-style content: a list of {type: text|image_url, …} parts.
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                total += estimate_tokens(part.get("text", "") or "", chars_per_token)
            elif part.get("type") == "image_url":
                total += IMAGE_TOKEN_COST
    else:
        total += estimate_tokens(content or "", chars_per_token)

    # Tool-call requests (assistant) and the matching results carry weight too.
    # Accept both the normalized store shape ({name, arguments}) and the OpenAI
    # wire shape ({function: {name, arguments}}).
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
        total += estimate_tokens((fn.get("name") or "") + (fn.get("arguments") or ""), chars_per_token)
        total += 4
    return total + 4


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
            # Replay prior tool calls so the model sees its own action history.
            tcs = m.get("tool_calls")
            if tcs:
                entry["tool_calls"] = [{
                    "id": tc.get("id") or f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": tc.get("name", "") or "",
                        "arguments": tc.get("arguments") or "{}",
                    },
                } for i, tc in enumerate(tcs)]
            out.append(entry)
        elif role == "tool":
            # An observation returned to the model, bound to its call by id.
            entry = {
                "role": "tool",
                "tool_call_id": m.get("tool_call_id") or "",
                "content": m.get("content", "") or "",
            }
            if m.get("name"):
                entry["name"] = m["name"]
            out.append(entry)
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

    # Never lead with an orphaned tool result: a `tool` message is only valid
    # immediately after the assistant `tool_calls` that produced it, so if the
    # cut landed on one, advance past the dangling results to the next real turn.
    while start < n - 1 and history[start].get("role") == "tool":
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
def tool_system_note(tools: list) -> str:
    """A short natural-language briefing appended to the system prompt when tools
    are advertised.

    The OpenAI ``tools`` array alone is enough for strong models, but many local
    backends only reliably *use* tools when the system prompt also tells them the
    tools exist and how to invoke them. We therefore list the enabled tools by
    name and spell out the calling contract — call by emitting a function call
    (not prose), match the JSON schema, send ``{}`` for a no-argument tool — plus
    the one ordering rule the browser tools need (navigate before you read a
    page) so the model stops reaching for a snapshot/screenshot of a blank tab.
    """
    names = []
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        name = (fn or {}).get("name") if isinstance(fn, dict) else None
        if name:
            names.append(name)
    if not names:
        return ""
    note = [
        "# Tools",
        "You can call these tools to take real actions: " + ", ".join(names) + ".",
        "Invoke a tool by emitting an actual tool/function call — never describe "
        "the call in prose. Use the exact tool name and pass arguments as JSON "
        "matching that tool's schema; send an empty object {} when a tool takes "
        "no arguments. After a tool returns, use its result to keep working or to "
        "write your final answer.",
    ]
    if any(n.startswith("browser_") for n in names):
        note.append(
            "For browser tools, open a page with browser_navigate before reading "
            "it; use browser_snapshot (a structured accessibility tree) to read or "
            "act on the page, and reserve browser_take_screenshot for when an "
            "actual image is needed."
        )
    return "\n".join(note)


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
                  extra_user: str | None = None, extra_images: list | None = None,
                  tools: list | None = None):
    """
    Returns (url, body, headers, debug) ready for a streaming POST.

    `messages_store` is the stored session list (no system row). `extra_user`,
    if provided, is appended as a fresh user turn before cropping; `extra_images`
    attaches that turn's images as a vision content array. `tools`, if given, is
    the OpenAI `tools` array advertised to the model (chat-completions mode only;
    the raw /v1/completions template path cannot carry tool schemas).
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
    # Brief the model on the tools it may call. Raw /v1/completions (custom
    # template) mode can't carry a `tools` array, so the note only applies — and
    # is only true — on the chat-completions path. Folding it into the system
    # message (before cropping) keeps it token-counted and never dropped.
    if tools and not settings.get("use_custom_template", False):
        note = tool_system_note(tools)
        if note:
            sys_text = (sys_text + "\n\n" + note).strip() if sys_text else note
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
        # Advertise tools (function-calling) so the model can request a call.
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

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
def _assemble_tool_calls(acc: dict) -> list[dict]:
    """Turn the per-index streaming accumulator into ordered, normalized calls."""
    calls = []
    for idx in sorted(acc):
        slot = acc[idx]
        name = (slot.get("name") or "").strip()
        if not name:
            continue
        calls.append({
            "id": slot.get("id") or f"call_{idx}",
            "name": name,
            "arguments": slot.get("arguments") or "",
        })
    return calls


def stream_completion(messages_store, settings, user_text, on_delta, stop_flag=None,
                      user_images=None, tools=None):
    """
    Drive a streaming generation.

    `on_delta(text)` is called for every content chunk. Returns a result dict
    with the full raw text, the cleaned text, reasoning, any tool calls the model
    requested, and metadata. `user_images`, if given, attach to the latest user
    turn (vision input). `tools` advertises callable functions to the model.
    """
    url, body, headers, debug = build_request(messages_store, settings, user_text,
                                               user_images, tools=tools)
    op = settings.get("think_open_tag", "<think>")
    cl = settings.get("think_close_tag", "</think>")
    is_chat = not settings.get("use_custom_template", False)

    raw = []
    tool_acc: dict[int, dict] = {}   # index -> {id, name, arguments}
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
            delta_obj = choice.get("delta") or {}
            delta = delta_obj.get("content")
            # Tool-call fragments arrive interleaved with content; the function
            # name lands once up front, then arguments stream in piece by piece,
            # each tagged with a stable `index`.
            for tc in delta_obj.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
        else:
            delta = choice.get("text")
        if delta:
            raw.append(delta)
            on_delta(delta)

    elapsed = max(1e-6, time.time() - t0)
    raw_text = "".join(raw)
    tool_calls = _assemble_tool_calls(tool_acc)
    clean_text = strip_think(raw_text, op, cl)
    think_text = extract_think(raw_text, op, cl)
    had_think = has_think(raw_text, op)

    cpt = settings.get("chars_per_token", 4.0)
    completion_tokens = usage.get("completion_tokens") or estimate_tokens(raw_text, cpt)
    prompt_tokens = usage.get("prompt_tokens") or debug["prompt_tokens"]

    # When the model asks for tools the reported reason is "tool_calls"; surface
    # that explicitly even if the backend omitted it.
    if tool_calls and not finish_reason:
        finish_reason = "tool_calls"

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
        "tool_calls": tool_calls,
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
