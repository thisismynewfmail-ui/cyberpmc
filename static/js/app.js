/* ============================================================
   OMNIBRAIN // COGNITION TERMINAL — client
   ============================================================ */
(() => {
"use strict";

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const TOKEN_KEY = "omnibrain_token";

/* ---------- shared state ---------- */
let settings = {};
let active = { id: null, name: "", messages: [], context: {} };
let busy = false;
let linkOnline = false;
const streamBuffers = {};           // messageId -> live text
const thinkExpanded = new Set();    // messageIds whose reasoning is expanded (folded by default)
const toolExpanded = new Set();     // toolblock keys the user has expanded (folded by default)
let mcpState = { enabled: false, servers: [], config_json: "" };
let stuck = true;                    // transcript pinned to bottom?
let booted = false;

/* ============================================================
   SOCKET
   ============================================================ */
const socket = io({ auth: { token: localStorage.getItem(TOKEN_KEY) || "" } });

socket.on("connect", () => setLink({ online: linkOnline, detail: "socket up" }, true));
socket.on("access_denied", (d) => {
  if (/token/i.test(d.reason || "")) {
    const t = prompt("This terminal requires an access token:");
    if (t !== null) { localStorage.setItem(TOKEN_KEY, t); location.reload(); }
  } else {
    showBoot("ACCESS DENIED", d.reason || "Connection refused", true);
  }
});

socket.on("settings", (s) => { settings = s; populateSettings(); reveal(); });
socket.on("sessions", (d) => renderSessions(d.sessions, d.active_id));
socket.on("session_sync", (p) => { applySession(p); reveal(); });
socket.on("gen_token", (d) => onToken(d));
socket.on("busy", (d) => setBusy(d.busy));
socket.on("link", (d) => setLink(d));
socket.on("clients", (d) => setScreens(d.count));
socket.on("toast", (d) => toast(d.text));
socket.on("voices", (d) => renderVoices(d));
socket.on("tts_audio", (d) => onTtsAudio(d));
socket.on("tts_clear", () => clearTts());
socket.on("previews_done", () => { $("#btn-gen-previews").classList.remove("busy"); });
socket.on("mcp_state", (d) => renderMcp(d));

/* ============================================================
   BOOT SEQUENCE
   ============================================================ */
const bootLines = [
  "INITIALIZING COGNITION LINK",
  "MOUNTING NEURAL BUFFERS",
  "CALIBRATING OPTICAL CORE",
  "SYNCING SHARED STATE",
];
let bi = 0;
const bootTimer = setInterval(() => {
  bi = (bi + 1) % bootLines.length;
  const el = $("#boot-line");
  if (el) el.textContent = bootLines[bi];
}, 650);

function reveal() {
  if (booted) return;
  booted = true;
  clearInterval(bootTimer);
  setTimeout(() => {
    document.body.classList.remove("state-boot");
    $("#app").classList.remove("app-hidden");
    positionUnderline();
  }, 500);
}
function showBoot(title, sub, bad) {
  $(".boot-title").textContent = title;
  const l = $("#boot-line"); l.textContent = sub; if (bad) l.style.color = "var(--red)";
  clearInterval(bootTimer);
  document.body.classList.add("state-boot");
  $("#app").classList.add("app-hidden");
}

/* ============================================================
   TABS
   ============================================================ */
$$(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));
function switchView(view) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.dataset.view === view));
  positionUnderline();
  if (view === "terminal") requestAnimationFrame(scrollToBottom);
}
function positionUnderline() {
  const t = $(".tab.active"); const u = $("#tab-underline");
  if (!t || !u) return;
  u.style.left = t.offsetLeft + "px";
  u.style.width = t.offsetWidth + "px";
}
window.addEventListener("resize", () => { positionUnderline(); });

/* ============================================================
   COGNITION CORE
   ============================================================ */
const core = $("#core");
const coreLabel = $("#core-label");
const eye = $(".c-eye");

function refreshCore() {
  let state = "idle", label = "STANDBY", cls = "";
  if (!linkOnline) { state = "offline"; label = "LINK SEVERED"; cls = "offline"; }
  else if (busy)   { state = "think";   label = "COGNITION";   cls = "think"; }
  else             { state = "idle";    label = "ONLINE"; }
  core.dataset.state = state;
  coreLabel.textContent = label;
  coreLabel.className = "core-label " + cls;
}
// optical saccade
setInterval(() => {
  if (!eye) return;
  if (core.dataset.state === "offline") { eye.style.transform = "translate(0,0)"; return; }
  const range = core.dataset.state === "think" ? 9 : 5;
  const x = (Math.random() * 2 - 1) * range;
  const y = (Math.random() * 2 - 1) * range;
  eye.style.transform = `translate(${x}px,${y}px)`;
}, 1700);

/* ============================================================
   LINK + SCREENS
   ============================================================ */
function setLink(d, keepDetail) {
  linkOnline = !!d.online;
  const sb = $("#stat-link");
  sb.classList.remove("online", "offline", "probing");
  sb.classList.add(linkOnline ? "online" : "offline");
  $("#link-text").textContent = linkOnline ? "ONLINE" : "OFFLINE";

  const chip = $("#link-chip");
  chip.classList.remove("online", "offline", "probing");
  chip.classList.add(linkOnline ? "online" : "offline");
  chip.querySelector("b").textContent = d.detail || (linkOnline ? "Link established" : "No link");

  if (d.models && d.models.length) showDetectedModels(d.models);
  refreshCore();
}
function setScreens(n) {
  $("#stat-screens").textContent = n;
  $("#net-screens").textContent = n;
  const cl = $("#node-cluster"); cl.innerHTML = "";
  for (let i = 0; i < Math.min(n, 24); i++) {
    const d = document.createElement("div");
    d.className = "node" + (i === 0 ? " self" : "");
    d.style.animationDelay = (i * 0.12) + "s";
    cl.appendChild(d);
  }
}

/* ============================================================
   SESSION + TRANSCRIPT
   ============================================================ */
function applySession(p) {
  active = p;
  $("#chat-session-name").textContent = (p.name || "SESSION").toUpperCase();
  $("#vital-session").textContent = (p.name || "—");
  const turns = p.messages.filter((m) => m.role === "user").length;
  $("#vital-turns").textContent = turns;
  updateContext(p.context || {});
  renderTranscript();
}

