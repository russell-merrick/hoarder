const $ = (id) => document.getElementById(id);

const MIME = {
  ".wav": "audio/wav",
  ".aif": "audio/aiff",
  ".aiff": "audio/aiff",
  ".flac": "audio/flac",
  ".mp3": "audio/mpeg",
  ".ogg": "audio/ogg",
  ".opus": "audio/ogg",
};

const PAGE = 1000;
const LEN_MAX = 1000;
const SEC_HI = 120;
const ROOTS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const FLAT = { Db: "C#", Eb: "D#", Gb: "F#", Ab: "G#", Bb: "A#" };

const ROLE_GROUPS = [
  [
    ["kick", "Kick"],
    ["hat", "Hat"],
    ["snare", "Snare"],
    ["shaker", "Shaker"],
  ],
  [
    ["bass", "Bass"],
    ["lead", "Lead"],
    ["arp", "Arp"],
    ["synth", "Synth"],
  ],
  [
    ["vocals", "Vocals"],
    ["chops", "Chops"],
  ],
  [
    ["fx", "FX"],
    ["riser", "Riser"],
    ["build", "Build"],
    ["noise", "White noise"],
  ],
  [["loop", "Loop"]],
];

const ROLE_LABEL = Object.fromEntries(ROLE_GROUPS.flat());
const selectedRoles = new Set();

const GENRE_GROUPS = [
  [
    ["techno", "Techno"],
    ["trance", "Trance"],
    ["house", "House"],
    ["dnb", "Drum & Bass"],
    ["dubstep", "Dubstep"],
    ["phonk", "Phonk"],
    ["eurodance", "Eurodance"],
    ["hardstyle", "Hardstyle"],
    ["garage", "Garage"],
  ],
  [
    ["trap", "Trap"],
    ["pop", "Pop"],
    ["hiphop", "Hip hop"],
    ["ambient", "Ambient"],
    ["disco", "Disco"],
  ],
];

const GENRE_LABEL = Object.fromEntries(GENRE_GROUPS.flat());
const genreMode = new Map();

const audio = $("audio");
const wave = $("wave");
let offset = 0;
let total = 0;
let pageItems = [];
let scanning = false;
let current = null;
let peaks = null;
let listToken = 0;
let blobUrl = null;
let loadToken = 0;
const fileCache = new Map();
const inflight = new Map();

function mimeFor(ext) {
  return MIME[ext] || "application/octet-stream";
}

