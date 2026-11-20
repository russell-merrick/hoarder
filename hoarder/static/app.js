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
let sortBy = "name";
let sortDir = "asc";
const LEN_MAX = 1000;
const SEC_HI = 120;
const ROOTS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const BPM_MIN = 40;
const BPM_MAX = 250;
const FLAT = { Db: "C#", Eb: "D#", Gb: "F#", Ab: "G#", Bb: "A#" };

const ROLE_GROUPS = [
  [
    ["kick", "Kick"],
    ["hat", "Hat"],
    ["snare", "Snare"],
    ["clap", "Clap"],
    ["shaker", "Shaker"],
    ["tom", "Tom"],
    ["cymbal", "Cymbal"],
    ["perc", "Perc"],
  ],
  [
    ["bass", "Bass"],
    ["808", "808"],
    ["lead", "Lead"],
    ["pluck", "Pluck"],
    ["pad", "Pad"],
    ["stab", "Stab"],
    ["arp", "Arp"],
    ["synth", "Synth"],
    ["keys", "Keys"],
    ["guitar", "Guitar"],
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
const selectedGenres = new Set();

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

function isMinor(key) {
  return /m$/.test(key || "");
}

function semitonesBetween(from, to) {
  let start = ROOTS.indexOf(rootName(from));
  const end = ROOTS.indexOf(rootName(to));
  if (start < 0 || end < 0) return 0;
  // Relative keys share their notes: an A minor loop in C major does not move.
  // A key with no mode counts as major.
  if (isMinor(from) && !isMinor(to)) start += 3;
  else if (isMinor(to) && !isMinor(from)) start -= 3;
  let delta = (end - start) % 12;
  if (delta < 0) delta += 12;
  if (delta > 6) delta -= 12;
  return delta;
}

function targetKey() {
  return $("target-key").value;
}

function fillKeys(select) {
  for (const [label, suffix] of [["Major", ""], ["Minor", "m"]]) {
    const group = document.createElement("optgroup");
    group.label = label;
    for (const root of ROOTS) {
      const option = document.createElement("option");
      option.value = `${root}${suffix}`;
      option.textContent = `${root}${suffix}`;
      group.append(option);
    }
    select.append(group);
  }
}

function shiftOf(sample) {
  if (!sample || !sample.key || !targetKey()) return 0;
  return semitonesBetween(sample.key, targetKey());
}

let project = { running: false, bpm: null, name: "" };
let link = { connected: false, armed: false, bpm: null, host: "" };
let linkedOn = false;
let linkLoop = false;
let linkClip = { loop: false, beats: 0, bpm: 0, repeat: "" };
let linkFreeAt = 0;
let linkCoast = null;
let linkedAudible = false;
let sawTransport = false;
let pausedByHost = false;
let linkWatching = false;
let hostFix = { ppq: 0, bpm: 0, playing: false, meter: [4, 4], at: 0, age: 0 };

function isPlaying() {
  if (linkedOn) return linkedAudible;
  return !audio.paused;
}

function meterNum() {
  const meter = link.meter || hostFix.meter;
  const num = Array.isArray(meter) ? Number(meter[0]) : 4;
  return num >= 1 && num <= 32 ? num : 4;
}

function renderBody(sample) {
  return {
    semitones: shiftOf(sample) || 0,
    bpm: project.bpm,
    stretch: 1,
    repeat: $("repeat").value || null,
    meter: meterNum(),
  };
}

function paintLink() {
  const button = $("link");
  const on = Boolean(link.connected);
  button.classList.toggle("on", on);
  button.disabled = !on;
  button.title = on ? "Plugin connected" : "No plugin connected";
}

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

function roleChip(id) {
  return document.querySelector(`#roles .chip[data-id="${CSS.escape(id)}"]`);
}

function paintRole(id) {
  const button = roleChip(id);
  if (!button) return;
  const on = selectedRoles.has(id);
  button.classList.toggle("on", on);
  button.setAttribute("aria-pressed", on ? "true" : "false");
}

function toggleRole(id) {
  if (selectedRoles.has(id)) selectedRoles.delete(id);
  else selectedRoles.add(id);
  paintRole(id);
  offset = 0;
  refreshList();
}

function toggleGenreTag(id) {
  if (selectedGenres.has(id)) selectedGenres.delete(id);
  else selectedGenres.add(id);
  paintGenreChecks();
  paintGenreButton();
  offset = 0;
  refreshList();
}

function filterTag(id, label, kind) {
  const tag = document.createElement("button");
  tag.type = "button";
  tag.className = kind === "genre" ? "tag genre" : "tag";
  tag.textContent = label;
  const on = kind === "genre" ? selectedGenres.has(id) : selectedRoles.has(id);
  tag.classList.toggle("on", on);
  tag.addEventListener("click", (event) => {
    event.stopPropagation();
    if (kind === "genre") toggleGenreTag(id);
    else toggleRole(id);
  });
  return tag;
}

function fillMeta(meta, sample) {
  meta.replaceChildren();
  const tags = document.createElement("span");
  tags.className = "meta-tags";
  paintTags(tags, sample);
  const add = document.createElement("button");
  add.type = "button";
  add.className = "tag-add";
  add.textContent = "+";
  add.setAttribute("aria-label", "Edit tags");
  add.addEventListener("click", (event) => {
    event.stopPropagation();
    openTagEditor(sample, add);
  });
  const path = document.createElement("span");
  path.className = "meta-path";
  path.textContent = folderText(sample);
  meta.append(tags, add, path);
}

function paintTags(tags, sample) {
  tags.replaceChildren();
  for (const id of Array.isArray(sample.roles) ? sample.roles : []) {
    if (ROLE_LABEL[id]) tags.append(filterTag(id, ROLE_LABEL[id], "role"));
  }
  for (const id of Array.isArray(sample.genres) ? sample.genres : []) {
    if (GENRE_LABEL[id]) tags.append(filterTag(id, GENRE_LABEL[id], "genre"));
  }
}

function paintRowTags(sample) {
  const li = document.querySelector(`.hit[data-id="${CSS.escape(String(sample.id))}"]`);
  const tags = li && li.querySelector(".meta-tags");
  if (tags) paintTags(tags, sample);
}

let tagSample = null;
let tagWrite = Promise.resolve();
let tagGen = 0;

const picks = [];

function closePicks() {
  for (const pick of picks) {
    pick.panel.hidden = true;
    pick.button.setAttribute("aria-expanded", "false");
  }
}

function mountPick(select) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn ghost pick-btn";
  button.id = `${select.id}-btn`;
  button.setAttribute("aria-haspopup", "listbox");
  button.setAttribute("aria-expanded", "false");
  const panel = document.createElement("div");
  panel.className = "menu-panel pick-panel";
  panel.id = `${select.id}-panel`;
  panel.hidden = true;
  panel.setAttribute("role", "listbox");
  select.classList.add("pick-native");
  select.hidden = true;
  const wrap = document.createElement("span");
  wrap.className = "pick";
  select.before(wrap);
  wrap.append(button, panel, select);
  const insideSettings = Boolean(select.closest("#settings-menu"));

  function paint() {
    const chosen = select.selectedOptions[0];
    button.textContent = chosen ? chosen.textContent : "";
    panel.replaceChildren();
    const addOption = (option) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "pick-item";
      row.textContent = option.textContent;
      row.setAttribute("role", "option");
      if (option.selected) row.classList.add("on");
      row.addEventListener("click", (event) => {
        event.stopPropagation();
        select.value = option.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        panel.hidden = true;
        button.setAttribute("aria-expanded", "false");
        paint();
      });
      panel.append(row);
    };
    for (const child of select.children) {
      if (child.tagName === "OPTGROUP") {
        const head = document.createElement("div");
        head.className = "pick-group";
        head.textContent = child.label;
        panel.append(head);
        for (const option of child.children) addOption(option);
      } else if (child.tagName === "OPTION") {
        addOption(child);
      }
    }
  }

  button.addEventListener("click", (event) => {
    event.stopPropagation();
    const willOpen = panel.hidden;
    closePicks();
    if (!insideSettings) {
      for (const id of ["settings-menu", "help-menu", "genre-panel"]) {
        const other = $(id);
        if (other) other.hidden = true;
      }
      for (const id of ["settings", "help", "genre-btn"]) {
        const other = $(id);
        if (other) other.setAttribute("aria-expanded", "false");
      }
      closeFileMenu();
      closeTagEditor();
    }
    if (!willOpen) return;
    panel.hidden = false;
    button.setAttribute("aria-expanded", "true");
    panel.style.top = "";
    panel.style.bottom = "";
    const rect = panel.getBoundingClientRect();
    if (rect.bottom > window.innerHeight - 8) {
      panel.style.top = "auto";
      panel.style.bottom = "calc(100% + 6px)";
    }
  });
  panel.addEventListener("click", (event) => event.stopPropagation());
  picks.push({ button, panel });
  paint();
}