function updateContext(ctx) {
  const size = ctx.context_size || settings.context_size || 8196;
  const tokens = ctx.tokens || 0;
  const pct = ctx.pct != null ? ctx.pct : 0;
  $("#stat-context").textContent = Math.round(pct) + "%";
  $("#vital-tokens").textContent = tokens;
  $("#vital-horizon").textContent = ctx.dropped || 0;
  const fill = $("#ctx-fill");
  fill.style.width = Math.min(100, pct) + "%";
  fill.classList.remove("warn", "crit");
  const thr = settings.context_threshold || 95;
  if (pct >= thr) fill.classList.add("crit");
  else if (pct >= thr * 0.8) fill.classList.add("warn");
  $("#ctx-readout").textContent = `${tokens} / ${size}`;
  $("#ctx-threshold").style.left = Math.min(100, thr) + "%";
}

function splitThink(text) {
  const open = settings.think_open_tag || "<think>";
  const close = settings.think_close_tag || "</think>";
  const low = text.toLowerCase();
  const lo = low.indexOf(open.toLowerCase());
  if (lo === -1) return { answer: text, thinking: "", openEnded: false };
  const before = text.slice(0, lo);
  const rest = text.slice(lo + open.length);
  const lc = rest.toLowerCase().indexOf(close.toLowerCase());
  if (lc === -1) return { answer: before, thinking: rest, openEnded: true };
  const thinking = rest.slice(0, lc);
  const after = rest.slice(lc + close.length);
  return { answer: (before + after), thinking, openEnded: false };
}

const transcript = $("#transcript");

function renderTranscript() {
  const msgs = active.messages || [];
  $("#empty-state").style.display = msgs.length ? "none" : "";
  // wipe everything except the empty-state node
  $$(".msg", transcript).forEach((n) => n.remove());
  for (const m of msgs) transcript.appendChild(buildMessage(m));
  if (stuck) scrollToBottom();
}

function buildMessage(m) {
  // Tool observations render as their own compacted block, not a chat bubble.
  if (m.role === "tool") return buildToolResult(m);

  const wrap = document.createElement("div");
  wrap.className = "msg " + m.role + (m.error ? " error" : "");
  wrap.dataset.id = m.id;

  const tag = document.createElement("div");
  tag.className = "msg-tag";
  const who = m.role === "user" ? (settings.username || "User").toUpperCase() : "OMNIBRAIN";
  tag.innerHTML = `<span>${who}</span>`;
  wrap.appendChild(tag);

  const isStreaming = !!m.streaming;
  const text = isStreaming ? (streamBuffers[m.id] ?? m.content ?? "") : (m.content ?? "");

  if (m.role === "assistant") {
    const { answer, thinking, openEnded } = splitThink(text);
    const hadThinking = thinking.length > 0 || openEnded;
    const showT = settings.show_thinking !== false;

    if (hadThinking && showT) wrap.appendChild(buildThink(m.id, thinking, openEnded && isStreaming));

    const bub = document.createElement("div");
    bub.className = "msg-bubble" + (!answer && isStreaming ? " empty" : "");
    bub.textContent = answer;
    if (isStreaming) bub.appendChild(caret());
    wrap.appendChild(bub);

    if (hadThinking && !showT) {
      const mk = document.createElement("div");
      mk.className = "think-marker";
      mk.innerHTML = (openEnded && isStreaming)
        ? `<span class="spinner" style="width:9px;height:9px;border:1.5px solid var(--cyan-dim);border-top-color:var(--cyan-2);border-radius:50%;display:inline-block;animation:spin .8s linear infinite"></span> REASONING…`
        : `◇ REASONED (hidden)`;
      wrap.appendChild(mk);
    }
    // Tool calls the model requested this turn — folded away so they never
    // interrupt the spoken reply; click to inspect the arguments.
    if (m.tool_calls && m.tool_calls.length) wrap.appendChild(buildToolCalls(m));
    if (settings.show_generation_info && m.meta && !isStreaming) wrap.appendChild(buildGenInfo(m.meta));
  } else {
    if (m.images && m.images.length) wrap.appendChild(buildImages(m.images));
    // An image-only turn needs no (empty) text bubble.
    if (text || !(m.images && m.images.length)) {
      const bub = document.createElement("div");
      bub.className = "msg-bubble";
      bub.textContent = text;
      wrap.appendChild(bub);
    }
  }

  if (!isStreaming) wrap.appendChild(buildFoot(m));
  return wrap;
}

function buildImages(images) {
  const box = document.createElement("div");
  box.className = "msg-images";
  for (const src of images) {
    const a = document.createElement("a");
    a.href = src; a.target = "_blank"; a.rel = "noopener";
    const img = document.createElement("img");
    img.src = src; img.loading = "lazy"; img.alt = "attached image";
    a.appendChild(img); box.appendChild(a);
  }
  return box;
}

function caret() { const c = document.createElement("span"); c.className = "caret"; return c; }

function buildThink(id, text, spinning) {
  const box = document.createElement("div");
  // Folded by default; only expanded if the user has opted in for this message.
  box.className = "think" + (thinkExpanded.has(id) ? "" : " collapsed") + (spinning ? " thinking" : "");
  const head = document.createElement("div");
  head.className = "think-head";
  head.innerHTML =
    (spinning ? `<span class="spinner"></span>` : `<span>◇</span>`) +
    ` <span class="think-label">${spinning ? "COGNIZING" : "COGNITION TRACE"}</span>` +
    ` <span class="chev">▾</span>`;
  head.addEventListener("click", () => {
    box.classList.toggle("collapsed");
    if (box.classList.contains("collapsed")) thinkExpanded.delete(id);
    else thinkExpanded.add(id);
  });
  const body = document.createElement("div");
  body.className = "think-body";
  body.textContent = text;
  box.appendChild(head); box.appendChild(body);
  return box;
}

