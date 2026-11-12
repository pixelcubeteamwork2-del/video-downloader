const $ = (id) => document.getElementById(id);
const form = $("url-form");
const input = $("url");
const statusBox = $("status");
const result = $("result");
const player = $("player");
let busy = false;
let current = null; // data from /api/info for the loaded video

const SITES = {
  youtube: ["YouTube", "yt"], facebook: ["Facebook", "fb"], pinterest: ["Pinterest", "pin"],
  instagram: ["Instagram", "ig"], twitter: ["X", "x"], reddit: ["Reddit", "rd"], linkedin: ["LinkedIn", "li"],
  tiktok: ["TikTok", "tt"],
};

function setBadge(el, site) {
  const [name, cls] = SITES[site] || ["Post", ""];
  el.textContent = name;
  el.className = "badge " + cls;
}

// action: optional { label, onClick } shown as a button inside the message.
function setStatus(message, type = "loading", action = null) {
  if (!message) { statusBox.hidden = true; return; }
  statusBox.hidden = false;
  statusBox.className = "status" + (type === "error" ? " error" : "");
  statusBox.innerHTML = "";
  if (type === "loading") {
    const s = document.createElement("span");
    s.className = "spinner";
    statusBox.appendChild(s);
  }
  const text = document.createElement("span");
  text.className = "status-text";
  text.textContent = message;
  statusBox.appendChild(text);
  if (action) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-soft btn-sm";
    btn.textContent = action.label;
    btn.addEventListener("click", action.onClick);
    statusBox.appendChild(btn);
  }
}