function rootName(key) {
  const match = /^([A-G])([#b])?/.exec(key || "");
  if (!match) return "";
  if (match[2] === "b") return FLAT[match[1] + "b"] || "";
  return match[1] + (match[2] || "");
}

function semitonesBetween(from, to) {
  const start = ROOTS.indexOf(rootName(from));
  const end = ROOTS.indexOf(rootName(to));
  if (start < 0 || end < 0) return 0;
  let delta = (end - start) % 12;
  if (delta < 0) delta += 12;
  if (delta > 6) delta -= 12;
  return delta;
}

function targetKey() {
  return $("target-key").value;
}

function shiftOf(sample) {
  if (!sample || !sample.key || !targetKey()) return 0;
  return semitonesBetween(sample.key, targetKey());
}

let project = { running: false, bpm: null, name: "" };

function matchQuery(sample) {
  const params = new URLSearchParams();
  const shift = shiftOf(sample);
  if (shift) params.set("semitones", String(shift));
  if (project.bpm && sample.bpm) params.set("bpm", String(project.bpm));
  const text = params.toString();
  return text ? `?${text}` : "";
}

function pitchedLabel(sample) {
  if (!sample) return "No sample selected";
  let label = sample.name;
  const target = targetKey();
  if (target && sample.key) {
    const shift = semitonesBetween(sample.key, target);
    if (!shift) label = `${label} → ${target}`;
    else {
      const sign = shift > 0 ? `+${shift}` : String(shift);
      label = `${label} → ${target} (${sign})`;
    }
  }
  if (project.bpm && sample.bpm) label = `${label} · ${Math.round(project.bpm)} BPM`;
  return label;
}

function applyPitch() {
  audio.preservesPitch = true;
  audio.mozPreservesPitch = true;
  audio.playbackRate = 1;
}

function safeName(name) {
  return (name || "sample").replace(/[:\\/\r\n"]/g, " ").trim() || "sample";
}

function fmtDur(seconds) {
  if (seconds == null || Number.isNaN(seconds)) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

function fmtClock(seconds) {
  if (!Number.isFinite(seconds)) return "0:00";
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

function folderText(sample) {
  const folder = (sample.folder || sample.pack || "").replace(/\\/g, " / ");
  return folder;
}

function fillMeta(meta, sample) {
  const tags = Array.isArray(sample.roles) ? sample.roles : [];
  for (const id of tags) {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = ROLE_LABEL[id] || id;
    meta.append(tag);
  }
  const genres = Array.isArray(sample.genres) ? sample.genres : [];
  for (const id of genres) {
    const tag = document.createElement("span");
    tag.className = "tag genre";
    tag.textContent = GENRE_LABEL[id] || id;
    meta.append(tag);
  }
  const rest = folderText(sample);
  if (rest) meta.append(document.createTextNode(rest));
}

function params() {
  const query = new URLSearchParams();
  const q = $("q").value.trim();
  if (q) query.set("q", q);
  const roles = ROLE_GROUPS.flat()
    .map(([id]) => id)
    .filter((id) => selectedRoles.has(id));
  if (roles.length) query.set("roles", roles.join(","));
  const genres = GENRE_GROUPS.flat()
    .map(([id]) => id)
    .filter((id) => genreMode.get(id) === "include");
  const excluded = GENRE_GROUPS.flat()
    .map(([id]) => id)
    .filter((id) => genreMode.get(id) === "exclude");
  if (genres.length) query.set("genres", genres.join(","));
  if (excluded.length) query.set("exclude_genres", excluded.join(","));
  const bounds = lengthBounds();
  if (bounds.min != null) query.set("min_sec", String(bounds.min));
  if (bounds.max != null) query.set("max_sec", String(bounds.max));
  if ($("hide-dupes").checked) query.set("unique", "1");
  query.set("limit", String(PAGE));
  query.set("offset", String(offset));
  return query;
}

function posToSec(pos) {
  if (pos <= 0) return 0;
  const t = pos / LEN_MAX;
  return 0.05 * Math.pow(SEC_HI / 0.05, t);
}

function roundSec(seconds) {
  if (seconds < 1) return Math.round(seconds * 100) / 100;
  if (seconds < 10) return Math.round(seconds * 10) / 10;
  return Math.round(seconds);
}

function fmtLen(seconds) {
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  return `${Math.round(seconds)}s`;
}

function lengthBounds() {
  const minPos = Number($("len-min").value);
  const maxPos = Number($("len-max").value);
  return {
    min: minPos <= 0 ? null : roundSec(posToSec(minPos)),
    max: maxPos >= LEN_MAX ? null : roundSec(posToSec(maxPos)),
  };
}

function paintLength() {
  const minPos = Number($("len-min").value);
  const maxPos = Number($("len-max").value);
  const fill = $("length-fill");
  const span = Math.max(0, maxPos - minPos) / LEN_MAX;
  fill.style.left = `calc(6px + (100% - 12px) * ${minPos / LEN_MAX})`;
  fill.style.width = `calc((100% - 12px) * ${span})`;
  const { min, max } = lengthBounds();
  const label = $("length-label");
  if (min == null && max == null) label.textContent = "Any length";
  else if (min == null) label.textContent = `≤ ${fmtLen(max)}`;
  else if (max == null) label.textContent = `≥ ${fmtLen(min)}`;
  else label.textContent = `${fmtLen(min)} – ${fmtLen(max)}`;
}

function onLengthInput(which) {
  const minEl = $("len-min");
  const maxEl = $("len-max");
  let min = Number(minEl.value);
  let max = Number(maxEl.value);
  if (max - min < 1) {
    if (which === "min") minEl.value = String(Math.max(0, max - 1));
    else maxEl.value = String(Math.min(LEN_MAX, min + 1));
  }
  paintLength();
  scheduleList();
}

function renderRoles() {
  const box = $("roles");
  box.replaceChildren();
  for (const group of ROLE_GROUPS) {
    const wrap = document.createElement("div");
    wrap.className = "sounds-group";
    for (const [id, label] of group) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "chip";
      button.textContent = label;
      button.setAttribute("aria-pressed", "false");
      button.addEventListener("click", () => {
        if (selectedRoles.has(id)) selectedRoles.delete(id);
        else selectedRoles.add(id);
        const on = selectedRoles.has(id);
        button.classList.toggle("on", on);
        button.setAttribute("aria-pressed", on ? "true" : "false");
        offset = 0;
        refreshList();
      });
      wrap.append(button);
    }
    box.append(wrap);
  }
}

function paintGenre(button, id, label) {
  const mode = genreMode.get(id) || "";
  button.classList.toggle("on", mode === "include");
  button.classList.toggle("exclude", mode === "exclude");
  button.textContent = mode === "exclude" ? `Not ${label}` : label;
  button.setAttribute("aria-pressed", mode === "include" ? "true" : "false");
}

function renderGenres() {
  const box = $("genres");
  box.replaceChildren();
  for (const group of GENRE_GROUPS) {
    const wrap = document.createElement("div");
    wrap.className = "sounds-group";
    for (const [id, label] of group) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "chip genre";
      paintGenre(button, id, label);
      button.addEventListener("click", () => {
        const mode = genreMode.get(id);
        if (!mode) genreMode.set(id, "include");
        else if (mode === "include") genreMode.set(id, "exclude");
        else genreMode.delete(id);
        paintGenre(button, id, label);
        offset = 0;
        refreshList();
      });
      wrap.append(button);
    }
    box.append(wrap);
  }
}

async function refreshProject() {
  try {
    const response = await fetch("/api/project");
    if (!response.ok) return;
    const body = await response.json();
    const next = body.bpm ? Math.round(body.bpm) : null;
    const prev = project.bpm ? Math.round(project.bpm) : null;
    project = body;
    const label = $("project-bpm");
    if (body.running && next) {
      label.hidden = false;
      label.textContent = `Live ${next}`;
      label.title = body.name || "";
    } else {
      label.hidden = true;
      label.textContent = "";
    }
    if (next !== prev && current && !audio.paused) play(current);
  } catch (_) {}
}

async function refreshStatus() {
  const response = await fetch("/api/status");
  if (!response.ok) return;
  const body = await response.json();
  scanning = Boolean(body.scanning);
  const bits = (body.roots || []).map((root) => {
    const count = (root.count || 0).toLocaleString();
    return root.online ? `${count} ${root.label.toLowerCase()}` : `${root.label} offline`;
  });
  $("counts").textContent = bits.join(" · ") || "—";
  const offline = (body.roots || []).filter((root) => !root.online).map((root) => root.label);
  $("scan-note").textContent = scanning
    ? `Scanning… ${body.seen.toLocaleString()} files`
    : offline.length
      ? `${offline.join(", ")} offline`
      : "";
  $("scan-note").classList.toggle("warn", !scanning && offline.length > 0);
  $("rescan").disabled = scanning;
  syncResultsBar();
}

async function refreshList() {
  const token = ++listToken;
  const response = await fetch(`/api/samples?${params()}`);
  if (token !== listToken) return;
  if (!response.ok) {
    $("result-count").textContent = "Couldn't load the catalog";
    $("pager").hidden = true;
    syncResultsBar();
    return;
  }
  const body = await response.json();
  if (token !== listToken) return;
  total = body.total;
  drawList(body);
}

function drawList(body) {
  pageItems = body.items;
  const hits = $("hits");
  hits.replaceChildren();
  if (!body.items.length) {
    const empty = document.createElement("li");
    empty.className = "hit-empty";
    empty.textContent = scanning ? "Scanning the library…" : "No samples match these filters.";
    hits.appendChild(empty);
  }
  for (const sample of body.items) {
    hits.appendChild(renderHit(sample));
  }
  const pages = Math.max(1, Math.ceil(body.total / PAGE));
  const onePage = pages <= 1;
  const from = body.total ? body.offset + 1 : 0;
  const to = body.offset + body.items.length;
  $("result-count").textContent = !body.total
    ? scanning
      ? "Scanning…"
      : "No samples match"
    : onePage
      ? ""
      : `${from.toLocaleString()}–${to.toLocaleString()} of ${body.total.toLocaleString()}`;
  const page = Math.floor(body.offset / PAGE) + 1;
  $("page").textContent = body.total && !onePage ? `Page ${page} of ${pages}` : "";
  $("pager").hidden = onePage;
  $("prev").disabled = body.offset <= 0;
  $("next").disabled = body.offset + PAGE >= body.total;
  syncResultsBar();
}

function syncResultsBar() {
  const hasCount = $("result-count").textContent.trim().length > 0;
  const hasNote = $("scan-note").textContent.trim().length > 0;
  $("results-bar").hidden = !hasCount && !hasNote;
}

function renderHit(sample) {
  const li = document.createElement("li");
  li.className = "hit" + (current && current.id === sample.id ? " on" : "");
  li.draggable = true;
  li.dataset.id = String(sample.id);
  li.title = sample.path || "";

  const play = document.createElement("button");
  play.type = "button";
  play.className = "hit-play";
  play.textContent = current && current.id === sample.id && !audio.paused ? "■" : "▶";
  play.addEventListener("click", (event) => {
    event.stopPropagation();
    toggle(sample);
  });

  const body = document.createElement("div");
  const name = document.createElement("div");
  name.className = "name";
  name.textContent = sample.name;
  const meta = document.createElement("div");
  meta.className = "meta";
  fillMeta(meta, sample);
  body.append(name, meta);

  const key = document.createElement("span");
  key.className = sample.key ? "key" : "key empty";
  key.textContent = sample.key || "—";

  const dur = document.createElement("span");
  dur.className = "dur";
  dur.textContent = fmtDur(sample.duration);

  li.append(play, body, key, dur);
  li.addEventListener("click", () => toggle(sample));
  li.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    event.stopPropagation();
    openFileMenu(sample, event.clientX, event.clientY);
  });
  bindDrag(li, () => sample);
  return li;
}

let menuSample = null;

function openFileMenu(sample, x, y) {
  menuSample = sample;
  const menu = $("file-menu");
  menu.hidden = false;
  menu.style.left = `${x}px`;
  menu.style.top = `${y}px`;
  const rect = menu.getBoundingClientRect();
  if (rect.right > window.innerWidth - 8) menu.style.left = `${Math.max(8, x - rect.width)}px`;
  if (rect.bottom > window.innerHeight - 8) menu.style.top = `${Math.max(8, y - rect.height)}px`;
}

function closeFileMenu() {
  menuSample = null;
  $("file-menu").hidden = true;
}

function reveal(sample) {
  fetch(`/api/samples/${sample.id}/reveal`, { method: "POST" }).then((response) => {
    if (!response.ok) $("now-path").textContent = "Couldn't open the folder.";
  }).catch(() => {
    $("now-path").textContent = "Couldn't open the folder.";
  });
}

function remember(id, file) {
  if (fileCache.has(id)) fileCache.delete(id);
  fileCache.set(id, file);
  while (fileCache.size > 6) fileCache.delete(fileCache.keys().next().value);
}

function warm(sample) {
  if (!sample || fileCache.has(sample.id) || inflight.has(sample.id)) return;
  const pending = fetch(`/api/samples/${sample.id}/file`)
    .then((response) => {
      if (!response.ok) throw new Error("missing file");
      return response.blob();
    })
    .then((blob) => {
      remember(sample.id, new File([blob], safeName(sample.name), { type: mimeFor(sample.ext) }));
    })
    .catch(() => {})
    .finally(() => inflight.delete(sample.id));
  inflight.set(sample.id, pending);
}

function dragFile(sample) {
  const query = matchQuery(sample);
  if (query) {
    const stem = safeName(sample.name).replace(/\.[^.]+$/i, "");
    const bits = [stem];
    if (shiftOf(sample) && targetKey()) bits.push(targetKey());
    if (project.bpm && sample.bpm) bits.push(`${Math.round(project.bpm)}bpm`);
    return {
      name: safeName(`${bits.join(" ")}.wav`),
      url: `${location.origin}/api/samples/${sample.id}/file${query}`,
      type: "audio/wav",
      file: null,
    };
  }
  return {
    name: safeName(sample.name),
    url: `${location.origin}/api/samples/${sample.id}/file`,
    type: mimeFor(sample.ext),
    file: fileCache.get(sample.id) || null,
  };
}

function bindDrag(el, getSample) {
  el.addEventListener("pointerdown", () => {
    const sample = getSample();
    if (sample && !matchQuery(sample)) warm(sample);
  });
  el.addEventListener("dragstart", (event) => {
    const sample = getSample();
    if (!sample) {
      event.preventDefault();
      return;
    }
    const drop = dragFile(sample);
    event.dataTransfer.effectAllowed = "copy";
    if (drop.file) {
      try {
        event.dataTransfer.items.add(drop.file);
      } catch (_) {
        /* DownloadURL below still hands Ableton a file. */
      }
    }
    // Chromium turns this into a file drop Live can place on a track.
    // A matched drop is a wav with the pitch and the project tempo baked in.
    // The cached original is left out of that drag.
    event.dataTransfer.setData("DownloadURL", `${drop.type}:${drop.name}:${drop.url}`);
    if (drop.file) {
      event.dataTransfer.setData("text/uri-list", drop.url);
      event.dataTransfer.setData("text/plain", drop.name);
    }
  });
}

function showCurrent(sample) {
  current = sample;
  $("now-name").textContent = pitchedLabel(sample);
  $("now-path").textContent = sample ? sample.path : "";
  $("play").disabled = !sample;
  for (const row of $("hits").querySelectorAll(".hit")) {
    const on = sample && row.title === sample.path;
    row.classList.toggle("on", on);
    const button = row.querySelector(".hit-play");
    if (button) button.textContent = on && !audio.paused ? "■" : "▶";
  }
}

async function play(sample) {
  const token = ++loadToken;
  showCurrent(sample);
  warm(sample);
  $("play").textContent = "Play";
  const response = await fetch(`/api/samples/${sample.id}/preview${matchQuery(sample)}`);
  if (token !== loadToken) return;
  if (!response.ok) {
    $("now-path").textContent = "Can't preview this file. Drag still drops the original.";
    audio.removeAttribute("src");
    peaks = null;
    drawWave();
    placePlayhead();
    return;
  }
  const type = response.headers.get("content-type") || "audio/wav";
  const bytes = await response.arrayBuffer();
  if (token !== loadToken) return;
  if (blobUrl) URL.revokeObjectURL(blobUrl);
  blobUrl = URL.createObjectURL(new Blob([bytes], { type }));
  audio.src = blobUrl;
  applyPitch(sample);
  const ctx = new AudioContext();
  try {
    const decoded = await ctx.decodeAudioData(bytes.slice(0));
    peaks = peaksFrom(decoded, Math.max(80, Math.floor(wave.clientWidth || 600)));
  } catch (_) {
    peaks = null;
  } finally {
    await ctx.close();
  }
  drawWave();
  placePlayhead();
  await audio.play();
  $("play").textContent = "Stop";
  $("play").classList.add("on");
  showCurrent(sample);
}

function stop() {
  audio.pause();
  stopWave();
  $("play").textContent = "Play";
  $("play").classList.remove("on");
  showCurrent(current);
}

function scrollToSample(id) {
  const row = document.querySelector(`.hit[data-id="${id}"]`);
  if (row) row.scrollIntoView({ block: "nearest" });
}

async function playNext() {
  if (!pageItems.length) return;
  const index = current ? pageItems.findIndex((sample) => sample.id === current.id) : -1;
  if (index >= 0 && index < pageItems.length - 1) {
    const next = pageItems[index + 1];
    scrollToSample(next.id);
    await play(next);
    return;
  }
  if (index === pageItems.length - 1 && offset + PAGE < total) {
    offset += PAGE;
    await refreshList();
    if (!pageItems.length) return;
    scrollToSample(pageItems[0].id);
    await play(pageItems[0]);
    return;
  }
  if (index === -1) {
    scrollToSample(pageItems[0].id);
    await play(pageItems[0]);
  }
}

async function playPrevious() {
  if (!pageItems.length || !current) return;
  const index = pageItems.findIndex((sample) => sample.id === current.id);
  if (index > 0) {
    const previous = pageItems[index - 1];
    scrollToSample(previous.id);
    await play(previous);
    return;
  }
  if (index === 0 && offset > 0) {
    offset = Math.max(0, offset - PAGE);
    await refreshList();
    if (!pageItems.length) return;
    const previous = pageItems[pageItems.length - 1];
    scrollToSample(previous.id);
    await play(previous);
  }
}

function toggle(sample) {
  if (!sample) return;
  if (current && current.id === sample.id && !audio.paused) {
    stop();
    return;
  }
  play(sample).catch((err) => {
    $("now-path").textContent = String(err);
  });
}

function peaksFrom(buffer, bars) {
  const left = buffer.getChannelData(0);
  const right = buffer.numberOfChannels > 1 ? buffer.getChannelData(1) : null;
  const out = new Float32Array(bars);
  const step = Math.max(1, Math.floor(left.length / bars));
  const stride = step > 400 ? 4 : 1;
  for (let i = 0; i < bars; i++) {
    let peak = 0;
    const start = i * step;
    const end = Math.min(left.length, start + step);
    for (let j = start; j < end; j += stride) {
      const value = Math.max(Math.abs(left[j]), right ? Math.abs(right[j]) : 0);
      if (value > peak) peak = value;
    }
    out[i] = peak;
  }
  return out;
}

function drawWave() {
  const ctx = wave.getContext("2d");
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const width = wave.clientWidth || 600;
  const height = wave.clientHeight || 72;
  const bitmapW = Math.floor(width * dpr);
  const bitmapH = Math.floor(height * dpr);
  if (wave.width !== bitmapW || wave.height !== bitmapH) {
    wave.width = bitmapW;
    wave.height = bitmapH;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const styles = getComputedStyle(document.documentElement);
  ctx.fillStyle = styles.getPropertyValue("--bg-muted").trim() || "#12141a";
  ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = styles.getPropertyValue("--accent").trim() || "#6ee7b7";
  const data = peaks;
  if (data && data.length) {
    const barWidth = width / data.length;
    for (let i = 0; i < data.length; i++) {
      const h = Math.max(1, data[i] * (height - 8));
      ctx.globalAlpha = 0.9;
      ctx.fillRect(i * barWidth, (height - h) / 2, Math.max(1, barWidth - 0.5), h);
    }
  }
  ctx.globalAlpha = 1;
}

let waveRaf = 0;

function placePlayhead() {
  const head = $("playhead");
  const ratio = audio.duration ? Math.min(1, Math.max(0, audio.currentTime / audio.duration)) : 0;
  const playing = !audio.paused && !audio.ended;
  if (!playing && ratio <= 0) {
    head.hidden = true;
    return;
  }
  head.hidden = false;
  const x = Math.round(ratio * (wave.clientWidth || 0));
  head.style.transform = `translate3d(${x}px,0,0)`;
  if (audio.duration) {
    const label = `${fmtClock(audio.currentTime)} / ${fmtClock(audio.duration)}`;
    if ($("now-time").textContent !== label) $("now-time").textContent = label;
  }
}

function waveFrame() {
  placePlayhead();
  if (!audio.paused && !audio.ended) waveRaf = requestAnimationFrame(waveFrame);
  else waveRaf = 0;
}

function startWave() {
  if (waveRaf) return;
  waveRaf = requestAnimationFrame(waveFrame);
}

function stopWave() {
  if (waveRaf) cancelAnimationFrame(waveRaf);
  waveRaf = 0;
  placePlayhead();
}

function scheduleList() {
  clearTimeout(scheduleList.timer);
  scheduleList.timer = setTimeout(() => {
    offset = 0;
    refreshList();
  }, 180);
}

function poll() {
  const timer = setInterval(async () => {
    await refreshStatus();
    if (!scanning) {
      clearInterval(timer);
      await refreshList();
    }
  }, 700);
}

function bind() {
  try {
    $("hide-dupes").checked = localStorage.getItem("hoarder-hide-dupes") !== "0";
  } catch (_) {}

  $("theme").value = document.documentElement.dataset.theme || "dark";
  $("theme").addEventListener("change", () => {
    document.documentElement.dataset.theme = $("theme").value;
    localStorage.setItem("hoarder-theme", $("theme").value);
    drawWave();
  });

  paintLength();
  for (const id of ["len-min", "len-max"]) {
    const which = id === "len-min" ? "min" : "max";
    $(id).addEventListener("pointerdown", () => {
      $("len-min").style.zIndex = which === "min" ? "2" : "1";
      $("len-max").style.zIndex = which === "max" ? "2" : "1";
    });
    $(id).addEventListener("input", () => onLengthInput(which));
  }

  renderRoles();
  renderGenres();
  $("q").addEventListener("input", () => scheduleList());

  $("target-key").addEventListener("change", () => {
    if (!current) return;
    $("now-name").textContent = pitchedLabel(current);
    if (!audio.paused) play(current);
  });

  const settings = $("settings");
  const menu = $("settings-menu");
  settings.addEventListener("click", (event) => {
    event.stopPropagation();
    const open = menu.hidden;
    menu.hidden = !open;
    settings.setAttribute("aria-expanded", open ? "true" : "false");
  });
  menu.addEventListener("click", (event) => event.stopPropagation());
  document.addEventListener("click", () => {
    menu.hidden = true;
    settings.setAttribute("aria-expanded", "false");
    closeFileMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeFileMenu();
  });
  $("hits").addEventListener("scroll", () => closeFileMenu());
  $("open-explorer").addEventListener("click", (event) => {
    event.stopPropagation();
    const sample = menuSample;
    closeFileMenu();
    if (sample) reveal(sample);
  });
  $("hide-dupes").addEventListener("change", () => {
    try {
      localStorage.setItem("hoarder-hide-dupes", $("hide-dupes").checked ? "1" : "0");
    } catch (_) {}
    offset = 0;
    refreshList();
  });

  $("prev").addEventListener("click", () => {
    offset = Math.max(0, offset - PAGE);
    refreshList();
  });
  $("next").addEventListener("click", () => {
    if (offset + PAGE < total) {
      offset += PAGE;
      refreshList();
    }
  });
  $("rescan").addEventListener("click", async () => {
    const response = await fetch("/api/rescan", { method: "POST" });
    if (response.ok || response.status === 409) {
      scanning = true;
      $("rescan").disabled = true;
      poll();
    }
  });
  $("play").addEventListener("click", () => {
    if (!current) return;
    if (audio.paused) play(current);
    else stop();
  });

  audio.addEventListener("loadedmetadata", () => applyPitch(current));
  audio.addEventListener("playing", () => {
    applyPitch(current);
    startWave();
  });
  audio.addEventListener("pause", () => stopWave());
  audio.addEventListener("ended", () => stop());
  wave.addEventListener("click", (event) => {
    if (!audio.duration) return;
    const rect = wave.getBoundingClientRect();
    audio.currentTime = ((event.clientX - rect.left) / rect.width) * audio.duration;
    placePlayhead();
  });
  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(() => {
      drawWave();
      placePlayhead();
    }).observe(wave.parentElement);
  }

  document.addEventListener("keydown", (event) => {
    const tag = event.target && event.target.tagName;
    const typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    if (!typing && event.key === "/") {
      event.preventDefault();
      $("q").focus();
      return;
    }
    if (typing) return;
    if (event.code === "Space") {
      event.preventDefault();
      if (!current || event.repeat) return;
      if (audio.paused) play(current);
      else stop();
    }
    if (event.code === "ArrowDown" || event.code === "ArrowUp") {
      event.preventDefault();
      if (event.repeat) return;
      const move = event.code === "ArrowDown" ? playNext : playPrevious;
      move().catch((err) => {
        $("now-path").textContent = String(err);
      });
    }
  });
}

bind();
refreshProject();
setInterval(refreshProject, 2000);
refreshStatus()
  .then(() => refreshList())
  .then(() => {
    if (scanning) poll();
  });
drawWave();