/* ---------- MCP tool call / result blocks (compacted) ---------- */
function buildToolBlock(key, cls, headHTML, bodyText) {
  const box = document.createElement("div");
  box.className = "toolblock " + cls + (toolExpanded.has(key) ? "" : " collapsed");
  const head = document.createElement("div");
  head.className = "toolblock-head";
  head.innerHTML = headHTML + ` <span class="chev">▾</span>`;
  head.addEventListener("click", () => {
    box.classList.toggle("collapsed");
    if (box.classList.contains("collapsed")) toolExpanded.delete(key);
    else toolExpanded.add(key);
  });
  const body = document.createElement("div");
  body.className = "toolblock-body";
  body.textContent = bodyText;
  box.appendChild(head); box.appendChild(body);
  return box;
}

function buildToolCalls(m) {
  const frag = document.createDocumentFragment();
  m.tool_calls.forEach((tc, i) => {
    let args = tc.arguments || "";
    try { args = JSON.stringify(JSON.parse(args), null, 2); } catch {}
    const head = `<span>⛭</span> <b>TOOL CALL</b> · <span class="tname">${escapeHtml(tc.name)}</span>`;
    frag.appendChild(buildToolBlock(m.id + "::call" + i, "call", head, args || "(no arguments)"));
  });
  return frag;
}

function buildToolResult(m) {
  const wrap = document.createElement("div");
  wrap.className = "msg tool";
  wrap.dataset.id = m.id;
  const running = m.status === "running";
  const isErr = m.status === "error";
  const cls = "result" + (isErr ? " err" : "");
  const icon = running ? `<span class="tool-spinner"></span>` : (isErr ? `<span>⚠</span>` : `<span>◇</span>`);
  const label = running ? "RUNNING" : (isErr ? "TOOL ERROR" : "TOOL RESULT");
  const head = `${icon} <b>${label}</b> · <span class="tname">${escapeHtml(m.name || "")}</span>`;
  const body = running ? "executing…" : (m.content || "(no content)");
  wrap.appendChild(buildToolBlock(m.id, cls, head, body));
  return wrap;
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function buildGenInfo(meta) {
  const g = document.createElement("div");
  g.className = "geninfo";
  const bits = [];
  if (meta.completion_tokens != null) bits.push(`OUT <b>${meta.completion_tokens}</b> tok`);
  if (meta.tokens_per_second != null) bits.push(`<b>${meta.tokens_per_second}</b> tok/s`);
  if (meta.elapsed != null) bits.push(`<b>${meta.elapsed}</b>s`);
  if (meta.prompt_tokens != null) bits.push(`PROMPT <b>${meta.prompt_tokens}</b>`);
  if (meta.dropped_messages) bits.push(`CROPPED <b>${meta.dropped_messages}</b>`);
  if (meta.finish_reason) bits.push(`FIN <b>${meta.finish_reason}</b>`);
  if (meta.model) bits.push(`<b>${meta.model}</b>`);
  g.innerHTML = bits.join("<span style='opacity:.4'>·</span> ");
  return g;
}

function buildFoot(m) {
  const f = document.createElement("div");
  f.className = "msg-foot";
  const copy = document.createElement("button");
  copy.textContent = "⧉ COPY";
  copy.addEventListener("click", () => {
    const txt = m.role === "assistant" ? splitThink(m.content || "").answer : (m.content || "");
    navigator.clipboard?.writeText(txt).then(() => toast("Copied to clipboard"));
  });
  const del = document.createElement("button");
  del.textContent = "✕ DELETE";
  del.addEventListener("click", () => {
    if (del.dataset.armed) socket.emit("delete_message", { id: m.id });
    else { del.dataset.armed = "1"; del.textContent = "✕ CONFIRM"; setTimeout(() => { del.dataset.armed = ""; del.textContent = "✕ DELETE"; }, 2500); }
  });
  f.appendChild(copy); f.appendChild(del);
  return f;
}

/* ---------- streaming token updates ---------- */
function onToken(d) {
  if (d.session_id !== active.id) return;
  if (settings.voice_enabled && settings.tts_engine === "noise") speakNoise(d.delta);
  streamBuffers[d.message_id] = (streamBuffers[d.message_id] || "") + d.delta;
  const el = $(`.msg[data-id="${d.message_id}"]`, transcript);
  if (el) {
    const fresh = buildMessage({ id: d.message_id, role: "assistant", content: streamBuffers[d.message_id], streaming: true });
    el.replaceWith(fresh);
  }
  if (stuck) scrollToBottom();
}

/* ============================================================
   SCROLLING
   ============================================================ */
transcript.addEventListener("scroll", () => {
  const gap = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight;
  stuck = gap < 48;
  $("#jump-latest").classList.toggle("show", !stuck);
});
function scrollToBottom() { transcript.scrollTop = transcript.scrollHeight; }
$("#jump-latest").addEventListener("click", () => { stuck = true; scrollToBottom(); $("#jump-latest").classList.remove("show"); });

/* ============================================================
   COMPOSER
   ============================================================ */
const input = $("#input");
const sendBtn = $("#send-btn");
function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 180) + "px"; }
input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
sendBtn.addEventListener("click", send);
function send() {
  const text = input.value.trim();
  if ((!text && !pendingImages.length) || busy) return;
  const payload = { text };
  if (pendingImages.length) payload.images = pendingImages.slice();
  socket.emit("send_message", payload);
  input.value = ""; pendingImages = []; renderAttachTray(); autosize(); stuck = true;
}
$("#prompt-mark").textContent = "USER ▸";

/* ============================================================
   IMAGE ATTACHMENTS — pick, compress, preview
   ============================================================ */
let pendingImages = [];                 // compressed data URLs awaiting transmit
const IMG_MAX_DIM_FALLBACK = 1280;
const IMG_QUALITY_FALLBACK = 0.82;
const attachBtn = $("#attach-btn");
const imageInput = $("#image-input");
const attachTray = $("#attach-tray");