function hideMenus() {
  for (const id of ["settings-menu", "help-menu", "genre-panel", "file-menu"]) {
    const panel = $(id);
    if (panel) panel.hidden = true;
  }
  for (const id of ["settings", "help", "genre-btn"]) {
    const button = $(id);
    if (button) button.setAttribute("aria-expanded", "false");
  }
  closePicks();
  menuSample = null;
}

function openTagEditor(sample, anchor) {
  const editor = $("tag-editor");
  const same = tagSample && tagSample.id === sample.id && !editor.hidden;
  hideMenus();
  if (same) {
    closeTagEditor();
    return;
  }
  tagSample = sample;
  for (const input of editor.querySelectorAll("input")) {
    const list = input.dataset.kind === "genre" ? sample.genres : sample.roles;
    input.checked = Array.isArray(list) && list.includes(input.value);
  }
  editor.hidden = false;
  anchor.classList.add("on");
  const rect = anchor.getBoundingClientRect();
  editor.style.left = `${Math.max(8, rect.left)}px`;
  editor.style.top = `${rect.bottom + 6}px`;
  const box = editor.getBoundingClientRect();
  if (box.right > window.innerWidth - 8) {
    editor.style.left = `${Math.max(8, window.innerWidth - box.width - 8)}px`;
  }
  if (box.bottom > window.innerHeight - 8) {
    editor.style.top = `${Math.max(8, rect.top - box.height - 6)}px`;
  }
}