function formatDuration(sec) {
  if (!sec) return "";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

// 83.456 -> "1:23.46" (hours added when needed)
function formatTime(sec) {
  sec = Math.max(0, sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  const s = (sec % 60).toFixed(2).padStart(5, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

// Accepts "83.5", "1:23.5" or "1:02:03.5"; returns seconds or NaN.
function parseTime(text) {
  const parts = String(text).trim().split(":");
  if (!parts[0] || parts.length > 3 || parts.some((p) => p === "" || isNaN(p))) return NaN;
  return parts.reduce((total, p) => total * 60 + parseFloat(p), 0);
}

function formatSize(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
  return `${bytes.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function duration() {
  return (isFinite(player.duration) && player.duration) || (current && current.duration) || 0;
}

$("paste").addEventListener("click", async () => {
  try {
    input.value = (await navigator.clipboard.readText()).trim();
    input.dispatchEvent(new Event("input"));
    input.focus();
  } catch {
    setStatus("Clipboard access was blocked - paste with Ctrl+V instead.", "error");
  }
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (busy) return;
  busy = true;
  $("fetch").disabled = true;
  result.hidden = true;
  $("gallery").hidden = true;
  player.pause();
  setStatus("Reading the post...");

  try {
    const res = await fetch("/api/info", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: input.value.trim() }),
    });
    const data = await res.json();
    if (!res.ok) {
      const err = new Error(data.error || "Could not read that link.");
      err.loginRequired = data.login_required;
      throw err;
    }
    if (data.kind === "gallery") showGalleryOnly(data);
    else showResult(data);
    setStatus(null);
  } catch (err) {
    setStatus(err.message, "error",
      err.loginRequired ? { label: "Set up login", onClick: openSettings } : null);
  } finally {
    busy = false;
    $("fetch").disabled = false;
  }
});

const SITE_LOGO = {
  youtube: "▶", facebook: "f", instagram: "◎", tiktok: "♪", twitter: "𝕏",
  reddit: "r", linkedin: "in", pinterest: "P",
};
let selectedOpt = null;  // quality chosen in the radio list

function showResult(data) {
  current = data;
  const [siteName, siteClass] = SITES[data.site] || ["Video", ""];
  $("site").textContent = siteName;
  $("site").className = "clip-site " + siteClass;
  $("site-logo").textContent = SITE_LOGO[data.site] || "▶";
  $("site-logo").className = "site-logo " + siteClass;
  $("title").textContent = data.title;
  $("uploader").textContent = data.uploader ? `by ${data.uploader}` : "";
  $("meta-duration").textContent = formatDuration(data.duration);
  $("meta-res").textContent = data.frame_size ? data.frame_size.replace("x", "×") : "";
  // "•" separators only between the parts that are actually present
  const metaParts = ["uploader", "meta-duration", "meta-res"].map((id) => $(id));
  metaParts.forEach((el) => el.classList.remove("sep"));
  metaParts.filter((el) => el.textContent).slice(1).forEach((el) => el.classList.add("sep"));
  $("copy-link").onclick = async () => {
    try { await navigator.clipboard.writeText(data.url); toast("Link copied", data.url, "ok", 3000); }
    catch { toast("Couldn't copy", "Your browser blocked clipboard access.", "error", 4000); }
  };
  $("open-source").href = data.url;
  fillInfo(data);

  // Player
  if (data.preview) {
    player.poster = data.thumbnail || "";
    player.src = data.preview;
    player.hidden = false;
    $("no-preview").hidden = true;
  } else {
    player.removeAttribute("src");
    player.load();
    player.hidden = true;
    $("no-preview").hidden = false;
  }
  $("step-row").hidden = !data.preview;
  $("timeline").classList.toggle("no-preview", !data.preview);

  // Clip range and frame grabs need ffmpeg on the server.
  document.querySelectorAll(".needs-ffmpeg").forEach((el) => (el.hidden = !data.can_edit));
  $("no-edit").hidden = data.can_edit;
  selectTab("export");
  $("encoder").textContent = data.exact_encoder === "NVIDIA GPU" ? "NVIDIA GPU" : "processor (CPU)";
  $("frame-size").textContent = data.frame_size ? ` (${data.frame_size.replace("x", " × ")})` : "";
  for (const id of ["set-start", "set-end", "play-clip"]) $(id).disabled = !data.preview;
  resetRange();
  updatePlayhead();

  renderQualities(data);
  loadThumbnails(data.token);
  if (data.media_available) loadPostMedia(data);
  $("features").hidden = true;
  document.body.classList.add("workspace-open");
  result.hidden = false;
  drawFilmstrip();
}

function fillInfo(data) {
  const d = data.duration;
  $("info-duration").textContent = d ? formatDuration(d) : "–";
  const [w, h] = (data.frame_size || "").split("x").map(Number);
  $("info-res").textContent = w ? `${w} × ${h}` : "–";
  $("info-hd").hidden = !(w && Math.min(w, h) >= 720);
  if (w && Math.min(w, h) >= 2160) $("info-hd").textContent = "4K";
  else if (w && Math.min(w, h) >= 1440) $("info-hd").textContent = "2K";
  else $("info-hd").textContent = "HD";
  const best = data.options.find((o) => o.format !== "audio" && o.format !== "mp3");
  $("info-size").textContent = best && best.size ? formatSize(best.size) : "–";
  try {
    const host = new URL(data.url).hostname.replace(/^www\./, "");
    $("info-source").textContent = host;
    $("info-source").href = data.url;
  } catch { $("info-source").textContent = "–"; }
}

// ---------- Quality list (radio rows) ----------

function codecText(opt) {
  if (opt.format === "mp3") return "MP3 • best quality";
  if (opt.format === "audio") return "M4A • AAC";
  return $("compat-on").checked ? "MP4 • H.264" : "MP4 • original codec";
}

function renderQualities(data) {
  const box = $("options");
  box.innerHTML = "";
  selectedOpt = null;
  let group = null;
  data.options.forEach((opt, i) => {
    const isAudio = opt.format === "audio" || opt.format === "mp3";
    const kind = isAudio ? "Audio" : "Video";
    if (kind !== group) {
      group = kind;
      const divider = document.createElement("div");
      divider.className = "q-divider";
      divider.textContent = kind;
      box.appendChild(divider);
    }
    const row = document.createElement("label");
    row.className = "q-row" + (isAudio ? " audio" : "");
    row.innerHTML =
      `<input type="radio" name="quality" value="${i}">` +
      `<span class="q-radio" aria-hidden="true"></span>` +
      `<svg class="icon q-icon"><use href="#${isAudio ? "i-music" : "i-film"}"/></svg>` +
      `<span class="q-text"><span class="q-label"></span><span class="q-sub"></span></span>` +
      `<span class="q-size"></span>` +
      `<button type="button" class="q-dl" title="Download this quality"><svg class="icon"><use href="#i-download"/></svg></button>`;
    row.querySelector(".q-label").textContent = opt.label;
    if (i === 0 && !isAudio) row.querySelector(".q-label").insertAdjacentHTML("beforeend", ' <em class="best-tag">Best</em>');
    row.querySelector(".q-sub").textContent = codecText(opt);
    row.querySelector(".q-size").textContent = formatSize(opt.size) || "";
    row.dataset.index = i;
    const radio = row.querySelector("input");
    radio.addEventListener("change", () => selectQuality(opt));
    row.querySelector(".q-dl").addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      radio.checked = true;
      selectQuality(opt);
      startDownload(opt);
    });
    box.appendChild(row);
    if (i === 0) { radio.checked = true; selectedOpt = opt; }
  });
  updateDownloadButton();
}

function selectQuality(opt) {
  selectedOpt = opt;
  updateDownloadButton();
}

function updateCodecLabels() {
  if (!current) return;
  $("options").querySelectorAll(".q-row").forEach((row) => {
    row.querySelector(".q-sub").textContent = codecText(current.options[+row.dataset.index]);
  });
  $("fmt-display-text").textContent = $("compat-on").checked ? "MP4 (H.264)" : "MP4 (original codec)";
}

function updateDownloadButton() {
  const opt = selectedOpt;
  if (!opt) return;
  const on = $("trim-on").checked;
  const clip = getClip();
  const total = duration();
  $("options").querySelectorAll(".q-row").forEach((row) => {
    row.classList.toggle("selected", current && current.options[+row.dataset.index] === opt);
  });
  if (on && clip.valid) {
    const secs = (clip.end - clip.start).toFixed(2);
    $("dl-main-label").textContent = `Download Selected Clip (${secs}s)`;
    const est = opt.size && total ? opt.size * ((clip.end - clip.start) / total) : 0;
    $("dl-main-size").textContent = est ? `~${formatSize(est)}` : "";
  } else {
    $("dl-main-label").textContent = `Download ${opt.label}${opt.format === "mp3" ? " MP3" : opt.format === "audio" ? " M4A" : ""}`;
    $("dl-main-size").textContent = formatSize(opt.size) || "";
  }
  $("dl-heading").textContent = on ? "Download Clip" : "Download Video";
}

$("dl-main").addEventListener("click", () => selectedOpt && startDownload(selectedOpt));

// ---------- Trim range ----------

function getClip() {
  const start = parseTime($("start").value);
  const end = parseTime($("end").value);
  return { start, end, valid: !isNaN(start) && !isNaN(end) && end - start >= 0.1 && start >= 0 };
}

function setRange(start, end) {
  const total = duration();
  start = Math.max(0, start);
  if (total) end = Math.min(end, total);
  $("start").value = formatTime(start);
  $("end").value = formatTime(end);
  // Trimming is on whenever the range is narrower than the whole video.
  $("trim-on").checked = total ? (start > 0.05 || end < total - 0.05) : true;
  updateTrimUI();
}

function resetRange() {
  const total = (current && current.duration) || duration() || 0;
  $("start").value = formatTime(0);
  $("end").value = formatTime(total);
  $("trim-on").checked = false;
  updateTrimUI();
}

function updateTrimUI() {
  const clip = getClip();
  const total = duration();
  $("start").classList.toggle("invalid", isNaN(clip.start));
  $("end").classList.toggle("invalid", isNaN(clip.end) || !clip.valid);
  $("clip-dur").value = clip.valid ? formatTime(clip.end - clip.start) : "–";
  $("clip-len").textContent = clip.valid ? `${(clip.end - clip.start).toFixed(2)} seconds` : "End must be after start";
  $("strip-total").textContent = formatTime(total);
  $("bubble-start").textContent = isNaN(clip.start) ? "–" : formatTime(clip.start);
  $("bubble-end").textContent = isNaN(clip.end) ? "–" : formatTime(clip.end);
  const range = $("range");
  let a = 0, b = 1;
  if (total && clip.valid) {
    a = Math.min(clip.start, total) / total;
    b = Math.min(clip.end, total) / total;
  }
  range.style.left = `${a * 100}%`;
  range.style.width = `${(b - a) * 100}%`;
  $("timeline").style.setProperty("--sel-a", `${a * 100}%`);  // dims the frames outside the clip
  $("timeline").style.setProperty("--sel-b", `${b * 100}%`);
  $("trim-reset").disabled = !$("trim-on").checked;
  updateDownloadButton();
}

for (const id of ["start", "end"]) {
  $(id).addEventListener("input", () => {
    const total = duration();
    const clip = getClip();
    $("trim-on").checked = clip.valid && total ? (clip.start > 0.05 || clip.end < total - 0.05) : true;
    updateTrimUI();
  });
  $(id).addEventListener("change", () => {
    const t = parseTime($(id).value);
    if (!isNaN(t)) $(id).value = formatTime(t);
    if (!isNaN(t) && player.src) player.currentTime = Math.min(t, duration());
    updateTrimUI();
  });
}

$("set-start").addEventListener("click", () => {
  const end = parseTime($("end").value);
  setRange(player.currentTime, isNaN(end) || end <= player.currentTime ? duration() : end);
});
$("set-end").addEventListener("click", () => {
  const start = parseTime($("start").value);
  setRange(isNaN(start) || start >= player.currentTime ? 0 : start, player.currentTime);
});
$("trim-reset").addEventListener("click", resetRange);

let stopAt = null;
$("play-clip").addEventListener("click", () => {
  const clip = getClip();
  if (!clip.valid) return;
  player.currentTime = clip.start;
  stopAt = clip.end;
  player.play();
});

// Drag the start / end handles on the filmstrip.
function timeAt(clientX) {
  const rect = $("timeline").getBoundingClientRect();
  return Math.min(Math.max(0, (clientX - rect.left) / rect.width), 1) * duration();
}

for (const which of ["start", "end"]) {
  const handle = $(`handle-${which}`);
  handle.addEventListener("pointerdown", (e) => {
    if (!duration()) return;
    e.preventDefault();
    e.stopPropagation();
    handle.setPointerCapture(e.pointerId);
    handle.classList.add("dragging");
    const move = (ev) => {
      const t = timeAt(ev.clientX);
      const clip = getClip();
      if (which === "start") setRange(Math.min(t, clip.end - 0.1), clip.end);
      else setRange(clip.start, Math.max(t, clip.start + 0.1));
      if (player.getAttribute("src")) player.currentTime = which === "start" ? getClip().start : getClip().end;
    };
    const up = () => {
      handle.classList.remove("dragging");
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  });
  // Keyboard: arrows nudge the handle by one frame (Shift: one second).
  handle.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    e.stopPropagation();
    const step = (e.shiftKey ? 1 : 1 / ((current && current.fps) || 30)) * (e.key === "ArrowLeft" ? -1 : 1);
    const clip = getClip();
    if (which === "start") setRange(Math.min(clip.start + step, clip.end - 0.1), clip.end);
    else setRange(clip.start, Math.max(clip.end + step, clip.start + 0.1));
  });
}

$("timeline").addEventListener("click", (e) => {
  if (e.target.closest(".handle") || !player.getAttribute("src")) return;
  player.currentTime = timeAt(e.clientX);
});

// ---------- Filmstrip: real frames from the preview, drawn into a canvas ----------

let stripJob = 0;
async function drawFilmstrip() {
  const job = ++stripJob;
  const canvas = $("strip-canvas");
  const box = $("timeline");
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const w = box.clientWidth, h = box.clientHeight;
  if (!w || !h) return;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!current || !current.preview) return;

  const src = document.createElement("video");
  src.muted = true;
  src.preload = "auto";
  src.src = current.preview;
  try {
    await new Promise((ok, fail) => {
      src.addEventListener("loadeddata", ok, { once: true });
      src.addEventListener("error", fail, { once: true });
      setTimeout(fail, 15000);
    });
  } catch { return; }
  const total = src.duration || duration();
  if (!total || job !== stripJob) return;
  const tileW = Math.max(40, h * (src.videoWidth / src.videoHeight || 16 / 9));
  const count = Math.max(4, Math.ceil(w / tileW));
  const tw = canvas.width / count;
  for (let i = 0; i < count; i++) {
    if (job !== stripJob) return;
    src.currentTime = Math.min(total - 0.05, ((i + 0.5) / count) * total);
    try {
      await new Promise((ok, fail) => {
        src.addEventListener("seeked", ok, { once: true });
        setTimeout(fail, 5000);
      });
    } catch { continue; }
    // Cover-fit each frame into its tile.
    const vr = src.videoWidth / src.videoHeight, tr = tw / canvas.height;
    let sw = src.videoWidth, sh = src.videoHeight, sx = 0, sy = 0;
    if (vr > tr) { sw = sh * tr; sx = (src.videoWidth - sw) / 2; } else { sh = sw / tr; sy = (src.videoHeight - sh) / 2; }
    ctx.drawImage(src, sx, sy, sw, sh, i * tw, 0, tw + 1, canvas.height);
  }
  src.removeAttribute("src");
  src.load();
}

let stripResize = null;
window.addEventListener("resize", () => {
  if (result.hidden) return;
  clearTimeout(stripResize);
  stripResize = setTimeout(drawFilmstrip, 300);
});

// ---------- Player helpers ----------

function updatePlayhead() {
  $("now").textContent = formatTime(player.currentTime);
  const total = duration();
  $("playhead").style.left = total ? `${(player.currentTime / total) * 100}%` : "0";
}

player.addEventListener("timeupdate", () => {
  updatePlayhead();
  if (stopAt !== null && player.currentTime >= stopAt) {
    player.pause();
    stopAt = null;
  }
});
player.addEventListener("seeked", updatePlayhead);
player.addEventListener("pause", () => { stopAt = null; });
player.addEventListener("loadedmetadata", () => {
  if (!$("trim-on").checked || isNaN(parseTime($("end").value))) $("end").value = formatTime(player.duration);
  updateTrimUI();
  updatePlayhead();
});
player.addEventListener("error", () => {
  if (!player.getAttribute("src")) return;
  $("no-preview").hidden = false;
  $("no-preview").textContent = "The preview couldn't be played. You can still type times and download.";
});

function seekBy(seconds) {
  player.pause();
  player.currentTime = Math.min(Math.max(0, player.currentTime + seconds), duration());
}

$("step-row").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  if (btn.dataset.step) seekBy(parseFloat(btn.dataset.step));
  if (btn.dataset.frames) seekBy(parseFloat(btn.dataset.frames) / ((current && current.fps) || 30));
});

// Clear (×) button in the link box
const clearBtn = $("clear-url");
const syncClear = () => { clearBtn.hidden = !input.value; };
input.addEventListener("input", syncClear);
clearBtn.addEventListener("click", () => { input.value = ""; syncClear(); input.focus(); });

// Editor-style shortcuts: Space / K play-pause, J / L ∓1s, , / . one frame, I / O mark in / out.
document.addEventListener("keydown", (e) => {
  if (result.hidden || e.ctrlKey || e.metaKey || e.altKey || settingsDialog.open) return;
  const target = e.target instanceof Element ? e.target : null;
  // Don't steal keys while typing; let Space/Enter press a focused button as usual.
  const typing = target && target.matches(
    "input:not([type=checkbox]):not([type=radio]), select, textarea, [contenteditable]");
  if (typing || (e.key === " " && target && target.matches("button, [role=tab], label"))) return;
  const fps = (current && current.fps) || 30;
  const hasPlayer = Boolean(player.getAttribute("src"));
  const key = e.key.toLowerCase();
  const actions = {
    ",": () => seekBy(-1 / fps),
    ".": () => seekBy(1 / fps),
    "j": () => seekBy(-1),
    "l": () => seekBy(1),
    "k": () => player.pause(),
    " ": () => (player.paused ? player.play() : player.pause()),
    "i": () => $("set-start").click(),
    "o": () => $("set-end").click(),
  };
  if (!hasPlayer || !actions[key]) return;
  e.preventDefault();
  actions[key]();
});

// ---------- Inspector tabs (Export / Frame / Thumbnail) ----------
const TABS = ["export", "frame", "thumb"];

function selectTab(name) {
  for (const t of TABS) {
    const tab = $(`tab-${t}`), panel = $(`panel-${t}`);
    const on = t === name;
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
    panel.hidden = !on;
  }
}

TABS.forEach((t, i) => {
  const tab = $(`tab-${t}`);
  tab.addEventListener("click", () => selectTab(t));
  tab.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const visible = TABS.filter((x) => !$(`tab-${x}`).hidden);
    const next = visible[(visible.indexOf(t) + (e.key === "ArrowRight" ? 1 : visible.length - 1)) % visible.length];
    selectTab(next);
    $(`tab-${next}`).focus();
  });
});

// ---------- Downloads ----------

function setButtonsDisabled(disabled) {
  document.querySelectorAll("#result button:not(#cancel)").forEach((b) => (b.disabled = disabled));
}

async function fetchFile(url, fallbackName) {
  const res = await fetch(url);
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || "Download failed.");
  }
  const blob = await res.blob();
  const name = getFilename(res.headers.get("Content-Disposition")) || fallbackName;
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 10000);
  return name;
}

// ---------- Downloads go to the browser's own download manager ----------

const toastBox = document.createElement("div");
toastBox.className = "toasts";
toastBox.setAttribute("aria-live", "polite");
document.body.appendChild(toastBox);

// A small notice in the corner; returns an object to update or close it.
function toast(title, detail = "", kind = "", ms = 0) {
  const el = document.createElement("div");
  el.className = "toast" + (kind ? ` ${kind}` : "");
  el.innerHTML = '<div class="toast-body"><b></b><span></span></div><button type="button" class="toast-x" aria-label="Dismiss">&times;</button>';
  toastBox.appendChild(el);
  let timer = null;
  const api = {
    update(t, d = "", k = "", hideAfter = 0) {
      el.className = "toast" + (k ? ` ${k}` : "");
      el.querySelector("b").textContent = t;
      el.querySelector("span").textContent = d;
      clearTimeout(timer);
      if (hideAfter) timer = setTimeout(api.close, hideAfter);
    },
    close() { el.classList.add("leaving"); setTimeout(() => el.remove(), 250); },
  };
  el.querySelector(".toast-x").addEventListener("click", api.close);
  api.update(title, detail, kind, ms);
  return api;
}

function browserDownload(href) {
  const link = document.createElement("a");
  link.href = href;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

const DOWNLOADS_HINT = /Mac/i.test(navigator.platform) ? "⌥⌘L" : "Ctrl+J";

async function startDownload(opt) {
  const body = { url: current.url, format: opt.format, compat: $("compat-on").checked,
                 title: current.title, token: current.token };
  if ($("trim-on").checked) {
    const clip = getClip();
    if (!clip.valid) { setStatus("Fix the start and end times first.", "error"); return; }
    body.start = clip.start.toFixed(3);
    body.end = clip.end.toFixed(3);
    body.exact = $("cut-mode").value === "exact";
  }

  const note = toast(`Starting ${opt.label}...`, "Checking the best way to download it.");
  try {
    const res = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Could not start the download.");

    // Hand the file to the browser: it appears in the browser's downloads list right away.
    browserDownload(`/api/jobs/${data.id}/stream`);
    if (data.direct) {
      note.update("Downloading in your browser", `${data.name} · open your downloads (${DOWNLOADS_HINT}) to follow it.`, "ok", 6000);
      return;
    }
    note.update("Preparing your file", `${data.name} is in your browser's downloads (${DOWNLOADS_HINT}). It stays at 0 B while the server prepares it, then finishes.`);
    await watchPreparedJob(data.id, data.name, note);
  } catch (err) {
    note.update("Download failed", err.message, "error", 12000);
  }
}

// Quietly follow a prepared download so the notice can say what's happening.
async function watchPreparedJob(id, name, note) {
  while (true) {
    await new Promise((r) => setTimeout(r, 1000));
    let job;
    try {
      const r = await fetch(`/api/jobs/${id}`);
      job = await r.json();
      if (!r.ok) throw new Error(job.error);
    } catch {
      return; // server restarted or job expired: the browser download shows the outcome
    }
    if (job.status === "running") {
      const pct = job.percent != null ? ` · ${job.percent.toFixed(0)}%` : "";
      const left = job.eta ? ` · ${formatDuration(job.eta)} left` : "";
      note.update("Preparing your file", `${job.phase || "Working"}${pct}${left}`);
      continue;
    }
    if (job.status === "done") {
      note.update("Ready", `${name} is finishing in your browser's downloads.`, "ok", 6000);
    } else {
      note.update("Download failed", job.error || "The download was cancelled.", "error", 15000);
    }
    return;
  }
}

async function captureFrame(fmt) {
  player.pause();
  const t = player.getAttribute("src") ? player.currentTime : (parseTime($("start").value) || 0);
  const params = new URLSearchParams({ token: current.token, t: t.toFixed(3), fmt });
  setButtonsDisabled(true);
  setStatus(`Capturing full-resolution frame at ${formatTime(t)}...`);
  try {
    const name = await fetchFile(`/api/frame?${params}`, `frame.${fmt}`);
    setStatus(`Saved "${name}"`, "done");
  } catch (err) {
    setStatus(err.message, "error");
  } finally {
    setButtonsDisabled(false);
  }
}

// ---------- Thumbnails ----------

let thumbnails = [];

function thumbFormat() {
  return document.querySelector('input[name="thumb-fmt"]:checked').value;
}

function thumbNote(text, kind = "") {
  const note = $("thumb-note");
  note.hidden = !text;
  note.className = "panel-note" + (kind ? ` ${kind}` : "");
  note.textContent = text || "";
}

function sizeName(w, h) {
  const p = Math.min(w, h);
  if (p >= 1080) return "Full HD";
  if (p >= 720) return "HD";
  if (p >= 480) return "SD";
  return "Small";
}

function renderThumbnails() {
  const list = $("thumb-list");
  list.innerHTML = "";
  const fmt = thumbFormat();
  thumbnails.forEach((t, i) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "option" + (i === 0 ? " best" : "");
    btn.innerHTML =
      `<svg class="icon opt-icon"><use href="#i-image"/></svg>` +
      `<span class="opt-label"></span><span class="opt-tag"></span><span class="opt-meta"></span>` +
      `<svg class="icon opt-go"><use href="#i-download"/></svg>`;
    btn.querySelector(".opt-label").textContent = `${t.width} × ${t.height}`;
    btn.querySelector(".opt-tag").textContent = sizeName(t.width, t.height);
    btn.querySelector(".opt-meta").textContent = (fmt === "original" ? t.format : fmt).toUpperCase();
    btn.addEventListener("click", () => downloadThumbnail(t, btn));
    list.appendChild(btn);
  });
}

async function loadThumbnails(token) {
  thumbnails = [];
  thumbNote("");
  $("thumb-list").innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
  try {
    const res = await fetch(`/api/thumbnails?token=${token}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Couldn't load thumbnails.");
    if (!current || current.token !== token) return; // a different video was loaded meanwhile
    thumbnails = data.thumbnails;
    renderThumbnails();
    if (!thumbnails.length) thumbNote("This video has no downloadable thumbnail.");
  } catch (err) {
    $("thumb-list").innerHTML = "";
    thumbNote(err.message, "error");
  }
}

async function downloadThumbnail(t, btn) {
  const fmt = thumbFormat();
  const params = new URLSearchParams({ token: current.token, i: t.index, fmt });
  btn.disabled = true;
  btn.classList.add("is-busy");
  thumbNote("");
  try {
    const name = await fetchFile(`/api/thumbnail?${params}`, `thumbnail.${fmt === "original" ? t.format : fmt}`);
    thumbNote(`Saved "${name}"`, "ok");
  } catch (err) {
    thumbNote(err.message, "error");
  } finally {
    btn.disabled = false;
    btn.classList.remove("is-busy");
  }
}

document.querySelectorAll('input[name="thumb-fmt"]').forEach((r) => r.addEventListener("change", renderThumbnails));

$("snap-png").addEventListener("click", () => captureFrame("png"));
$("snap-jpg").addEventListener("click", () => captureFrame("jpg"));

function getFilename(header) {
  if (!header) return null;
  const star = header.match(/filename\*=UTF-8''([^;]+)/i);
  if (star) return decodeURIComponent(star[1]);
  const plain = header.match(/filename="?([^";]+)"?/i);
  return plain ? plain[1] : null;
}

// ---------- Media gallery (photos, carousels, multi-video posts) ----------

let galleryToken = null;

function galleryNote(text, kind = "") {
  const note = $("g-note");
  note.hidden = !text;
  note.className = "panel-note" + (kind ? ` ${kind}` : "");
  note.textContent = text || "";
}

function mediaUrl(token, i, inline = false) {
  return `/api/media/file?${new URLSearchParams({ token, i, ...(inline ? { inline: 1 } : {}) })}`;
}

function renderGallery(data, items, { standalone }) {
  galleryToken = data.token;
  setBadge($("g-site"), data.site);
  $("g-site").hidden = !standalone;
  $("g-title").textContent = standalone ? data.title : "More media in this post";
  $("g-uploader").textContent = standalone && data.uploader ? `by ${data.uploader}` : "";
  const photos = items.filter((m) => m.kind === "image").length;
  const videos = items.filter((m) => m.kind === "video").length;
  $("g-count").textContent = [photos && `${photos} photo${photos > 1 ? "s" : ""}`,
                              videos && `${videos} video${videos > 1 ? "s" : ""}`].filter(Boolean).join(" · ");
  $("g-zip").hidden = items.length < 2;
  galleryNote("");

  const grid = $("g-grid");
  grid.innerHTML = "";
  items.forEach((m) => {
    const card = document.createElement("figure");
    card.className = "media-card";
    const frame = document.createElement("div");
    frame.className = "media-frame";
    if (m.kind === "image") {
      const img = document.createElement("img");
      img.decoding = "async";
      img.alt = `Image ${m.index + 1}`;
      img.src = mediaUrl(data.token, m.index, true);
      frame.appendChild(img);
    } else if (m.kind === "audio") {
      const aud = document.createElement("audio");
      aud.src = mediaUrl(data.token, m.index, true);
      aud.controls = true;
      aud.preload = "none";
      frame.innerHTML = '<div class="media-placeholder"><svg class="icon"><use href="#i-music"/></svg><span>Sound</span></div>';
      frame.appendChild(aud);
    } else if (m.direct) {
      const vid = document.createElement("video");
      vid.src = mediaUrl(data.token, m.index, true);
      vid.preload = "metadata";
      vid.muted = true;
      vid.controls = true;
      vid.playsInline = true;
      frame.appendChild(vid);
    } else {
      frame.innerHTML = '<div class="media-placeholder"><svg class="icon"><use href="#i-film"/></svg><span>Video</span></div>';
    }
    if (m.kind === "video") frame.insertAdjacentHTML("beforeend", '<span class="media-kind">Video</span>');

    const foot = document.createElement("figcaption");
    foot.className = "media-foot";
    const info = document.createElement("span");
    info.className = "media-info";
    const size = m.width && m.height ? `${m.width} × ${m.height}` : "";
    info.innerHTML = `<b>#${m.index + 1}</b><span></span>`;
    info.lastChild.textContent = [size, (m.ext || "").toUpperCase()].filter(Boolean).join(" · ");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-soft btn-sm";
    btn.innerHTML = '<svg class="icon"><use href="#i-download"/></svg>Save';
    btn.addEventListener("click", () => saveMedia(m, btn));
    foot.append(info, btn);
    card.append(frame, foot);
    grid.appendChild(card);
  });
  $("gallery").hidden = false;
}

function showGalleryOnly(data) {
  current = null;
  result.hidden = true;
  $("features").hidden = true;
  document.body.classList.add("workspace-open");
  renderGallery(data, data.media, { standalone: true });
}

async function loadPostMedia(data) {
  try {
    const res = await fetch(`/api/media?token=${data.token}`);
    const body = await res.json();
    if (!res.ok || !current || current.token !== data.token) return;
    const items = body.media || [];
    // Only worth showing when the post has more than the video already on screen.
    if (items.length > 1 || items.some((m) => m.kind === "image")) {
      renderGallery(data, items, { standalone: false });
    }
  } catch { /* the gallery is optional extra content */ }
}

async function saveMedia(m, btn) {
  btn.disabled = true;
  galleryNote(m.direct ? "" : "Preparing the video with sound, this can take a moment...");
  try {
    const name = await fetchFile(mediaUrl(galleryToken, m.index), `media-${m.index + 1}.${m.ext}`);
    galleryNote(`Saved "${name}"`, "ok");
  } catch (err) {
    galleryNote(err.message, "error");
  } finally {
    btn.disabled = false;
  }
}

$("g-zip").addEventListener("click", async () => {
  const btn = $("g-zip");
  btn.disabled = true;
  galleryNote("Collecting every file into a ZIP...");
  try {
    const name = await fetchFile(`/api/media/zip?token=${galleryToken}`, "media.zip");
    galleryNote(`Saved "${name}"`, "ok");
  } catch (err) {
    galleryNote(err.message, "error");
  } finally {
    btn.disabled = false;
  }
});

// ---------- Login settings ----------

const settingsDialog = $("settings");

function settingsStatus(text, kind = "") {
  const el = $("settings-status");
  el.hidden = !text;
  el.className = "panel-note" + (kind ? ` ${kind}` : "");
  el.textContent = text || "";
}

function applySettings(data) {
  $("browser-select").value = data.browser || "";
  $("cookie-state").textContent = data.cookie_file ? "cookies.txt in use" : "No file";
  $("cookie-remove").hidden = !data.cookie_file;
  const label = data.active === "file" ? "Login: cookies.txt"
    : data.active === "browser" ? `Login: ${$("browser-select").selectedOptions[0].textContent.split(" (")[0]}`
    : "Login: off";
  $("login-label").textContent = label;
  $("open-settings").classList.toggle("is-on", Boolean(data.active));
}

async function refreshSettings() {
  try {
    const res = await fetch("/api/settings");
    applySettings(await res.json());
  } catch { /* keep the current labels */ }
}

function openSettings() {
  settingsStatus("");
  refreshSettings();
  settingsDialog.showModal();
}

$("open-settings").addEventListener("click", openSettings);
$("close-settings").addEventListener("click", () => settingsDialog.close());
settingsDialog.addEventListener("click", (e) => { if (e.target === settingsDialog) settingsDialog.close(); });

$("browser-select").addEventListener("change", async (e) => {
  const browser = e.target.value;
  settingsStatus(browser ? "Reading your browser's login..." : "Turning login off...");
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ browser }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Couldn't save the setting.");
    settingsStatus(browser ? `On: using ${e.target.selectedOptions[0].textContent.split(" (")[0]}'s login (${data.cookies} cookies found).` : "Login is off.", "ok");
  } catch (err) {
    settingsStatus(err.message, "error");
  }
  refreshSettings();
});