// Downscale + re-encode an image File to a JPEG data URL, entirely in-browser.
function compressImage(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("read failed"));
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => reject(new Error("decode failed"));
      img.onload = () => {
        let w = img.naturalWidth || img.width, h = img.naturalHeight || img.height;
        const maxDim = Number(settings.image_max_dim) || IMG_MAX_DIM_FALLBACK;
        if (maxDim > 0 && Math.max(w, h) > maxDim) {
          const scale = maxDim / Math.max(w, h);
          w = Math.round(w * scale); h = Math.round(h * scale);
        }
        const canvas = document.createElement("canvas");
        canvas.width = w; canvas.height = h;
        const ctx = canvas.getContext("2d");
        ctx.drawImage(img, 0, 0, w, h);
        const q = Number(settings.image_quality) || IMG_QUALITY_FALLBACK;
        try { resolve(canvas.toDataURL("image/jpeg", q)); }
        catch (e) { reject(e); }
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

async function addImageFiles(files) {
  const imgs = [...files].filter((f) => f && f.type && f.type.startsWith("image/"));
  if (!imgs.length) return;
  for (const f of imgs) {
    if (pendingImages.length >= 8) { toast("Up to 8 images per message", true); break; }
    try { pendingImages.push(await compressImage(f)); }
    catch (e) { toast("Could not read an image", true); }
  }
  renderAttachTray();
}

function renderAttachTray() {
  if (!attachTray) return;
  attachTray.innerHTML = "";
  attachTray.hidden = pendingImages.length === 0;
  attachBtn.classList.toggle("has", pendingImages.length > 0);
  pendingImages.forEach((src, i) => {
    const thumb = document.createElement("div");
    thumb.className = "attach-thumb";
    thumb.style.backgroundImage = `url("${src}")`;
    const rm = document.createElement("button");
    rm.className = "rm"; rm.textContent = "✕"; rm.title = "Remove";
    rm.addEventListener("click", () => { pendingImages.splice(i, 1); renderAttachTray(); });
    thumb.appendChild(rm);
    attachTray.appendChild(thumb);
  });
}

attachBtn.addEventListener("click", () => { if (!busy) imageInput.click(); });
imageInput.addEventListener("change", () => {
  addImageFiles(imageInput.files);
  imageInput.value = "";  // allow re-picking the same file
});
// paste an image straight from the clipboard into the composer
input.addEventListener("paste", (e) => {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  const files = [...items].filter((it) => it.kind === "file" && it.type.startsWith("image/"))
                          .map((it) => it.getAsFile()).filter(Boolean);
  if (files.length) { e.preventDefault(); addImageFiles(files); }
});

function setBusy(b) {
  busy = b;
  $("#busy-flag").classList.toggle("on", b);
  $("#btn-stop").disabled = !b;
  sendBtn.disabled = b;
  input.disabled = b;
  if (attachBtn) attachBtn.disabled = b;
  refreshCore();
}
$("#btn-stop").addEventListener("click", () => socket.emit("stop_generation"));
$("#btn-new-quick").addEventListener("click", () => { socket.emit("new_session"); switchView("terminal"); });

/* ============================================================
   SESSIONS
   ============================================================ */
$("#btn-new-session").addEventListener("click", () => { socket.emit("new_session"); switchView("terminal"); });
$("#btn-clear-session").addEventListener("click", () => {
  if (confirm("Clear all messages in the active session?")) socket.emit("clear_session", { id: active.id });
});

function renderSessions(list, activeId) {
  const box = $("#session-list"); box.innerHTML = "";
  for (const s of list) {
    const card = document.createElement("div");
    card.className = "s-card" + (s.id === activeId ? " active" : "");
    card.innerHTML = `
      <div class="s-glyph">◈</div>
      <div class="s-main">
        <div class="s-name"></div>
        <div class="s-meta">${s.count} msg <span style="opacity:.4">·</span> ${timeAgo(s.updated)}${s.id === activeId ? ' <span class="live">· LIVE</span>' : ""}</div>
      </div>
      <div class="s-tools"></div>`;
    card.querySelector(".s-name").textContent = s.name;

    card.addEventListener("click", (e) => {
      if (e.target.closest(".s-tools") || e.target.closest("input")) return;
      socket.emit("load_session", { id: s.id }); switchView("terminal");
    });

    const tools = card.querySelector(".s-tools");
    tools.appendChild(toolBtn("✎", "Rename", () => renameInline(card, s)));
    tools.appendChild(toolBtn("⧉", "Duplicate", () => socket.emit("duplicate_session", { id: s.id })));
    const del = toolBtn("🗙", "Delete", null, "del");
    del.addEventListener("click", () => {
      if (del.dataset.armed) socket.emit("delete_session", { id: s.id });
      else { del.dataset.armed = "1"; del.textContent = "✓?"; setTimeout(() => { del.dataset.armed = ""; del.textContent = "🗙"; }, 2500); }
    });
    tools.appendChild(del);
    box.appendChild(card);
  }
}
function toolBtn(label, title, fn, cls) {
  const b = document.createElement("button");
  b.textContent = label; b.title = title; if (cls) b.classList.add(cls);
  if (fn) b.addEventListener("click", (e) => { e.stopPropagation(); fn(); });
  return b;
}
function renameInline(card, s) {
  const nameEl = card.querySelector(".s-name");
  const inp = document.createElement("input");
  inp.value = s.name; nameEl.textContent = ""; nameEl.appendChild(inp); inp.focus(); inp.select();
  const commit = () => { const v = inp.value.trim(); if (v && v !== s.name) socket.emit("rename_session", { id: s.id, name: v }); else nameEl.textContent = s.name; };
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") inp.blur(); if (e.key === "Escape") { nameEl.textContent = s.name; } });
  inp.addEventListener("blur", commit);
}
function timeAgo(ts) {
  if (!ts) return "—";
  const d = Math.floor(Date.now() / 1000 - ts);
  if (d < 60) return d + "s ago";
  if (d < 3600) return Math.floor(d / 60) + "m ago";
  if (d < 86400) return Math.floor(d / 3600) + "h ago";
  return Math.floor(d / 86400) + "d ago";
}

/* ============================================================
   SETTINGS — populate + bind
   ============================================================ */
const debounce = (fn, ms = 450) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
function isFocused(el) { return document.activeElement === el; }

function populateSettings() {
  $$("[data-setting]").forEach((el) => {
    const key = el.dataset.setting;
    if (isFocused(el)) return;
    if (key === "extra_params_json") { el.value = JSON.stringify(settings.extra_params || {}, null, 2); return; }
    if (key === "api_key") { el.value = ""; el.placeholder = settings.api_key_set ? "•••••••• (set)" : "(blank)"; return; }
    if (key in settings) el.value = settings[key];
  });
  $$("[data-toggle]").forEach((el) => el.classList.toggle("on", !!settings[el.dataset.toggle]));
  $$("[data-sampler]").forEach((el) => { if (!isFocused(el)) el.value = (settings.sampling || {})[el.dataset.sampler]; });
  $$("[data-sampler-toggle]").forEach((el) => el.classList.toggle("on", !!(settings.sampling || {})[el.dataset.samplerToggle]));

  $("#thr-range").value = settings.context_threshold ?? 95;
  $("#thr-val").textContent = settings.context_threshold ?? 95;
  $("#apikey-state").textContent = settings.api_key_set ? "(currently set)" : "";
  $("#stat-model").textContent = settings.model || "—";
  $("#prompt-mark").textContent = (settings.username || "User").toUpperCase() + " ▸";
  $("#sampler-grid").classList.toggle("dim", !!settings.use_endpoint_sampler_defaults);
  const attachEl = $("#attach-btn");
  if (attachEl) attachEl.style.display = (settings.image_input_enabled === false) ? "none" : "";
  $("#net-url").textContent = `http://${location.hostname}:${location.port || 5005}`;
  refreshSpeechUI();
  validateExtra();
  // re-render so username / show_thinking / geninfo changes take effect immediately
  renderTranscript();
}

function pushSetting(key, val) { socket.emit("update_settings", { [key]: val }); }
function pushSampler(key, val) { socket.emit("update_settings", { sampling: { [key]: val } }); }

// text / number inputs
$$("[data-setting]").forEach((el) => {
  const key = el.dataset.setting;
  if (key === "extra_params_json") {
    el.addEventListener("input", validateExtra);
    el.addEventListener("change", () => { try { const obj = el.value.trim() ? JSON.parse(el.value) : {}; pushSetting("extra_params", obj); } catch {} });
    return;
  }
  const numeric = el.type === "number";
  const handler = () => {
    let v = el.value;
    if (key === "api_key" && v === "") return; // don't clobber unless edited
    if (numeric) v = el.value === "" ? 0 : Number(el.value);
    pushSetting(key, v);
  };
  el.addEventListener("change", handler);
  if (el.tagName === "TEXTAREA") el.addEventListener("input", debounce(handler, 600));
});
// clearing api key explicitly: blur with empty after focus should clear
$('input[data-setting="api_key"]').addEventListener("keyup", (e) => {
  if (e.key === "Enter") pushSetting("api_key", e.target.value);
});

// toggles
$$("[data-toggle]").forEach((el) => {
  el.addEventListener("click", () => {
    const key = el.dataset.toggle;
    const val = !el.classList.contains("on");
    el.classList.toggle("on", val);
    pushSetting(key, val);
    if (key === "use_endpoint_sampler_defaults") $("#sampler-grid").classList.toggle("dim", val);
    if (key === "voice_enabled") {
      settings.voice_enabled = val;
      if (val) getAudio();   // unlock audio on the user gesture
      else clearTts();       // stop playback immediately when muted
      refreshSpeechUI();
    }
  });
});
// sampler numeric
$$("[data-sampler]").forEach((el) => {
  el.addEventListener("change", () => { const v = el.value === "" ? 0 : Number(el.value); pushSampler(el.dataset.sampler, v); });
});
// sampler toggles
$$("[data-sampler-toggle]").forEach((el) => {
  el.addEventListener("click", () => { const v = !el.classList.contains("on"); el.classList.toggle("on", v); pushSampler(el.dataset.samplerToggle, v); });
});
// threshold slider
$("#thr-range").addEventListener("input", (e) => { $("#thr-val").textContent = e.target.value; });
$("#thr-range").addEventListener("change", (e) => pushSetting("context_threshold", Number(e.target.value)));

function validateExtra() {
  const el = $('[data-setting="extra_params_json"]'); const st = $("#extra-json-status");
  if (!el.value.trim()) { st.textContent = "empty → no extra params"; st.className = "json-status"; return; }
  try { JSON.parse(el.value); st.textContent = "✓ valid JSON"; st.className = "json-status ok"; }
  catch (e) { st.textContent = "✕ invalid JSON"; st.className = "json-status bad"; }
}

$("#btn-test-link").addEventListener("click", () => { setProbing(); socket.emit("test_link"); });
$("#btn-load-samplers").addEventListener("click", () => socket.emit("load_sampler_defaults"));
function setProbing() {
  const sb = $("#stat-link"); sb.classList.remove("online", "offline"); sb.classList.add("probing");
  $("#link-text").textContent = "PROBING";
  const chip = $("#link-chip"); chip.classList.remove("online", "offline"); chip.classList.add("probing");
  chip.querySelector("b").textContent = "probing…";
}
function showDetectedModels(models) {
  const box = $("#model-pick"), list = $("#model-pick-list");
  if (!models.length) { box.hidden = true; return; }
  box.hidden = false; list.innerHTML = "";
  models.slice(0, 12).forEach((m) => {
    const b = document.createElement("button"); b.textContent = m;
    b.addEventListener("click", () => { pushSetting("model", m); $('[data-setting="model"]').value = m; });
    list.appendChild(b);
  });
}

/* ============================================================
   SPEECH — engines, controls, playback
   ============================================================ */
let voicesState = { voices: [], piper_available: false };

/* ---- Web Audio (shared by both engines) ---- */
let audioCtx = null;
function getAudio() {
  if (!audioCtx) {
    try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); }
    catch (e) { return null; }
  }
  if (audioCtx.state === "suspended") audioCtx.resume();
  return audioCtx;
}
function ttsVolume() { return settings.tts_volume != null ? Number(settings.tts_volume) : 0.85; }