function closeTagEditor() {
  tagSample = null;
  $("tag-editor").hidden = true;
  for (const button of document.querySelectorAll(".tag-add.on")) button.classList.remove("on");
}

function stillMatches(sample) {
  for (const id of selectedRoles) {
    if (!sample.roles.includes(id)) return false;
  }
  for (const id of selectedGenres) {
    if (!sample.genres.includes(id)) return false;
  }
  return true;
}

function onTagChange() {
  const sample = tagSample;
  if (!sample) return;
  const gen = ++tagGen;
  const roles = [...$("tag-editor").querySelectorAll('input[data-kind="role"]:checked')].map((input) => input.value);
  const genres = [...$("tag-editor").querySelectorAll('input[data-kind="genre"]:checked')].map((input) => input.value);
  sample.roles = roles;
  sample.genres = genres;
  paintRowTags(sample);
  const id = sample.id;
  tagWrite = tagWrite.then(async () => {
    const response = await fetch(`/api/samples/${id}/tags`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ roles, genres }),
    });
    if (gen !== tagGen) return;
    if (!response.ok) {
      closeTagEditor();
      refreshList();
      return;
    }
    const body = await response.json();
    if (gen !== tagGen) return;
    sample.roles = body.roles;
    sample.genres = body.genres;
    const li = document.querySelector(`.hit[data-id="${CSS.escape(String(id))}"]`);
    if (li) li.dataset.savedTags = `${body.roles.join(",")}|${body.genres.join(",")}`;
    if (tagSample && tagSample.id === id) {
      paintRowTags(sample);
      if (!stillMatches(sample)) {
        closeTagEditor();
        offset = 0;
        refreshList();
      }
    }
  }).catch(() => {
    closeTagEditor();
    refreshList();
  });
}

function tagCheck(id, label, kind) {
  const row = document.createElement("label");
  row.className = kind === "genre" ? "check genre" : "check role";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.value = id;
  input.dataset.kind = kind;
  input.addEventListener("change", () => onTagChange());
  row.append(input, document.createTextNode(label));
  return row;
}