$("cookie-file").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  settingsStatus("Saving cookies.txt...");
  try {
    const res = await fetch("/api/settings/cookies", { method: "POST", body });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Couldn't use that file.");
    settingsStatus(`cookies.txt saved (${data.cookies} cookies). Fetch the post again.`, "ok");
  } catch (err) {
    settingsStatus(err.message, "error");
  }
  e.target.value = "";
  refreshSettings();
});

$("cookie-remove").addEventListener("click", async () => {
  await fetch("/api/settings/cookies", { method: "DELETE" });
  settingsStatus("cookies.txt removed.", "ok");
  refreshSettings();
});

refreshSettings();

// ---------- Editor-friendly (H.264) mode ----------
// Remembered per browser; on by default because most editors can't open AV1/VP9.
(() => {
  const box = $("compat-on");
  try {
    const saved = localStorage.getItem("compat");
    if (saved !== null) box.checked = saved === "1";
  } catch { /* storage blocked: keep the default */ }
  const sync = () => {
    $("compat-hint").textContent = box.checked
      ? "Opens in Premiere Pro, DaVinci Resolve, CapCut and others. 1440p/4K is converted after download."
      : "Keeps the site's original codec (often AV1/VP9 on YouTube): smaller files, but many editors show only the audio.";
  };
  box.addEventListener("change", () => {
    try { localStorage.setItem("compat", box.checked ? "1" : "0"); } catch { /* ignore */ }
    sync();
    updateCodecLabels();
  });
  sync();
})();

// ---------- Landing page buttons ----------
// "Try an example": a short public NASA video that works without login.
const EXAMPLE_URL = "https://x.com/NASA/status/2041690396586090592";
$("try-example").addEventListener("click", () => {
  input.value = EXAMPLE_URL;
  input.dispatchEvent(new Event("input"));
  form.requestSubmit();
});

// "Start now" / "Start downloading": jump to the link box and put the cursor in it.
for (const id of ["nav-start", "cta-start"]) {
  $(id).addEventListener("click", (e) => {
    e.preventDefault();
    window.scrollTo({ top: 0, behavior: "smooth" });
    setTimeout(() => input.focus({ preventScroll: true }), 350);
  });
}

/* ---------- Back to the start page ---------- */
$("new-download").addEventListener("click", (e) => {
  e.preventDefault();
  player.pause();
  result.hidden = true;
  $("gallery").hidden = true;
  $("features").hidden = false;
  document.body.classList.remove("workspace-open");
  setStatus("");
  input.value = "";
  input.dispatchEvent(new Event("input"));
  window.scrollTo({ top: 0, behavior: "smooth" });
  input.focus({ preventScroll: true });
});