/* ---- NOISE engine: streaming "animal crossing" blips ---- */
let blipCount = 0;
function blip(ctx) {
  const now = ctx.currentTime;
  const o = ctx.createOscillator(), g = ctx.createGain();
  const base = Number(settings.noise_pitch) || 320;
  const varr = Number(settings.noise_pitch_variance) || 0;
  o.type = settings.noise_waveform || "square";
  o.frequency.value = Math.max(40, base + (Math.random() * 2 - 1) * varr);
  const peak = Math.max(0.0001, ttsVolume() * 0.16);
  g.gain.setValueAtTime(0.0001, now);
  g.gain.exponentialRampToValueAtTime(peak, now + 0.006);
  g.gain.exponentialRampToValueAtTime(0.0001, now + 0.08);
  o.connect(g); g.connect(ctx.destination);
  o.start(now); o.stop(now + 0.09);
}
function speakNoise(deltaText) {
  const ctx = getAudio(); if (!ctx) return;
  const speed = Math.max(1, Number(settings.noise_speed) || 2);
  for (const ch of deltaText) {
    if (/\s/.test(ch)) continue;
    blipCount++;
    if (blipCount % speed === 0) blip(ctx);
  }
}

/* ---- PIPER engine: ordered playback of streamed WAV blocks ---- */
const ttsBuffers = {};   // seq -> decoded AudioBuffer (awaiting its turn)
let ttsNext = 1;         // next seq to play (server restarts seq at 1 per turn)
let ttsActive = false;   // a block is currently sounding
let ttsSource = null;