function fillTagEditor() {
  const roles = $("tag-editor-roles");
  const genres = $("tag-editor-genres");
  roles.replaceChildren();
  genres.replaceChildren();
  for (const [id, label] of ROLE_GROUPS.flat()) roles.append(tagCheck(id, label, "role"));
  for (const [id, label] of GENRE_GROUPS.flat()) genres.append(tagCheck(id, label, "genre"));
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
    .filter((id) => selectedGenres.has(id));
  if (genres.length) query.set("genres", genres.join(","));
  const bounds = lengthBounds();
  if (bounds.min != null) query.set("min_sec", String(bounds.min));
  if (bounds.max != null) query.set("max_sec", String(bounds.max));
  const tempo = tempoBounds();
  if (tempo.min != null) query.set("min_bpm", String(tempo.min));
  if (tempo.max != null) query.set("max_bpm", String(tempo.max));
  if ($("key-filter").value) query.set("key", $("key-filter").value);
  query.set("sort", sortBy);
  query.set("direction", sortDir);
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

// The last 5% of the bar is the open end: no minimum, or no maximum.
const LEN_SNAP = 0.05;

function snapLength(pos) {
  if (pos <= LEN_MAX * LEN_SNAP) return 0;
  if (pos >= LEN_MAX * (1 - LEN_SNAP)) return LEN_MAX;
  return pos;
}

function onLengthInput(which) {
  const minEl = $("len-min");
  const maxEl = $("len-max");
  let min = Number(minEl.value);
  let max = Number(maxEl.value);
  if (which === "min") min = snapLength(min);
  else max = snapLength(max);
  minEl.value = String(min);
  maxEl.value = String(max);
  if (max - min < 1) {
    if (which === "min") minEl.value = String(Math.max(0, max - 1));
    else maxEl.value = String(Math.min(LEN_MAX, min + 1));
  }
  paintLength();
  scheduleList();
}

function lengthPos(event, range) {
  const rect = range.getBoundingClientRect();
  const ratio = (event.clientX - rect.left - 6) / Math.max(1, rect.width - 12);
  return snapLength(Math.round(Math.min(LEN_MAX, Math.max(0, ratio * LEN_MAX))));
}

function lengthTarget(pos) {
  const min = Number($("len-min").value);
  const max = Number($("len-max").value);
  if (pos <= min) return "min";
  if (pos >= max) return "max";
  return pos - min <= max - pos ? "min" : "max";
}

function setLength(which, pos) {
  $(which === "min" ? "len-min" : "len-max").value = String(pos);
  $("len-min").style.zIndex = which === "min" ? "2" : "1";
  $("len-max").style.zIndex = which === "max" ? "2" : "1";
  onLengthInput(which);
}

const BPM_SPAN = BPM_MAX - BPM_MIN;

function tempoBounds() {
  const min = Number($("bpm-min").value);
  const max = Number($("bpm-max").value);
  return {
    min: min <= BPM_MIN && max > BPM_MIN ? null : min,
    max: max >= BPM_MAX && min < BPM_MAX ? null : max,
  };
}

function paintTempo() {
  const min = Number($("bpm-min").value);
  const max = Number($("bpm-max").value);
  const fill = $("tempo-fill");
  const span = Math.max(0, max - min) / BPM_SPAN;
  fill.style.left = `calc(6px + (100% - 12px) * ${(min - BPM_MIN) / BPM_SPAN})`;
  fill.style.width = `calc((100% - 12px) * ${span})`;
  const bounds = tempoBounds();
  const label = $("tempo-label");
  if (bounds.min == null && bounds.max == null) label.textContent = "Any tempo";
  else if (bounds.min == null) label.textContent = `≤ ${bounds.max}`;
  else if (bounds.max == null) label.textContent = `≥ ${bounds.min}`;
  else if (bounds.min === bounds.max) label.textContent = `${bounds.min} BPM`;
  else label.textContent = `${bounds.min} – ${bounds.max}`;
  const exact = $("tempo-exact");
  if (document.activeElement !== exact) {
    exact.value = bounds.min != null && bounds.min === bounds.max ? String(bounds.min) : "";
  }
}

function applyExactTempo() {
  const exact = $("tempo-exact");
  const raw = exact.value.trim();
  if (!raw) {
    const bounds = tempoBounds();
    if (bounds.min != null && bounds.min === bounds.max) {
      $("bpm-min").value = String(BPM_MIN);
      $("bpm-max").value = String(BPM_MAX);
      paintTempo();
      scheduleList();
    }
    return;
  }
  const value = Math.round(Number(raw));
  if (!Number.isFinite(value)) return;
  const bpm = Math.min(BPM_MAX, Math.max(BPM_MIN, value));
  $("bpm-min").value = String(bpm);
  $("bpm-max").value = String(bpm);
  exact.value = String(bpm);
  paintTempo();
  scheduleList();
}

function onTempoInput(which) {
  const minEl = $("bpm-min");
  const maxEl = $("bpm-max");
  const min = Number(minEl.value);
  const max = Number(maxEl.value);
  if (min > max) {
    if (which === "min") maxEl.value = String(min);
    else minEl.value = String(max);
  }
  paintTempo();
  scheduleList();
}

function tempoPos(event, range) {
  const rect = range.getBoundingClientRect();
  const ratio = (event.clientX - rect.left - 6) / Math.max(1, rect.width - 12);
  return Math.round(Math.min(BPM_MAX, Math.max(BPM_MIN, BPM_MIN + ratio * BPM_SPAN)));
}

function tempoTarget(pos) {
  const min = Number($("bpm-min").value);
  const max = Number($("bpm-max").value);
  if (pos <= min) return "min";
  if (pos >= max) return "max";
  return pos - min <= max - pos ? "min" : "max";
}

function setTempo(which, pos) {
  $(which === "min" ? "bpm-min" : "bpm-max").value = String(pos);
  $("bpm-min").style.zIndex = which === "min" ? "2" : "1";
  $("bpm-max").style.zIndex = which === "max" ? "2" : "1";
  onTempoInput(which);
}

function bindRange(id, posOf, targetOf, apply) {
  const range = $(id);
  range.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target.closest("input")) return;
    const which = targetOf(posOf(event, range));
    const move = (ev) => apply(which, posOf(ev, range));
    move(event);
    range.setPointerCapture(event.pointerId);
    const end = () => {
      range.removeEventListener("pointermove", move);
      range.removeEventListener("pointerup", end);
      range.removeEventListener("pointercancel", end);
    };
    range.addEventListener("pointermove", move);
    range.addEventListener("pointerup", end);
    range.addEventListener("pointercancel", end);
  });
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
      button.dataset.id = id;
      button.textContent = label;
      button.setAttribute("aria-pressed", "false");
      button.addEventListener("click", () => toggleRole(id));
      wrap.append(button);
    }
    box.append(wrap);
  }
}

function paintGenreButton() {
  const button = $("genre-btn");
  const picked = GENRE_GROUPS.flat()
    .map(([id]) => id)
    .filter((id) => selectedGenres.has(id));
  button.classList.toggle("on", picked.length > 0);
  if (picked.length === 0) button.textContent = "Any";
  else if (picked.length === 1) button.textContent = GENRE_LABEL[picked[0]] || picked[0];
  else button.textContent = `${picked.length} selected`;
}

function paintGenreChecks() {
  for (const input of document.querySelectorAll("#genre-panel input")) {
    input.checked = selectedGenres.has(input.value);
  }
}

function renderGenres() {
  const box = $("genre-panel");
  box.replaceChildren();
  for (const [id, label] of GENRE_GROUPS.flat()) {
    const row = document.createElement("label");
    row.className = "check";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = id;
    input.checked = selectedGenres.has(id);
    input.addEventListener("change", () => {
      if (input.checked) selectedGenres.add(id);
      else selectedGenres.delete(id);
      paintGenreButton();
      offset = 0;
      refreshList();
    });
    row.append(input, document.createTextNode(label));
    box.append(row);
  }
}

async function refreshProject() {
  try {
    const response = await fetch("/api/project");
    if (!response.ok) return;
    const body = await response.json();
    if (body.link) noteHost(body.link);
    if (link.connected && !link.armed) {
      fetch("/api/link/arm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ on: true }),
      }).catch(() => {});
    }
    paintLink();
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
    if (next !== prev && current && isPlaying()) play(current);
  } catch (_) {}
}

