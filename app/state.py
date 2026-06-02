"""
Persistent, thread-safe state.

A single server process is the source of truth for every connected screen.
All settings and chat sessions live here, mirrored to data/state.json via an
atomic temp-file swap so a crash mid-write can't corrupt the store.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid

from . import config


def _now() -> float:
    return time.time()


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge patch into base (patch wins), returning base."""
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


class StateStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._state = copy.deepcopy(config.DEFAULT_STATE)
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        if os.path.exists(config.STATE_FILE):
            try:
                with open(config.STATE_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                # Merge saved settings onto defaults so new keys appear after
                # an upgrade, without discarding the user's stored values.
                merged = copy.deepcopy(config.DEFAULT_SETTINGS)
                _deep_merge(merged, saved.get("settings", {}))
                self._state["settings"] = merged
                self._state["sessions"] = saved.get("sessions", {})
                self._state["active_id"] = saved.get("active_id")
            except Exception as e:  # noqa: BLE001
                print(f"[state] could not load state file: {e}; using defaults")
        # Guarantee at least one session exists and is active.
        if not self._state["sessions"]:
            self._new_session_locked("Session 01")
        if self._state["active_id"] not in self._state["sessions"]:
            self._state["active_id"] = next(iter(self._state["sessions"]))
        self._save()

    def _save(self):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        tmp = config.STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, config.STATE_FILE)

    # -- settings ----------------------------------------------------------
    def get_settings(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state["settings"])

    def public_settings(self) -> dict:
        """Settings with the API key masked for broadcast safety."""
        with self._lock:
            s = copy.deepcopy(self._state["settings"])
            s["api_key_set"] = bool(s.get("api_key"))
            s["api_key"] = ""  # never broadcast the secret
            return s

    def update_settings(self, patch: dict) -> dict:
        with self._lock:
            # Ignore the masking helper if echoed back.
            patch = {k: v for k, v in patch.items() if k != "api_key_set"}
            # The MCP server map is REPLACED wholesale (not deep-merged) so that
            # removing a server from the JSON actually deletes it instead of the
            # stale entry lingering. Everything else deep-merges as usual.
            if "mcp_servers" in patch:
                self._state["settings"]["mcp_servers"] = copy.deepcopy(patch.pop("mcp_servers"))
            # Allow clearing the key only with an explicit empty string when the
            # caller actually sent the field; otherwise leave it untouched.
            _deep_merge(self._state["settings"], patch)
            self._save()
            return self.public_settings()

    # -- sessions ----------------------------------------------------------
    def _new_session_locked(self, name: str | None = None) -> str:
        sid = _gen_id("sess")
        idx = len(self._state["sessions"]) + 1
        self._state["sessions"][sid] = {
            "id": sid,
            "name": name or f"Session {idx:02d}",
            "created": _now(),
            "updated": _now(),
            "messages": [],
            # Per-session MCP server toggles, keyed by server name. An absent key
            # means "active" so newly started servers are usable everywhere by
            # default; the sidebar menu flips these per session.
            "mcp_active": {},
        }
        self._state["active_id"] = sid
        return sid

    def new_session(self, name: str | None = None) -> str:
        with self._lock:
            sid = self._new_session_locked(name)
            self._save()
            return sid

    def list_sessions(self) -> list[dict]:
        with self._lock:
            out = []
            for s in self._state["sessions"].values():
                out.append({
                    "id": s["id"],
                    "name": s["name"],
                    "created": s["created"],
                    "updated": s["updated"],
                    "count": len(s["messages"]),
                })
            out.sort(key=lambda x: x["updated"], reverse=True)
            return out

    def get_session(self, sid: str) -> dict | None:
        with self._lock:
            s = self._state["sessions"].get(sid)
            return copy.deepcopy(s) if s else None

    def active_id(self) -> str | None:
        with self._lock:
            return self._state["active_id"]

    def set_active(self, sid: str) -> bool:
        with self._lock:
            if sid in self._state["sessions"]:
                self._state["active_id"] = sid
                self._save()
                return True
            return False

    def rename_session(self, sid: str, name: str) -> bool:
        with self._lock:
            s = self._state["sessions"].get(sid)
            if not s:
                return False
            s["name"] = name.strip() or s["name"]
            s["updated"] = _now()
            self._save()
            return True

    def duplicate_session(self, sid: str) -> str | None:
        with self._lock:
            s = self._state["sessions"].get(sid)
            if not s:
                return None
            new_id = _gen_id("sess")
            self._state["sessions"][new_id] = {
                "id": new_id,
                "name": f"{s['name']} (copy)",
                "created": _now(),
                "updated": _now(),
                "messages": copy.deepcopy(s["messages"]),
                "mcp_active": copy.deepcopy(s.get("mcp_active", {})),
            }
            self._save()
            return new_id

    def delete_session(self, sid: str) -> bool:
        with self._lock:
            if sid not in self._state["sessions"]:
                return False
            del self._state["sessions"][sid]
            if not self._state["sessions"]:
                self._new_session_locked("Session 01")
            if self._state["active_id"] == sid:
                self._state["active_id"] = next(iter(self._state["sessions"]))
            self._save()
            return True

    def clear_session(self, sid: str) -> bool:
        with self._lock:
            s = self._state["sessions"].get(sid)
            if not s:
                return False
            s["messages"] = []
            s["updated"] = _now()
            self._save()
            return True

    # -- per-session MCP toggles ------------------------------------------
    def session_mcp_active(self, sid: str | None) -> dict:
        """The server→bool map for a session (empty if none / unknown)."""
        with self._lock:
            s = self._state["sessions"].get(sid)
            return copy.deepcopy(s.get("mcp_active", {})) if s else {}

    def set_session_mcp(self, sid: str, server: str, enabled: bool) -> bool:
        with self._lock:
            s = self._state["sessions"].get(sid)
            if not s:
                return False
            s.setdefault("mcp_active", {})[server] = bool(enabled)
            self._save()
            return True

    # -- messages ----------------------------------------------------------
    def add_message(self, sid: str, message: dict) -> dict | None:
        with self._lock:
            s = self._state["sessions"].get(sid)
            if not s:
                return None
            message.setdefault("id", _gen_id("msg"))
            message.setdefault("ts", _now())
            s["messages"].append(message)
            s["updated"] = _now()
            self._save()
            return copy.deepcopy(message)

    def update_message(self, sid: str, mid: str, patch: dict) -> dict | None:
        with self._lock:
            s = self._state["sessions"].get(sid)
            if not s:
                return None
            for m in s["messages"]:
                if m.get("id") == mid:
                    m.update(patch)
                    s["updated"] = _now()
                    self._save()
                    return copy.deepcopy(m)
            return None

    def buffer_message(self, sid: str, mid: str, content: str) -> None:
        """Update a message body in memory only (no disk write) for streaming."""
        with self._lock:
            s = self._state["sessions"].get(sid)
            if not s:
                return
            for m in s["messages"]:
                if m.get("id") == mid:
                    m["content"] = content
                    return

    def delete_message(self, sid: str, mid: str) -> bool:
        with self._lock:
            s = self._state["sessions"].get(sid)
            if not s:
                return False
            before = len(s["messages"])
            s["messages"] = [m for m in s["messages"] if m.get("id") != mid]
            if len(s["messages"]) != before:
                s["updated"] = _now()
                self._save()
                return True
            return False

    def collapse_prior_think(self, sid: str):
        """
        Strip reasoning from every existing assistant message so that past
        think blocks vanish once a new turn begins and are never replayed.
        """
        from . import llm
        with self._lock:
            s = self._state["sessions"].get(sid)
            if not s:
                return
            op = self._state["settings"].get("think_open_tag", "<think>")
            cl = self._state["settings"].get("think_close_tag", "</think>")
            changed = False
            for m in s["messages"]:
                if m.get("role") == "assistant" and m.get("has_think"):
                    clean = m.get("clean") or llm.strip_think(m.get("content", ""), op, cl)
                    m["content"] = clean
                    m["clean"] = clean
                    m["think"] = ""
                    m["has_think"] = False
                    changed = True
            if changed:
                self._save()