function b64ToArrayBuffer(b64) {
  const bin = atob(b64);
  const len = bin.length, bytes = new Uint8Array(len);
  for (let i = 0; i < len; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}
function onTtsAudio(d) {
  if (!settings.voice_enabled) return;
  const ctx = getAudio(); if (!ctx) return;
  let buf;
  try { buf = b64ToArrayBuffer(d.audio); } catch (e) { return; }
  ctx.decodeAudioData(buf.slice(0), (audio) => {
    if (!settings.voice_enabled) return;
    ttsBuffers[d.seq] = audio;
    pumpTts();
  }, () => {});
}
function pumpTts() {
  if (ttsActive) return;
  const audio = ttsBuffers[ttsNext];
  if (!audio) return;
  delete ttsBuffers[ttsNext];
  ttsNext++;
  const ctx = getAudio(); if (!ctx) return;
  const src = ctx.createBufferSource(), g = ctx.createGain();
  g.gain.value = ttsVolume();
  src.buffer = audio; src.connect(g); g.connect(ctx.destination);
  ttsSource = src; ttsActive = true;
  src.onended = () => { ttsActive = false; ttsSource = null; pumpTts(); };
  try { src.start(); } catch (e) { ttsActive = false; ttsSource = null; }
}
function clearTts() {
  try { if (ttsSource) ttsSource.stop(); } catch (e) {}
  ttsSource = null; ttsActive = false;
  for (const k in ttsBuffers) delete ttsBuffers[k];
  ttsNext = 1;
}

/* ---- Speech tab UI ---- */
function refreshSpeechUI() {
  const engine = settings.tts_engine || "noise";
  $$(".engine-opt").forEach((b) => b.classList.toggle("sel", b.dataset.engine === engine));
  const nc = $("#noise-card"), pc = $("#piper-card");
  if (nc) nc.classList.toggle("inactive", engine !== "noise");
  if (pc) pc.classList.toggle("inactive", engine !== "piper");

  // chip status
  const chip = $("#speech-chip");
  if (chip) {
    chip.classList.remove("on", "off");
    chip.classList.add(settings.voice_enabled ? "on" : "off");
    chip.querySelector("b").textContent = settings.voice_enabled
      ? (engine === "piper" ? "PIPER" : "NOISE") + " · LIVE" : "MUTED";
  }

  setRange("#vol-range", "#vol-val", Math.round(ttsVolume() * 100));
  setRange("#nspeed-range", "#nspeed-val", settings.noise_speed ?? 2);
  setRange("#npitch-range", "#npitch-val", settings.noise_pitch ?? 320);
  setRange("#nvar-range", "#nvar-val", settings.noise_pitch_variance ?? 90);
  const pr = settings.piper_length_scale ?? 1.0;
  setRange("#prate-range", "#prate-val", pr, (v) => Number(v).toFixed(2));
  // keep selection highlight in sync with the latest settings
  highlightVoice();
}
function setRange(rangeSel, labelSel, val, fmt) {
  const r = $(rangeSel), l = $(labelSel);
  if (r && !isFocused(r)) r.value = val;
  if (l) l.textContent = fmt ? fmt(val) : val;
}
function highlightVoice() {
  $$(".voice-row").forEach((row) =>
    row.classList.toggle("sel", row.dataset.id === (settings.piper_voice || "")));
}

function renderVoices(d) {
  voicesState = d || { voices: [], piper_available: false };
  const status = $("#piper-status");
  if (status) {
    status.classList.remove("ok", "bad");
    if (voicesState.piper_available) {
      status.classList.add("ok");
      status.textContent = `Piper TTS detected · ${voicesState.voices.length} voice(s) installed`;
    } else {
      status.classList.add("bad");
      status.textContent = "Piper TTS not installed — run:  pip install piper-tts";
    }
  }
  const box = $("#voice-list"); if (!box) return;
  box.innerHTML = "";
  if (!voicesState.voices.length) {
    box.innerHTML = '<div class="voice-empty">No voices found. Drop <code>.onnx</code> + ' +
      '<code>.onnx.json</code> files into the <code>voices/</code> folder, then GENERATE PREVIEWS.</div>';
    return;
  }
  for (const v of voicesState.voices) {
    const row = document.createElement("div");
    row.className = "voice-row" + (v.id === (settings.piper_voice || "") ? " sel" : "");
    row.dataset.id = v.id;

    const pick = document.createElement("button");
    pick.className = "voice-pick";
    pick.innerHTML = `<i class="vdot"></i><span class="vname"></span>`;
    pick.querySelector(".vname").textContent = v.name;
    pick.addEventListener("click", () => {
      pushSetting("piper_voice", v.id);
      settings.piper_voice = v.id;
      highlightVoice();
    });

    const prev = document.createElement("button");
    prev.className = "voice-prev";
    if (v.has_preview) {
      prev.innerHTML = "▶ PREVIEW";
      prev.addEventListener("click", (e) => { e.stopPropagation(); playPreview(v); });
    } else {
      prev.classList.add("none");
      prev.innerHTML = "— NO PREVIEW";
      prev.title = "Press GENERATE PREVIEWS to render a sample";
      prev.addEventListener("click", (e) => { e.stopPropagation(); genPreviews(); });
    }

    row.appendChild(pick); row.appendChild(prev);
    box.appendChild(row);
  }
}

let previewAudio = null;
function playPreview(v) {
  getAudio(); // user gesture
  try { if (previewAudio) { previewAudio.pause(); } } catch (e) {}
  previewAudio = new Audio(v.preview_url + "?t=" + Date.now());
  previewAudio.volume = ttsVolume();
  previewAudio.play().catch(() => toast("Could not play preview", true));
}
function genPreviews() {
  if (!voicesState.piper_available) { toast("Install Piper TTS first (pip install piper-tts)", true); return; }
  $("#btn-gen-previews").classList.add("busy");
  socket.emit("generate_previews");
}

/* ---- Speech tab wiring ---- */
$$(".engine-opt").forEach((b) => b.addEventListener("click", () => {
  const eng = b.dataset.engine;
  if (settings.tts_engine === eng) return;
  settings.tts_engine = eng;
  clearTts();
  pushSetting("tts_engine", eng);
  refreshSpeechUI();
}));
bindSpeechRange("#vol-range", "#vol-val", "tts_volume", (v) => v / 100, (v) => Math.round(v * 100));
bindSpeechRange("#nspeed-range", "#nspeed-val", "noise_speed", (v) => Math.round(v));
bindSpeechRange("#npitch-range", "#npitch-val", "noise_pitch", (v) => Math.round(v));
bindSpeechRange("#nvar-range", "#nvar-val", "noise_pitch_variance", (v) => Math.round(v));
bindSpeechRange("#prate-range", "#prate-val", "piper_length_scale",
  (v) => Number(v), (v) => Number(v).toFixed(2));

function bindSpeechRange(rangeSel, labelSel, key, toVal, fmt) {
  const r = $(rangeSel); if (!r) return;
  r.addEventListener("input", () => {
    const v = toVal(Number(r.value));
    settings[key] = v;
    $(labelSel).textContent = fmt ? fmt(v) : v;
  });
  r.addEventListener("change", () => pushSetting(key, toVal(Number(r.value))));
}

$("#btn-gen-previews").addEventListener("click", genPreviews);
$("#btn-test-noise").addEventListener("click", () => {
  const ctx = getAudio(); if (!ctx) return;
  let i = 0;
  const id = setInterval(() => { blip(ctx); if (++i >= 14) clearInterval(id); }, 70);
});

/* ============================================================
   BACKGROUND NETWORK CANVAS
   ============================================================ */
(function netCanvas() {
  const cv = $("#net-canvas"); const ctx = cv.getContext("2d");
  let w, h, nodes = [], pulses = [];
  function resize() { w = cv.width = innerWidth; h = cv.height = innerHeight; build(); }
  function build() {
    const count = Math.round((w * h) / 36000);
    nodes = [];
    for (let i = 0; i < count; i++) nodes.push({ x: Math.random() * w, y: Math.random() * h, vx: (Math.random() - .5) * .12, vy: (Math.random() - .5) * .12 });
  }
  function neighbors(n) { return nodes.filter((o) => o !== n && Math.hypot(o.x - n.x, o.y - n.y) < 150); }
  function tick() {
    ctx.clearRect(0, 0, w, h);
    for (const n of nodes) {
      n.x += n.vx; n.y += n.vy;
      if (n.x < 0 || n.x > w) n.vx *= -1;
      if (n.y < 0 || n.y > h) n.vy *= -1;
    }
    // edges
    ctx.lineWidth = 1;
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        if (d < 150) {
          ctx.strokeStyle = `rgba(124,93,29,${(1 - d / 150) * 0.22})`;
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        }
      }
    }
    // nodes
    for (const n of nodes) {
      ctx.fillStyle = "rgba(69,214,223,0.28)";
      ctx.beginPath(); ctx.arc(n.x, n.y, 1.4, 0, 7); ctx.fill();
    }
    // pulses travel along random edges
    if (Math.random() < 0.04 && nodes.length > 2) {
      const a = nodes[(Math.random() * nodes.length) | 0];
      const nb = neighbors(a);
      if (nb.length) pulses.push({ a, b: nb[(Math.random() * nb.length) | 0], t: 0 });
    }
    pulses = pulses.filter((p) => p.t <= 1);
    for (const p of pulses) {
      p.t += 0.02;
      const x = p.a.x + (p.b.x - p.a.x) * p.t, y = p.a.y + (p.b.y - p.a.y) * p.t;
      ctx.fillStyle = "rgba(240,180,42,0.85)";
      ctx.beginPath(); ctx.arc(x, y, 2, 0, 7); ctx.fill();
      ctx.fillStyle = "rgba(240,180,42,0.18)";
      ctx.beginPath(); ctx.arc(x, y, 5, 0, 7); ctx.fill();
    }
    requestAnimationFrame(tick);
  }
  addEventListener("resize", resize); resize(); tick();
})();