async function refreshStatus() {
  const response = await fetch("/api/status");
  if (!response.ok) return;
  const body = await response.json();
  scanning = Boolean(body.scanning);
  const total = ($("hide-dupes").checked ? body.active : body.samples) || 0;
  $("counts").textContent = `${total.toLocaleString()} ${total === 1 ? "sample" : "samples"}`;
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

function paintSortArrows() {
  for (const button of document.querySelectorAll("#hit-head .col-sort")) {
    const on = button.dataset.sort === sortBy;
    const dir = on ? sortDir : "asc";
    button.classList.toggle("on", on);
    button.classList.toggle("up", dir === "asc");
    button.classList.toggle("down", dir !== "asc");
    button.setAttribute("aria-pressed", on ? "true" : "false");
  }
}

function applySort(sort, direction) {
  if (sortBy === sort && sortDir === direction) return;
  sortBy = sort;
  sortDir = direction;
  offset = 0;
  paintSortArrows();
  refreshList();
}

function drawList(body) {
  closeTagEditor();
  pageItems = body.items;
  const hits = $("hits");
  const pager = $("pager");
  hits.replaceChildren($("hit-head"));
  if (!body.items.length) {
    const empty = document.createElement("li");
    empty.className = "hit-empty";
    empty.textContent = scanning ? "Scanning the library…" : "No samples match these filters.";
    hits.appendChild(empty);
  }
  for (const sample of body.items) {
    hits.appendChild(renderHit(sample));
  }
  hits.appendChild(pager);
  hits.scrollTop = 0;
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
  li.draggable = !nativeDrag;
  li.dataset.id = String(sample.id);
  li.title = sample.path || "";

  const play = document.createElement("button");
  play.type = "button";
  play.className = "hit-play";
  play.textContent = current && current.id === sample.id && isPlaying() ? "■" : "▶";
  play.addEventListener("click", (event) => {
    event.stopPropagation();
    toggle(sample);
  });

  const body = document.createElement("div");
  body.className = "hit-body";
  const name = document.createElement("div");
  name.className = "name";
  name.textContent = sample.name;
  const meta = document.createElement("div");
  meta.className = "meta";
  fillMeta(meta, sample);
  body.append(name, meta);

  const bpm = document.createElement("span");
  bpm.className = sample.bpm ? "key" : "key empty";
  bpm.textContent = sample.bpm ? String(Math.round(sample.bpm)) : "—";

  const key = document.createElement("span");
  key.className = sample.key ? "key" : "key empty";
  key.textContent = sample.key || "—";

  const dur = document.createElement("span");
  dur.className = "dur";
  dur.textContent = fmtDur(sample.duration);

  li.append(play, body, bpm, key, dur);
  li.addEventListener("click", () => {
    if (li.dataset.dragged) {
      delete li.dataset.dragged;
      return;
    }
    toggle(sample);
  });
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

function copyModified(sample) {
  fetch(`/api/samples/${sample.id}/copy`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(renderBody(sample)),
  }).then((response) => {
    $("now-path").textContent = response.ok
      ? "Copied the modified sample."
      : "Couldn't copy the modified sample.";
  }).catch(() => {
    $("now-path").textContent = "Couldn't copy the modified sample.";
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
  if (link.connected) {
    return {
      name: safeName(sample.name),
      url: `${location.origin}/api/samples/${sample.id}/file`,
    };
  }
  const query = matchQuery(sample);
  if (query) {
    const stem = safeName(sample.name).replace(/\.[^.]+$/i, "");
    const bits = [stem];
    if (shiftOf(sample) && targetKey()) bits.push(targetKey());
    if (project.bpm && sample.bpm) bits.push(`${Math.round(project.bpm)}bpm`);
    return {
      name: safeName(`${bits.join(" ")}.wav`),
      url: `${location.origin}/api/samples/${sample.id}/file${query}`,
    };
  }
  return {
    name: safeName(sample.name),
    url: `${location.origin}/api/samples/${sample.id}/file`,
  };
}

const nativeDrag = /Windows/.test(navigator.userAgent);

function bindDrag(el, getSample) {
  let start = null;
  el.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || !nativeDrag) return;
    if (event.target.closest("button")) return;
    const sample = getSample();
    if (!sample) return;
    start = { x: event.clientX, y: event.clientY, id: sample.id, pointer: event.pointerId, sent: false };
  });
  el.addEventListener("pointermove", (event) => {
    if (!start || start.sent || start.pointer !== event.pointerId) return;
    const dx = event.clientX - start.x;
    const dy = event.clientY - start.y;
    if (dx * dx + dy * dy < 16) return;
    start.sent = true;
    el.dataset.dragged = "1";
    if (el.hasPointerCapture(event.pointerId)) el.releasePointerCapture(event.pointerId);
    const sample = getSample();
    if (!sample) return;
    $("now-path").textContent = "Dragging the sample…";
    fetch(`/api/samples/${sample.id}/drag`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(renderBody(sample)),
    }).then((response) => response.json().then((body) => ({ ok: response.ok, body }))).then((result) => {
      if (!result.ok) {
        $("now-path").textContent = "Couldn't drag the sample.";
        return;
      }
      $("now-path").textContent = result.body.effect ? "Dropped the sample." : "Drag cancelled.";
    }).catch(() => {
      $("now-path").textContent = "Couldn't drag the sample.";
    });
  });
  const end = () => {
    start = null;
  };
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
  el.addEventListener("dragstart", (event) => {
    if (nativeDrag) {
      event.preventDefault();
      return;
    }
    const sample = getSample();
    if (!sample) {
      event.preventDefault();
      return;
    }
    const drop = dragFile(sample);
    const transfer = event.dataTransfer;
    transfer.clearData();
    transfer.effectAllowed = "copy";
    // Live rejects an in-memory file and a web link. DownloadURL is the file drop it accepts.
    transfer.setData("DownloadURL", `application/octet-stream:${drop.name}:${drop.url}`);
  });
}