/* ============================================================
   MCP TOOLS — Tools tab + per-session sidebar rail
   ============================================================ */
let mcpRailCollapsed = true;

function renderMcp(d) {
  mcpState = d || { enabled: false, servers: [], config_json: "" };

  // master chip
  const chip = $("#mcp-chip");
  if (chip) {
    chip.classList.remove("on", "off");
    chip.classList.add(mcpState.enabled ? "on" : "off");
    chip.querySelector("b").textContent = mcpState.enabled ? "ARMED" : "OFF";
  }

  // stored config JSON (don't clobber while the user is editing it)
  const cfg = $("#mcp-config");
  if (cfg && !isFocused(cfg)) cfg.value = mcpState.config_json || "";

  renderMcpServers();
  renderMcpRail();
}

function renderMcpServers() {
  const box = $("#mcp-server-list");
  if (!box) return;
  box.innerHTML = "";
  const servers = mcpState.servers || [];
  if (!servers.length) {
    box.innerHTML = '<div class="mcp-empty">No MCP servers configured. Add a ' +
      '<code>mcpServers</code> block above (the Playwright example is pre-filled) and press ' +
      '<b>SAVE SERVERS</b>.</div>';
    return;
  }
  for (const s of servers) {
    const running = s.status === "running";
    const card = document.createElement("div");
    card.className = "mcp-srv " + (running ? "run" : (s.status === "error" ? "err" :
      (s.status === "starting" ? "starting" : "")));

    const head = document.createElement("div");
    head.className = "mcp-srv-head";
    head.innerHTML = `
      <div class="mcp-srv-status">
        <i class="sdot"></i>
        <div class="mcp-srv-name">
          <b></b><code></code>
        </div>
      </div>
      <div class="mcp-srv-detail"></div>
      <div class="mcp-srv-actions"></div>`;
    head.querySelector(".mcp-srv-name b").textContent = s.name;
    head.querySelector(".mcp-srv-name code").textContent =
      [s.command, ...(s.args || [])].join(" ");
    head.querySelector(".mcp-srv-detail").textContent =
      s.detail || s.status.toUpperCase();

    const actions = head.querySelector(".mcp-srv-actions");
    if (running || s.status === "starting") {
      const stop = document.createElement("button");
      stop.className = "btn mini danger";
      stop.innerHTML = "<i>■</i> STOP";
      stop.addEventListener("click", () => socket.emit("mcp_stop_server", { name: s.name }));
      actions.appendChild(stop);
    } else {
      const start = document.createElement("button");
      start.className = "btn mini";
      start.innerHTML = "<i>▶</i> START";
      start.addEventListener("click", () => {
        start.innerHTML = "<i>◷</i> STARTING…"; start.disabled = true;
        socket.emit("mcp_start_server", { name: s.name });
      });
      actions.appendChild(start);
    }
    card.appendChild(head);

    // tool list (only meaningful once the server is connected)
    const tools = document.createElement("div");
    tools.className = "mcp-tools";
    if (running && (s.tools || []).length) {
      for (const t of s.tools) {
        const row = document.createElement("div");
        row.className = "mcp-tool" + (t.enabled ? "" : " off");
        const main = document.createElement("div");
        main.className = "mcp-tool-main";
        main.innerHTML = `<div class="mcp-tool-name"></div><div class="mcp-tool-desc"></div>`;
        main.querySelector(".mcp-tool-name").textContent = t.name;
        main.querySelector(".mcp-tool-desc").textContent = t.description || "";
        const sw = document.createElement("button");
        sw.className = "switch" + (t.enabled ? " on" : "");
        sw.setAttribute("role", "switch");
        sw.title = "Allow the model to call this tool";
        sw.addEventListener("click", () => {
          const val = !sw.classList.contains("on");
          sw.classList.toggle("on", val);
          row.classList.toggle("off", !val);
          socket.emit("mcp_toggle_tool", { key: t.key, enabled: val });
        });
        row.appendChild(main); row.appendChild(sw);
        tools.appendChild(row);
      }
    } else if (running) {
      tools.innerHTML = '<div class="mcp-tool-none">Server connected but exposes no tools.</div>';
    } else {
      tools.innerHTML = '<div class="mcp-tool-none">Start this server to discover its tools.</div>';
    }
    card.appendChild(tools);
    box.appendChild(card);
  }
}

function renderMcpRail() {
  const rail = $("#mcp-rail");
  if (!rail) return;
  rail.classList.toggle("on", !!mcpState.enabled);
  rail.classList.toggle("collapsed", mcpRailCollapsed);
  const running = (mcpState.servers || []).filter((s) => s.status === "running").length;
  $("#mcp-rail-state").textContent = !mcpState.enabled ? "OFF" : (running ? `${running} LIVE` : "ARMED");

  const list = $("#mcp-rail-list");
  list.innerHTML = "";
  const servers = mcpState.servers || [];
  if (!servers.length) {
    list.innerHTML = '<div class="mcp-rail-empty">No MCP servers. Configure them in the Tools tab.</div>';
    return;
  }
  for (const s of servers) {
    const active = s.session_active !== false;
    const row = document.createElement("div");
    const stateCls = s.status === "running" ? "run" : (s.status === "error" ? "err" :
      (s.status === "starting" ? "starting" : ""));
    row.className = "mcp-rail-row " + stateCls + (active ? "" : " off-server");
    row.innerHTML = `<i class="rdot"></i><span class="rname"></span>` +
      `<span class="rcount">${s.status === "running" ? (s.tools || []).length + "t" : ""}</span>`;
    row.querySelector(".rname").textContent = s.name;
    const sw = document.createElement("button");
    sw.className = "switch" + (active ? " on" : "");
    sw.setAttribute("role", "switch");
    sw.title = "Use this server in the current session";
    sw.addEventListener("click", () => {
      const val = !sw.classList.contains("on");
      sw.classList.toggle("on", val);
      row.classList.toggle("off-server", !val);
      socket.emit("mcp_session_toggle", { server: s.name, enabled: val });
    });
    row.appendChild(sw);
    list.appendChild(row);
  }
}

/* ---- Tools tab + rail wiring ---- */
$("#mcp-rail-head").addEventListener("click", () => {
  mcpRailCollapsed = !mcpRailCollapsed;
  $("#mcp-rail").classList.toggle("collapsed", mcpRailCollapsed);
});
$("#mcp-rail-config").addEventListener("click", () => switchView("tools"));
$("#btn-mcp-refresh").addEventListener("click", () => socket.emit("mcp_refresh"));
$("#btn-mcp-save").addEventListener("click", () => {
  const cfg = $("#mcp-config");
  socket.emit("mcp_save_servers", { json: cfg.value });
});
$("#mcp-config").addEventListener("input", () => {
  const el = $("#mcp-config"), st = $("#mcp-json-status");
  if (!el.value.trim()) { st.textContent = "empty"; st.className = "json-status"; return; }
  try { JSON.parse(el.value); st.textContent = "✓ valid JSON"; st.className = "json-status ok"; }
  catch (e) { st.textContent = "✕ invalid JSON"; st.className = "json-status bad"; }
});

/* ============================================================
   TOAST
   ============================================================ */
let toastTimer;
function toast(text, bad) {
  const t = $("#toast"); t.textContent = text; t.className = "toast show" + (bad ? " bad" : "");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}

/* init */
refreshCore(); positionUnderline(); autosize();
})();