function showCurrent(sample) {
  current = sample;
  $("now-name").textContent = pitchedLabel(sample);
  $("now-path").textContent = sample ? sample.path : "";
  $("play").disabled = !sample;
  $("play").textContent = isPlaying() ? "Stop" : "Play";
  $("play").classList.toggle("on", isPlaying());
  for (const row of $("hits").querySelectorAll(".hit")) {
    const on = sample && row.title === sample.path;
    row.classList.toggle("on", on);
    const button = row.querySelector(".hit-play");
    if (button) button.textContent = on && isPlaying() ? "■" : "▶";
  }
}

async function showWaveform(sample, token) {
  const response = await fetch(`/api/samples/${sample.id}/preview${matchQuery(sample)}`);
  if (token !== loadToken) return false;
  if (!response.ok) {
    $("now-path").textContent = "Can't preview this file. Drag still drops the original.";
    audio.removeAttribute("src");
    peaks = null;
    drawWave();
    placePlayhead();
    return false;
  }
  const type = response.headers.get("content-type") || "audio/wav";
  const bytes = await response.arrayBuffer();
  if (token !== loadToken) return false;
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
  return true;
}

async function play(sample) {
  const token = ++loadToken;
  const linked = Boolean(link.connected);
  pausedByHost = false;
  linkedOn = linked;
  if (linked) linkedAudible = true;
  if (linked) {
    audio.pause();
    stopWave();
    watchLink();
  }
  showCurrent(sample);
  warm(sample);
  const sent = linked ? playLinked(sample, token) : Promise.resolve(true);
  const drawn = showWaveform(sample, token);
  const ok = await sent;
  if (token !== loadToken) return;
  if (!ok) {
    linkedOn = false;
    linkedAudible = false;
    showCurrent(sample);
    $("now-path").textContent = "Plugin link didn't take the preview.";
    return;
  }
  const hasWave = await drawn;
  if (token !== loadToken) return;
  if (!hasWave && !linked) return;
  audio.muted = linked;
  audio.loop = false;
  if (linked) {
    armLinkClock();
    startWave();
  } else {
    try {
      await audio.play();
    } catch (_) {
      return;
    }
  }
  showCurrent(sample);
}

async function playLinked(sample, token) {
  const response = await fetch("/api/link/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: sample.id,
      semitones: shiftOf(sample) || 0,
      bpm: project.bpm,
      stretch: 1,
      repeat: $("repeat").value || null,
    }),
  });
  if (token !== loadToken) return false;
  if (!response.ok) return false;
  const body = await response.json();
  linkLoop = Boolean(body.loop);
  linkClip = {
    loop: linkLoop,
    beats: Number(body.beats) || 0,
    bpm: Number(body.bpm) || 0,
    repeat: body.repeat || "",
  };
  return true;
}

function stop() {
  if (linkedOn) fetch("/api/link/stop", { method: "POST" }).catch(() => {});
  linkedOn = false;
  linkedAudible = false;
  pausedByHost = false;
  linkLoop = false;
  linkCoast = null;
  audio.loop = false;
  audio.muted = false;
  audio.pause();
  stopWave();
  $("play").textContent = "Play";
  $("play").classList.remove("on");
  showCurrent(current);
}

function noteHost(snap) {
  const playing = Boolean(snap.playing);
  if (linkedOn && linkedAudible && sawTransport && !playing) {
    linkedAudible = false;
    pausedByHost = true;
    linkCoast = null;
    stopWave();
    showCurrent(current);
  } else if (linkedOn && pausedByHost && !sawTransport && playing) {
    pausedByHost = false;
    armLinkClock();
    startWave();
    showCurrent(current);
  }
  sawTransport = playing;
  hostFix = {
    ppq: Number(snap.ppq) || 0,
    bpm: Number(snap.bpm) || 0,
    playing,
    meter: Array.isArray(snap.meter) ? snap.meter : [4, 4],
    at: performance.now(),
    age: Number(snap.age_ms) || 0,
  };
  link = snap;
  paintLink();
}

function watchLink() {
  if (linkWatching) return;
  linkWatching = true;
  const tick = async () => {
    if (!linkedOn) {
      linkWatching = false;
      return;
    }
    try {
      const response = await fetch("/api/link");
      if (response.ok && linkedOn) noteHost(await response.json());
    } catch (_) {}
    if (linkedOn) setTimeout(tick, 50);
    else linkWatching = false;
  };
  tick();
}

function armLinkClock() {
  linkedAudible = true;
  linkCoast = null;
  linkFreeAt = performance.now();
  sawTransport = hostFix.playing;
}

function wrapPhase(value, length) {
  if (length <= 0) return 0;
  let phase = value % length;
  if (phase < 0) phase += length;
  return phase;
}

function repeatInterval(rate, meterNum) {
  if (rate === "1/16") return 0.25;
  if (rate === "1/8") return 0.5;
  if (rate === "1/4") return 1;
  if (rate === "1/2") return 2;
  const bars = Math.max(1, Number(meterNum) || 4);
  if (rate === "1bar") return bars;
  if (rate === "2bar") return 2 * bars;
  if (rate === "4bar") return 4 * bars;
  return 0;
}

function linkFileSeconds() {
  if (Number.isFinite(audio.duration) && audio.duration > 0) return audio.duration;
  const bpm = linkClip.bpm || hostFix.bpm;
  if (linkClip.beats > 0 && bpm > 0) return linkClip.beats * 60 / bpm;
  return 0;
}

function hostNow(now) {
  const extra = hostFix.playing && hostFix.bpm
    ? ((hostFix.age + (now - hostFix.at)) / 1000) * hostFix.bpm / 60
    : 0;
  return {
    ppq: hostFix.ppq + extra,
    bpm: hostFix.bpm,
    playing: hostFix.playing,
    meter: hostFix.meter,
  };
}

function linkedRatio(now) {
  if (!linkedAudible) return null;
  const host = hostNow(now);
  const seconds = linkFileSeconds();
  const beats = linkClip.beats;
  const bpm = host.bpm;
  const rendered = linkClip.bpm;
  const tempoHolds = rendered <= 0 || bpm <= 0 || Math.abs(bpm - rendered) < 0.51;
  const interval = repeatInterval(linkClip.repeat, host.meter[0]);
  const repeating = interval > 0 && bpm > 0;
  if (host.playing && repeating) {
    const at = wrapPhase(host.ppq, interval) * 60 / bpm;
    if (seconds <= 0 || at >= seconds) return null;
    return at / seconds;
  }
  if (!host.playing && repeating && seconds > 0) {
    const beatsIn = ((now - linkFreeAt) / 1000) * bpm / 60;
    const at = wrapPhase(beatsIn, interval) * 60 / bpm;
    if (at >= seconds) return null;
    return at / seconds;
  }
  const gridLoop = host.playing && linkClip.loop && beats > 0;
  if (gridLoop && !tempoHolds) {
    if (!linkCoast) {
      linkCoast = { ratio: wrapPhase(host.ppq, beats) / beats, at: now };
    }
    if (seconds <= 0) return linkCoast.ratio;
    return wrapPhase(linkCoast.ratio + (now - linkCoast.at) / 1000 / seconds, 1);
  }
  linkCoast = null;
  if (gridLoop) return wrapPhase(host.ppq, beats) / beats;
  if (seconds <= 0) return 0;
  const elapsed = (now - linkFreeAt) / 1000;
  if (linkClip.loop) return wrapPhase(elapsed / seconds, 1);
  if (elapsed >= seconds) return 1;
  return elapsed / seconds;
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
  if (current && current.id === sample.id && isPlaying()) {
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
  if (linkedOn) {
    const ratio = linkedRatio(performance.now());
    const seconds = linkFileSeconds();
    if (ratio == null) {
      head.hidden = true;
      return;
    }
    head.hidden = false;
    const x = Math.round(Math.min(1, Math.max(0, ratio)) * (wave.clientWidth || 0));
    head.style.transform = `translate3d(${x}px,0,0)`;
    if (seconds) {
      const label = `${fmtClock(ratio * seconds)} / ${fmtClock(seconds)}`;
      if ($("now-time").textContent !== label) $("now-time").textContent = label;
    }
    return;
  }
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
  const running = linkedOn ? linkedAudible : !audio.paused && !audio.ended;
  if (running) waveRaf = requestAnimationFrame(waveFrame);
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

const FONT_MIN = 0.8;
const FONT_MAX = 1.6;
const FONT_STEP = 0.1;

function currentFontScale() {
  const raw = Number(getComputedStyle(document.documentElement).getPropertyValue("--font-scale"));
  if (!Number.isFinite(raw)) return 1;
  const stepped = Math.round(raw * 10) / 10;
  return Math.min(FONT_MAX, Math.max(FONT_MIN, stepped));
}

function applyFontScale(scale) {
  const next = Math.min(FONT_MAX, Math.max(FONT_MIN, Math.round(scale * 10) / 10));
  document.documentElement.style.setProperty("--font-scale", String(next));
  try {
    localStorage.setItem("hoarder-font-scale", String(next));
  } catch (_) {}
  $("font-size-label").textContent = `${Math.round(next * 100)}%`;
  $("font-down").disabled = next <= FONT_MIN;
  $("font-up").disabled = next >= FONT_MAX;
}

function bind() {
  try {
    $("hide-dupes").checked = localStorage.getItem("hoarder-hide-dupes") !== "0";
  } catch (_) {}

  applyFontScale(currentFontScale());
  $("font-down").addEventListener("click", () => applyFontScale(currentFontScale() - FONT_STEP));
  $("font-up").addEventListener("click", () => applyFontScale(currentFontScale() + FONT_STEP));

  $("theme").value = document.documentElement.dataset.theme || "dark";
  $("theme").addEventListener("change", () => {
    document.documentElement.dataset.theme = $("theme").value;
    localStorage.setItem("hoarder-theme", $("theme").value);
    drawWave();
  });

  paintLength();
  paintTempo();
  bindRange("length-range", lengthPos, lengthTarget, setLength);
  bindRange("tempo-range", tempoPos, tempoTarget, setTempo);
  for (const id of ["len-min", "len-max"]) {
    const which = id === "len-min" ? "min" : "max";
    $(id).addEventListener("pointerdown", () => {
      $("len-min").style.zIndex = which === "min" ? "2" : "1";
      $("len-max").style.zIndex = which === "max" ? "2" : "1";
    });
    $(id).addEventListener("input", () => onLengthInput(which));
  }
  for (const id of ["bpm-min", "bpm-max"]) {
    const which = id === "bpm-min" ? "min" : "max";
    $(id).addEventListener("pointerdown", () => {
      $("bpm-min").style.zIndex = which === "min" ? "2" : "1";
      $("bpm-max").style.zIndex = which === "max" ? "2" : "1";
    });
    $(id).addEventListener("input", () => onTempoInput(which));
  }
  const tempoExact = $("tempo-exact");
  tempoExact.addEventListener("change", () => applyExactTempo());
  tempoExact.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    applyExactTempo();
  });

  renderRoles();
  renderGenres();
  fillTagEditor();
  $("tag-editor").addEventListener("click", (event) => event.stopPropagation());
  $("q").addEventListener("input", () => scheduleList());
  fillKeys($("target-key"));
  fillKeys($("key-filter"));
  mountPick($("repeat"));
  mountPick($("target-key"));
  mountPick($("key-filter"));
  mountPick($("theme"));
  $("key-filter").addEventListener("change", () => {
    offset = 0;
    refreshList();
  });
  paintSortArrows();
  $("hit-head").addEventListener("click", (event) => {
    const button = event.target.closest(".col-sort");
    if (!button) return;
    const next = sortBy === button.dataset.sort && sortDir === "asc" ? "desc" : "asc";
    applySort(button.dataset.sort, next);
  });

  $("target-key").addEventListener("change", () => {
    if (!current) return;
    $("now-name").textContent = pitchedLabel(current);
    if (isPlaying()) play(current);
  });

  const settings = $("settings");
  const menu = $("settings-menu");
  const helpBtn = $("help");
  const helpMenu = $("help-menu");
  const genreBtn = $("genre-btn");
  const genrePanel = $("genre-panel");
  function setMenu(button, panel, open) {
    panel.hidden = !open;
    button.setAttribute("aria-expanded", open ? "true" : "false");
  }
  function closePopups() {
    setMenu(settings, menu, false);
    setMenu(helpBtn, helpMenu, false);
    setMenu(genreBtn, genrePanel, false);
    closePicks();
    closeFileMenu();
    closeTagEditor();
  }
  settings.addEventListener("click", (event) => {
    event.stopPropagation();
    closePicks();
    const open = menu.hidden;
    setMenu(helpBtn, helpMenu, false);
    setMenu(genreBtn, genrePanel, false);
    setMenu(settings, menu, open);
  });
  helpBtn.addEventListener("click", (event) => {
    event.stopPropagation();
    closePicks();
    const open = helpMenu.hidden;
    setMenu(settings, menu, false);
    setMenu(genreBtn, genrePanel, false);
    setMenu(helpBtn, helpMenu, open);
  });
  genreBtn.addEventListener("click", (event) => {
    event.stopPropagation();
    closePicks();
    const open = genrePanel.hidden;
    setMenu(settings, menu, false);
    setMenu(helpBtn, helpMenu, false);
    setMenu(genreBtn, genrePanel, open);
  });
  menu.addEventListener("click", (event) => event.stopPropagation());
  helpMenu.addEventListener("click", (event) => event.stopPropagation());
  genrePanel.addEventListener("click", (event) => event.stopPropagation());
  document.addEventListener("click", () => closePopups());
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    closePopups();
  });
  $("hits").addEventListener("scroll", () => {
    closeFileMenu();
    closeTagEditor();
  });
  $("open-explorer").addEventListener("click", (event) => {
    event.stopPropagation();
    const sample = menuSample;
    closeFileMenu();
    if (sample) reveal(sample);
  });
  $("copy-modified").addEventListener("click", (event) => {
    event.stopPropagation();
    const sample = menuSample;
    closeFileMenu();
    if (sample) copyModified(sample);
  });
  $("hide-dupes").addEventListener("change", () => {
    try {
      localStorage.setItem("hoarder-hide-dupes", $("hide-dupes").checked ? "1" : "0");
    } catch (_) {}
    offset = 0;
    refreshStatus();
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
    if (isPlaying()) stop();
    else play(current);
  });
  $("repeat").addEventListener("change", () => {
    if (link.connected && current && linkedOn) play(current);
  });
  audio.addEventListener("loadedmetadata", () => applyPitch(current));
  audio.addEventListener("playing", () => {
    applyPitch(current);
    startWave();
  });
  audio.addEventListener("pause", () => {
    if (linkedOn) return;
    stopWave();
  });
  audio.addEventListener("ended", () => {
    if (linkedOn) return;
    stop();
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
      if (isPlaying()) stop();
      else play(current);
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
