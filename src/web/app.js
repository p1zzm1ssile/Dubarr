"use strict";

// ---------- Helpers ----------

const view = document.getElementById("view");
const REPO = "https://github.com/p1zzm1ssile/Dubarr";
const EMAIL = "p1zzm1ssilerepo@gmail.com";

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", "X-Dubarr": "1", ...(options.headers || {}) },
  });
  if (response.status === 401) {
    window.location.href = "/login";
    throw new Error("Please log in");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status})`);
    error.data = data;
    throw error;
  }
  return data;
}

const post = (path, body = {}) => api(path, { method: "POST", body: JSON.stringify(body) });

// Builds elements. Text always goes in as text, never as HTML.
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "style") node.style.cssText = value; // CSP allows styles set from script, not style attributes
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function bytes(n) {
  if (!n) return "0 GB";
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return i === 0 ? `${n} bytes` : `${n.toFixed(1)} ${units[i]}`;
}

function ago(timestamp) {
  const seconds = Math.round(Date.now() / 1000 - timestamp);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400 * 2) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function until(timestamp) {
  const seconds = Math.round(timestamp - Date.now() / 1000);
  if (seconds <= 60) return "soon";
  if (seconds < 3600) return `in ${Math.round(seconds / 60)}m`;
  return `in ${Math.round(seconds / 3600)}h`;
}

function duration(seconds) {
  if (seconds < 60) return "under a minute";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return m ? `${h} h ${m} min` : `${h} h`;
}

const when = (timestamp) => new Date(timestamp * 1000).toLocaleString();

function splitPath(path) {
  const i = path.lastIndexOf("/");
  return [path.slice(0, i), path.slice(i + 1)];
}

let languages = {};
let detection = { audio: false, subtitles: false };  // which Detect buttons to show, from /api/files
const langName = (code) => languages[code] || { und: "Unknown", mul: "Multiple", zxx: "No speech" }[code] || code;

let refreshTimer = null;
function every(ms, fn) {
  clearInterval(refreshTimer);
  refreshTimer = setInterval(fn, ms);
}

function flash(node, ok, message) {
  node.className = `result ${ok ? "ok" : "bad"}`;
  node.textContent = (ok ? "✓ " : "✗ ") + message;
}

// ---------- Header ----------

async function refreshHealth() {
  const pill = document.getElementById("health");
  const text = document.getElementById("health-text");
  try {
    const h = await api("/api/status");
    pill.className = "pill " + (h.healthy ? "ok" : "bad");
    text.textContent = !h.healthy ? "Needs attention" : h.activity ? h.activity.task + "…" : "Healthy";
    pill.title = [...h.problems, h.report_only ? "Report only: no files are changed by themselves" : "Changing files by itself"].join("\n");
    return h;
  } catch {
    pill.className = "pill bad";
    text.textContent = "Not responding";
    return null;
  }
}

document.getElementById("logout").addEventListener("click", async () => {
  await post("/api/logout").catch(() => {});
  window.location.href = "/login";
});

api("/api/session").then((s) => {
  document.getElementById("whoami").textContent = s.user || "";
  if (s.user === "(local network)") document.getElementById("logout").textContent = "Log in";
});

// ---------- Dashboard ----------

const STATUS_STYLE = {
  change: "info", not_bazarr: "info", seeding: "", no_wanted_audio: "warn", unknown_audio: "warn", unknown_subtitles: "warn", unknown_original: "warn", original_only: "ok", too_small: "",
  error: "bad", no_audio: "", clean: "ok", ignored: "", auto_tagged: "warn",
};

const STATUS_HELP = {
  change: "These have audio or subtitles in languages you don't keep. Apply them here, or turn off Report only in Settings and Dubarr does it by itself.",
  not_bazarr: "Subtitle files beside films and episodes that Bazarr didn't download, such as ones that came with the download. Remove them here, or choose Remove them by themselves in Settings > Bazarr. They go to the recycle folder, so you can restore them from History.",
  seeding: "Hardlinked files, usually still seeding in your torrent client. Dubarr waits until seeding stops, so the film isn't stored twice.",
  no_wanted_audio: "None of the audio is in a language you keep, so Dubarr leaves these alone. Replace them in Radarr or Sonarr if you want them in your language.",
  unknown_audio: "At least one audio track has no language tag, so Dubarr can't tell what it is and leaves the file alone. Press Listen & tag to hear it and set its language; the tag is written into the file, so it won't come back here.",
  unknown_subtitles: "Nothing to remove, but a subtitle track has no language tag. Press Listen & tag to read a few lines and set its language, so Dubarr knows whether to keep it.",
  auto_tagged: "Tagged from a detected language, which could be wrong, so Dubarr hasn't removed any tracks yet. Press Check tracks to listen, then Looks right to let Dubarr change the file as usual. After 7 days it goes ahead by itself.",
  unknown_original: "Nothing else to remove, but there's audio in another language that might be the original, and Always keep the original language audio is on. Connect Radarr or Sonarr so Dubarr knows the original language.",
  original_only: "None of your languages, but the audio in the original language is kept, e.g. Japanese-only anime. Nothing to do, unless you'd rather find a dubbed copy in Radarr or Sonarr.",
  error: "Files that couldn't be read or changed. The original is always left as it was.",
  no_audio: "Files with no audio at all.",
  clean: "Only languages you keep. Nothing to do.",
  too_small: "These would save less than the minimum set in Settings, and rewriting a whole film for a few KB isn't worth the wear on the array. Apply one by hand if you want it anyway, or lower the setting.",
  ignored: "Files you restored. Dubarr leaves them alone until you say otherwise.",
};

const CODEC_NAMES = {
  truehd: "TrueHD", eac3: "DD+", ac3: "Dolby Digital", dts: "DTS", aac: "AAC", flac: "FLAC", opus: "Opus",
  mp3: "MP3", vorbis: "Vorbis", alac: "ALAC", subrip: "SRT", srt: "SRT", ass: "ASS", ssa: "SSA", webvtt: "WebVTT",
  mov_text: "Text", hdmv_pgs_subtitle: "PGS", dvd_subtitle: "VobSub", dvb_subtitle: "DVB",
};
const CHANNELS = { 1: "mono", 2: "stereo", 6: "5.1", 7: "6.1", 8: "7.1" };

function format(t) {
  let name = CODEC_NAMES[t.codec] || (t.codec.startsWith("pcm") ? "PCM" : t.codec.toUpperCase());
  const profile = (t.profile || "").toLowerCase();
  if (t.codec === "dts" && profile.includes("hd ma")) name = "DTS-HD MA";
  else if (t.codec === "dts" && profile.includes("hd")) name = "DTS-HD";
  if (profile.includes("dts:x")) name = "DTS:X";
  if (profile.includes("atmos")) name += " Atmos";
  if (t.type === "audio" && t.channels) name += ` ${CHANNELS[t.channels] || `${t.channels}ch`}`;
  return name;
}

// Bazarr's names for the places it downloads subtitles from
const PROVIDERS = {
  opensubtitles: "OpenSubtitles.org", opensubtitlescom: "OpenSubtitles.com", subdl: "SubDL", podnapisi: "Podnapisi",
  addic7ed: "Addic7ed", subf2m: "Subf2m", yifysubtitles: "YIFY Subtitles", gestdown: "Gestdown",
  embeddedsubtitles: "extracted from the video", whisperai: "Whisper (made by AI)", subsource: "SubSource",
  animetosho: "AnimeTosho", jimaku: "Jimaku", napiprojekt: "NapiProjekt", supersubtitles: "SuperSubtitles",
  titulky: "Titulky", subssabbz: "Subs.sab.bz", subsunacs: "Subsunacs", tvsubtitles: "TVsubtitles",
};

function source(f) {
  if (f.from_bazarr) return f.source ? `Bazarr, from ${PROVIDERS[f.source] || f.source}` : "Bazarr";
  return f.from_bazarr === false ? "not from Bazarr" : "";
}

function trackChips(file) {
  const removed = new Set(file.remove);
  const willForce = new Set(file.mark_forced || []);
  return el("div", { class: "tracks" }, file.tracks.map((t) => {
    const kind = t.type === "audio" ? "audio" : "subs";
    const extra = [(t.forced || willForce.has(t.index)) && "forced", t.commentary && "commentary", t.hearing_impaired && "SDH",
      t.visual_impaired && "audio description"].filter(Boolean);
    const label = `${langName(t.language)} ${kind} · ${format(t)}${extra.length ? ` (${extra.join(", ")})` : ""}`;
    return el("span", {
      class: `track${t.type === "subtitle" ? " sub" : ""}${removed.has(t.index) ? " removed" : ""}`,
      title: [t.title ? `“${t.title}”` : "", t.frames && t.type === "subtitle" ? `${t.frames} lines` : "",
        t.size ? bytes(t.size) : "", t.raw_language ? `tagged "${t.raw_language}"` : "untagged",
        removed.has(t.index) ? "will be removed" : willForce.has(t.index) ? "kept, and flagged as forced" : "kept"]
        .filter(Boolean).join(" · "),
      text: label,
    });
  }).concat((file.files || []).map((f) => {
    const gone = (file.remove_files || []).includes(f.path);
    const extra = [f.forced && "forced", f.hearing_impaired && "SDH"].filter(Boolean);
    const from = source(f);
    return el("span", {
      class: `track sub file${gone ? " removed" : ""}${f.from_bazarr ? " bazarr" : f.from_bazarr === false ? " not-bazarr" : ""}`,
      title: [f.path.split("/").pop(), f.size ? bytes(f.size) : "", gone ? "will be removed" : "kept"].filter(Boolean).join(" · "),
      text: `${langName(f.language)} subs · ${f.format.toUpperCase()} file${extra.length ? ` (${extra.join(", ")})` : ""}${from ? ` · ${from}` : ""}`,
    });
  })));
}

function languageSelect(current) {
  const options = Object.entries(languages).sort((a, b) => a[1].localeCompare(b[1]));
  return el("select", { "aria-label": "Language" },
    current === "und" ? el("option", { value: "und", text: "Unknown, choose…", selected: true }) : null,
    el("option", { value: "zxx", text: "No speech (music or effects only)", selected: current === "zxx" }),
    options.map(([code, name]) => el("option", { value: code, text: name, selected: code === current })));
}

// A suggested language, with a button to choose it in the track's language list
function suggestion(g, select) {
  const use = el("button", { type: "button", text: `Use ${g.name}` });
  use.addEventListener("click", () => {
    select().value = g.language;
    use.disabled = true;
    use.textContent = `${g.name} chosen. Press Save tags`;
  });
  return el("div", { class: `detect-result ${g.sure ? "" : "unsure"}` },
    el("span", { text: `${g.by} thinks it's ${g.name} (${Math.round(g.confidence * 100)}% sure). `
      + (g.sure ? "" : "Not very sure: listen or read to check before using it. ") }), use);
}

// Ask Whisper, Ollama or the built-in detector for a track's language, and offer it as a suggestion.
// Asking again listens to parts of the film it hasn't heard yet, in case music fooled it.
function detectButton(file, t, preview, select) {
  if (!(t.type === "audio" ? detection.audio : detection.subtitles)) return null;
  const button = el("button", { type: "button", text: "Detect", title: t.type === "audio"
    ? "Whisper listens to a sample and suggests the language" : "Reads some lines and suggests the language" });
  let start = 0;
  button.addEventListener("click", async () => {
    button.disabled = true;
    preview.replaceChildren(el("div", { class: "muted small", text: t.type === "audio"
      ? (start ? "Listening to other parts of the film…" : "Listening… Whisper can take a little while, and tries other parts of the film if there's no talking.")
      : "Reading…" }));
    try {
      const g = await post("/api/detect", { path: file.path, index: t.index, start });
      preview.replaceChildren(suggestion(g, select));
      if (t.type === "audio") {
        start += 3;
        button.textContent = "Detect again";
        button.title = "Listens to other parts of the film";
      }
    } catch (e) {
      preview.replaceChildren(el("div", { class: "error-text", text: e.message }));
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

function tagPanel(file, onSaved) {
  const isMp4 = /\.(mp4|m4v)$/i.test(file.path);
  const selects = {};
  const removeTrack = {};
  const removeFile = {};
  const result = el("span", { class: "result", role: "status" });
  const query = (t) => new URLSearchParams({ path: file.path, index: t.index });

  const rows = file.tracks.map((t) => {
    const preview = el("div", { class: "tag-preview" });
    let button;
    if (t.type === "audio") {
      button = el("button", { type: "button", text: "Play a 30s sample" });
      let part = 0;
      button.addEventListener("click", () => {
        const q = query(t);
        q.set("part", part);
        preview.replaceChildren(el("audio", { controls: true, autoplay: true, preload: "auto", src: `/api/sample?${q}` }),
          el("div", { class: "help", text: part === 0
            ? "Taken from about a quarter of the way in. It can take a few seconds to start. No talking? Try another part."
            : "Taken from another part of the film. It can take a few seconds to start." }));
        part += 1;
        button.textContent = "Try another part";
      });
    } else {
      button = el("button", { type: "button", text: "Show some lines" });
      button.addEventListener("click", async () => {
        button.disabled = true;
        preview.replaceChildren(el("div", { class: "muted small", text: "Reading…" }));
        try {
          const { lines } = await api(`/api/subtitle-lines?${query(t)}`);
          preview.replaceChildren(el("ul", { class: "sub-lines" }, lines.map((l) => el("li", { text: l }))));
        } catch (e) {
          preview.replaceChildren(el("div", { class: "error-text", text: e.message }));
        } finally {
          button.disabled = false;
        }
      });
    }
    selects[t.index] = languageSelect(t.language);
    const detect = detectButton(file, t, preview, () => selects[t.index]);
    const guess = (file.guesses || {})[t.index];
    if (guess && t.language === "und") preview.replaceChildren(suggestion(guess, () => selects[t.index]));
    removeTrack[t.index] = el("input", { type: "checkbox", "aria-label": `Remove track ${t.index}` });
    const details = [t.type === "audio" ? "Audio" : "Subtitles", `track ${t.index}`, t.codec,
      t.channels ? `${t.channels} ch` : "", t.title ? `“${t.title}”` : "",
      t.forced ? "forced" : "", t.commentary ? "commentary" : ""].filter(Boolean).join(" · ");
    return el("div", { class: `tag-track${t.language === "und" ? " untagged" : ""}` },
      el("div", { class: "tag-head" },
        el("div", {}, el("strong", { text: details }),
          el("div", { class: "small muted", text: t.language === "und" ? "No language tag" : `Tagged ${langName(t.language)}` })),
        el("div", { class: "tag-controls" }, button, detect, selects[t.index],
          el("label", { class: "remove-pick" }, removeTrack[t.index], "Remove"))),
      preview);
  });

  const fileRows = (file.files || []).map((f) => {
    removeFile[f.path] = el("input", { type: "checkbox", "aria-label": `Remove ${f.path.split("/").pop()}` });
    const from = source(f);
    return el("div", { class: "tag-track" },
      el("div", { class: "tag-head" },
        el("div", {}, el("strong", { text: `Subtitle file · ${f.path.split("/").pop()}` }),
          el("div", { class: "small muted" }, `${langName(f.language)} · ${f.format.toUpperCase()}${f.size ? ` · ${bytes(f.size)}` : ""}`,
            from ? el("span", { class: `badge ${f.from_bazarr ? "ok" : "warn"}`, text: from }) : null)),
        el("div", { class: "tag-controls" }, el("label", { class: "remove-pick" }, removeFile[f.path], "Remove"))));
  });

  const remove = el("button", { type: "button", class: "danger", text: "Remove ticked" });
  remove.addEventListener("click", async () => {
    const tracks = file.tracks.filter((t) => removeTrack[t.index].checked).map((t) => t.index);
    const files = (file.files || []).filter((f) => removeFile[f.path].checked).map((f) => f.path);
    if (!tracks.length && !files.length) { flash(result, false, "Tick Remove on at least one track or file first"); return; }
    const n = tracks.length + files.length;
    if (!confirm(`Remove ${n} ticked track${n === 1 ? "" : "s"} and file${n === 1 ? "" : "s"} from ${file.path.split("/").pop()}? `
      + (tracks.length ? "A copy without them is written and checked, and the original goes to the recycle folder. " : "")
      + "You can restore from History.")) return;
    remove.disabled = true;
    try {
      flash(result, true, (await post("/api/remove", { path: file.path, tracks, files })).message);
      setTimeout(onSaved, 1500);
    } catch (e) {
      flash(result, false, e.message);
      remove.disabled = false;
    }
  });

  const save = el("button", { type: "button", class: "primary", text: "Save tags" });
  save.addEventListener("click", async () => {
    const tags = {};
    for (const t of file.tracks) {
      const value = selects[t.index].value;
      if (value !== t.language && value !== "und") tags[t.index] = value;
    }
    if (!Object.keys(tags).length) { flash(result, false, "Choose a language for at least one track first"); return; }
    save.disabled = true;
    result.className = "result muted";
    result.textContent = "Saving…";
    try {
      const r = await post("/api/tag", { path: file.path, tags });
      flash(result, true, r.message);
      setTimeout(onSaved, 1200);
    } catch (e) {
      flash(result, false, e.message);
    } finally {
      save.disabled = false;
    }
  });

  return el("div", { class: "tag-panel" },
    el("p", { class: "small muted", text: "Listen to each track or read a few lines, choose its language, and save. "
      + "Or tick Remove on the tracks and subtitle files you don't want, and press Remove ticked. "
      + (isMp4 ? "MP4 files can't be edited in place, so Dubarr writes a tagged copy and keeps the original in the recycle folder."
        : "The tag is written into the file in a moment, without copying it, so the file won't be flagged again.") }),
    rows,
    fileRows,
    el("div", { class: "card-actions" }, save, remove, result));
}

async function showDashboard(selected) {
  let status = selected || "change";
  let shown = [];
  let total = 0;
  const openPanels = new Set();

  const notices = el("div");
  const activity = el("div", { class: "panel" });
  const stats = el("div", { class: "panel stats" });
  const tiles = el("div", { class: "tiles" });
  const search = el("input", { type: "search", placeholder: "Search by name or folder", "aria-label": "Search files" });
  const listTitle = el("h2");
  const listHelp = el("p", { class: "muted small" });
  const applyAll = el("button", { type: "button", class: "primary" });
  // Accept all: tag every untagged track whose suggestion is at least this sure
  const acceptLevel = el("input", { type: "number", min: 50, max: 100, value: 90, "aria-label": "How sure, in percent",
    class: "accept-level" });
  const acceptButton = el("button", { type: "button", class: "primary" });
  const accept = el("span", { class: "accept-all" }, acceptButton, acceptLevel, "% sure or more");
  const labelAccept = () => {
    acceptButton.textContent = library ? `Accept all in ${library} at least` : "Accept all at least";
    accept.hidden = !["unknown_audio", "unknown_subtitles"].includes(status) || !currentCount
      || !(detection.audio || detection.subtitles);
  };
  acceptButton.addEventListener("click", () => {
    const minimum = Number(acceptLevel.value);
    if (!(minimum >= 50 && minimum <= 100)) { flash(listResult, false, "Pick a level from 50% to 100%"); return; }
    if (!confirm(`Tag every untagged track whose suggestion is at least ${minimum}% sure${library ? ` in ${library}` : ""}? `
      + "Files tagged this way wait under Tagged automatically before any tracks are removed, unless the guess passes the level set in Settings > Language detection.")) return;
    act("/api/accept", { minimum, library: library || null }, acceptButton);
  });
  // "" for every library, or the one library to list and apply
  let library = "";
  const librarySelect = el("select", { "aria-label": "Library", class: "library-select" });
  librarySelect.addEventListener("change", () => { library = librarySelect.value; labelApplyAll(); labelAccept(); loadFiles(false); });
  let currentCount = 0;
  const table = el("table");
  const more = el("div");
  const listResult = el("span", { class: "result", role: "status" });

  view.replaceChildren(
    el("h1", { text: "Dashboard" }),
    notices, activity, stats, tiles,
    el("div", { class: "panel" },
      listTitle, listHelp,
      el("div", { class: "toolbar" }, el("div", { style: "flex:1;min-width:200px" }, search), librarySelect, applyAll, accept, listResult),
      el("div", { style: "overflow-x:auto" }, table), more));

  async function act(path, body, button, done) {
    if (button) button.disabled = true;
    try {
      const r = await post(path, body);
      flash(listResult, true, r.message || "Done");
      if (done) done();
      setTimeout(refresh, 800);
    } catch (e) {
      flash(listResult, false, e.message);
    } finally {
      if (button) button.disabled = false;
    }
  }

  function renderRows() {
    const rows = shown.map((f) => {
      const [folder, name] = splitPath(f.path);
      const actions = [];
      if (["change", "too_small", "not_bazarr"].includes(f.status) && !f.ignored) {
        const b = el("button", { type: "button", text: f.status === "not_bazarr" ? "Remove" : "Apply" });
        b.addEventListener("click", () => act("/api/apply", { paths: [f.path] }, b));
        actions.push(b);
      }
      if (f.ignored) {
        const b = el("button", { type: "button", text: "Stop leaving alone", title: "Dubarr will deal with it again, like any other file" });
        b.addEventListener("click", () => act("/api/ignore", { path: f.path, ignored: false }, b, () => {}));
        actions.push(b);
      } else if (!["change", "clean", "too_small", "not_bazarr"].includes(f.status)) {
        const b = el("button", { type: "button", text: "Re-check", title: "Read the file again" });
        b.addEventListener("click", () => act("/api/recheck", { path: f.path }, b));
        actions.push(b);
      }
      if (f.status === "auto_tagged" && !f.ignored) {
        const b = el("button", { type: "button", class: "primary", text: "Looks right",
          title: "You've checked the tags: let Dubarr change this file as usual" });
        b.addEventListener("click", () => act("/api/looks-right", { path: f.path }, b));
        actions.push(b);
      }
      const suggested = Object.entries(f.guesses || {}).filter(([i]) => f.tracks.some((t) => String(t.index) === i && t.language === "und"));
      if (suggested.length && !f.ignored) {
        const b = el("button", { type: "button", text: "Use suggestions",
          title: suggested.map(([i, g]) => `Track ${i}: ${g.name} (${Math.round(g.confidence * 100)}%)`).join(", ") });
        b.addEventListener("click", () => act("/api/tag", { path: f.path, tags: Object.fromEntries(suggested.map(([i, g]) => [i, g.language])) }, b));
        actions.push(b);
      }
      const panel = el("div");
      const untagged = f.tracks.some((t) => t.language === "und");
      if (f.tracks.length) {
        const b = el("button", { type: "button", text: untagged ? "Listen & tag" : "Check tracks",
          title: "Hear the audio, read the subtitles, and fix their language tags" });
        const toggle = () => {
          if (openPanels.has(f.path) && panel.firstChild) { openPanels.delete(f.path); panel.replaceChildren(); return; }
          openPanels.add(f.path);
          panel.replaceChildren(tagPanel(f, () => { openPanels.delete(f.path); refresh(); }));
        };
        b.addEventListener("click", toggle);
        actions.push(b);
        if (openPanels.has(f.path)) panel.replaceChildren(tagPanel(f, () => { openPanels.delete(f.path); refresh(); }));
      }
      return el("tr", { class: "file-row" },
        el("td", { class: "name" },
          el("div", { class: "title", text: name }),
          el("div", { class: "path mono", text: folder }),
          el("div", { class: "small", text: f.message + (f.saved_bytes ? ` · saves ${bytes(f.saved_bytes)}` : "") }),
          suggested.length ? el("div", { class: "small suggested", text: "Suggested: " + suggested.map(([i, g]) => {
            const t = f.tracks.find((x) => String(x.index) === i);
            return `${t.type === "audio" ? "audio" : "subtitles"} track ${i} ${g.name} (${Math.round(g.confidence * 100)}%, ${g.by})`;
          }).join(", ") }) : null,
          f.tracks.length || (f.files || []).length ? trackChips(f) : null,
          panel),
        el("td", { class: "actions" }, el("div", { class: "action-stack" }, actions)));
    });
    table.replaceChildren(el("tbody", {}, rows.length ? rows
      : [el("tr", {}, el("td", { class: "empty", text: search.value ? "Nothing matches your search." : "Nothing here." }))]));
    more.replaceChildren();
    if (shown.length < total) {
      const b = el("button", { type: "button", text: `Show more (${total - shown.length} left)` });
      b.addEventListener("click", () => loadFiles(true));
      more.append(el("p", {}, b));
    }
  }

  async function loadFiles(append) {
    const params = new URLSearchParams({ status, search: search.value.trim(), offset: append ? shown.length : 0, library });
    const r = await api(`/api/files?${params}`);
    if (r.detect) { detection = r.detect; labelAccept(); }
    shown = append ? shown.concat(r.files) : r.files;
    total = r.total;
    renderRows();
  }

  function renderActivity(h) {
    const a = h && h.activity;
    if (a) {
      const fraction = a.bytes_total ? a.bytes_done / a.bytes_total : a.total ? a.done / a.total : null;
      const pct = fraction === null ? null : Math.min(100, Math.round(fraction * 100));
      const eta = a.eta_seconds !== null && a.eta_seconds !== undefined ? ` · about ${duration(a.eta_seconds)} left` : "";
      const stop = el("button", { type: "button", class: "danger", text: "Stop" });
      stop.addEventListener("click", () => act("/api/stop", {}, stop));
      activity.replaceChildren(
        el("div", { class: "toolbar", style: "margin:0" },
          el("strong", { text: `${a.task}…` }),
          el("span", { class: "muted small", text: (a.total ? `${a.done.toLocaleString()} of ${a.total.toLocaleString()}` : a.detail === "Listing files" ? "listing files" : `${a.done} file(s) so far`)
            + (a.total && pct !== null ? ` (${pct}%)` : "") + (a.done ? eta : "") }),
          el("span", { class: "spacer" }), stop),
        pct !== null ? el("div", { class: "progress" }, el("span", { style: `width:${pct}%` })) : el("div", { class: "progress" }, el("span", { style: "width:100%;opacity:.35" })),
        el("div", { class: "muted small mono", style: "word-break:break-all", text: a.detail || "" }));
      return;
    }
    const scan = el("button", { type: "button", text: "Scan now" });
    scan.addEventListener("click", () => act("/api/scan", {}, scan));
    const last = h && h.last_scan ? `Last scan ${ago(h.last_scan)}` : "Not scanned yet";
    const next = h && h.next_scan ? ` · next ${until(h.next_scan)}` : "";
    const mode = h && h.report_only ? "Report only: nothing is changed unless you apply it"
      : h && !h.in_window ? "Changing files by itself, during the hours set in Settings (outside them now)"
      : "Changing files by itself";
    activity.replaceChildren(el("div", { class: "toolbar", style: "margin:0" },
      el("div", {}, el("strong", { text: "Idle" }), el("div", { class: "muted small", text: `${last}${next}. ${mode}.` })),
      el("span", { class: "spacer" }), scan));
  }

  async function refresh() {
    const [d, h] = await Promise.all([api("/api/dashboard"), refreshHealth()]);
    renderActivity(h);

    notices.replaceChildren();
    if (!d.libraries.length) {
      notices.append(el("div", { class: "notice warn" }, el("strong", { text: "No libraries yet. " }),
        "Add your movie and TV folders in ", el("a", { href: "#/settings/libraries", text: "Settings > Libraries" }), "."));
    }
    for (const lib of d.libraries.filter((l) => !l.exists)) {
      notices.append(el("div", { class: "notice bad", text: `The library folder ${lib.path} doesn't exist inside the container. Check its path mapping.` }));
    }
    for (const clash of d.bazarr_clashes || []) {
      notices.append(el("div", { class: "notice warn" }, el("strong", { text: "Bazarr and Dubarr disagree. " }),
        `Bazarr's "${clash.profile}" profile downloads ${clash.languages.join(", ")} subtitles, which Dubarr removes, so they'd keep coming back. `,
        "Remove them from the profile in Bazarr (Settings > Languages), or add them to Dubarr's subtitle languages in ",
        el("a", { href: "#/settings/general", text: "Settings" }), "."));
    }
    if (d.report_only && d.libraries.length) {
      notices.append(el("div", { class: "notice" }, el("strong", { text: "Report only is on. " }),
        "Dubarr hasn't changed any files. Check the list below, then apply files one at a time or all at once, or turn off Report only in ",
        el("a", { href: "#/settings/general", text: "Settings" }), " to let Dubarr change them by itself."));
    }

    const pending = d.statuses.find((s) => s.key === "change");
    stats.replaceChildren(
      el("div", { class: "stat" }, el("span", { class: "num", text: bytes(d.saved.saved_bytes) }),
        el("span", { class: "label", text: `saved so far, from ${d.saved.files} file(s)` })),
      el("div", { class: "stat" }, el("span", { class: "num", text: bytes(pending.saved_bytes) }),
        el("span", { class: "label", text: `to save from ${pending.files} file(s) waiting` })));

    tiles.replaceChildren(...d.statuses.filter((s) => s.files || ["change", "no_wanted_audio", "clean"].includes(s.key)).map((s) =>
      el("button", { type: "button", class: `tile ${STATUS_STYLE[s.key] || ""}${s.key === status ? " active" : ""}`,
        onclick: () => { location.hash = `#/dashboard/${s.key}`; } },
        el("span", { class: "num", text: s.files.toLocaleString() }), el("span", { class: "label", text: s.label }))));

    const current = d.statuses.find((s) => s.key === status) || pending;
    listTitle.textContent = current.label;
    listHelp.textContent = STATUS_HELP[status] || "";
    const paths = d.libraries.map((l) => l.path);
    if (!paths.includes(library)) library = "";
    librarySelect.hidden = paths.length < 2;
    librarySelect.replaceChildren(el("option", { value: "", text: "All libraries", selected: !library }),
      ...paths.map((p) => el("option", { value: p, text: p, selected: p === library })));
    currentCount = current.files;
    applyAll.hidden = !["change", "not_bazarr"].includes(status) || !current.files;
    labelApplyAll();
    labelAccept();
    if (!openPanels.size) await loadFiles(false);
  }

  function labelApplyAll() {
    const verb = status === "not_bazarr" ? "Remove" : "Apply";
    applyAll.textContent = library ? `${verb} all in ${library}` : `${verb} all ${currentCount}`;
  }

  applyAll.addEventListener("click", () => {
    const what = library ? `every file in ${library}` : `all ${currentCount} file(s)`;
    const question = status === "not_bazarr"
      ? `Remove the subtitle files Bazarr didn't download from ${what} now? They go to the recycle folder, so you can restore them from History.`
      : `Remove the unwanted tracks from ${what} now? Originals go to the recycle folder, so you can restore them from History.`;
    if (!confirm(question)) return;
    act("/api/apply", { library: library || null, status }, applyAll);
  });

  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => loadFiles(false), 300); });

  if (!Object.keys(languages).length) languages = (await api("/api/settings")).languages;
  await refresh();
  let wasBusy = false;
  every(5000, async () => {
    const h = await refreshHealth();
    renderActivity(h);
    // While a job runs, and once more when it ends, keep the counts and list fresh
    const busy = !!(h && h.activity);
    if (busy || wasBusy) refresh();
    wasBusy = busy;
  });
}

// ---------- History ----------

const STATE_LABEL = {
  done: "Original kept", restored: "Restored", purged: "Original emptied from recycle folder",
  deleted: "Original deleted", failed: "Failed", swapping: "In progress", tagged: "Tagged in place",
};

async function showHistory() {
  const search = el("input", { type: "search", placeholder: "Search by name or folder", "aria-label": "Search history" });
  const restoreBatch = el("button", { type: "button", text: "Restore the last batch" });
  const result = el("span", { class: "result", role: "status" });
  const intro = el("p", { class: "muted small" });
  const table = el("table");
  const more = el("div");
  let entries = [];
  let total = 0;
  let run = null;

  view.replaceChildren(
    el("h1", { text: "History" }), intro,
    el("div", { class: "toolbar" }, el("div", { style: "flex:1;min-width:200px" }, search), restoreBatch, result),
    el("div", { class: "panel", style: "padding:0;overflow-x:auto" }, table), more);

  function render() {
    const head = el("thead", {}, el("tr", {}, ["When", "File", "Saved", "Original"].map((t) => el("th", { text: t })), el("th")));
    const rows = entries.map((e) => {
      const [folder, name] = splitPath(e.path);
      let action = null;
      if (e.state === "done") {
        action = el("button", { type: "button", text: "Restore" });
        action.addEventListener("click", async () => {
          if (!confirm(`Put the original of ${name} back? Dubarr will then leave this file alone.`)) return;
          action.disabled = true;
          try {
            const r = await post("/api/restore", { id: e.id });
            flash(result, true, r.message);
            load(false);
          } catch (err) {
            flash(result, false, err.message);
            action.disabled = false;
          }
        });
      }
      return el("tr", { class: "file-row" },
        el("td", { class: "small", style: "white-space:nowrap", text: when(e.changed_at) }),
        el("td", { class: "name" },
          el("div", { class: "title", text: name }),
          el("div", { class: "path mono", text: folder }),
          el("div", { class: "small", text: e.message }),
          e.note ? el("div", { class: "small muted", text: e.note }) : null),
        el("td", { class: "small", style: "white-space:nowrap", text: e.new_size ? bytes(e.original_size - e.new_size) : "–" }),
        el("td", { class: `small state-${e.state}`, text: STATE_LABEL[e.state] || e.state }),
        el("td", { class: "actions" }, action));
    });
    table.replaceChildren(head, el("tbody", {}, rows.length ? rows
      : [el("tr", {}, el("td", { colspan: 5, class: "empty", text: "Nothing changed yet." }))]));
    more.replaceChildren();
    if (entries.length < total) {
      const b = el("button", { type: "button", text: `Show more (${total - entries.length} left)` });
      b.addEventListener("click", () => load(true));
      more.append(el("p", {}, b));
    }
  }

  async function load(append) {
    const r = await api(`/api/history?${new URLSearchParams({ search: search.value.trim(), offset: append ? entries.length : 0 })}`);
    entries = append ? entries.concat(r.entries) : r.entries;
    total = r.total;
    intro.textContent = r.recycle_days
      ? `Every file Dubarr changed. Originals stay in the recycle folder for ${r.recycle_days} days, so you can restore them until then. A restored file is left alone from then on.`
      : "Every file Dubarr changed. The recycle folder is set to 0 days, so originals are deleted as soon as the new file checks out and can't be restored.";
    const d = await api("/api/dashboard");
    run = d.restorable_run;
    restoreBatch.hidden = !run;
    render();
  }

  restoreBatch.addEventListener("click", async () => {
    if (!confirm("Put back the originals of every file changed in the last batch?")) return;
    restoreBatch.disabled = true;
    try {
      const r = await post("/api/restore-run", { run_id: run });
      flash(result, true, r.message);
      setTimeout(() => load(false), 1500);
    } catch (e) {
      flash(result, false, e.message);
    } finally {
      restoreBatch.disabled = false;
    }
  });

  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => load(false), 300); });
  await load(false);
}

// ---------- Logs ----------

async function showLogs() {
  let kind = "all";
  const box = el("div", { class: "log", role: "log", "aria-live": "polite" });
  const search = el("input", { type: "search", placeholder: "Search logs", "aria-label": "Search logs" });
  const auto = el("input", { type: "checkbox", checked: true });
  const buttons = {};
  const segmented = el("div", { class: "segmented", role: "group", "aria-label": "Show" },
    ["all", "issues", "actions"].map((k) => (buttons[k] = el("button", {
      type: "button", class: k === kind ? "active" : "",
      text: { all: "All", issues: "Issues", actions: "Actions" }[k],
      onclick: () => { kind = k; Object.values(buttons).forEach((b) => b.classList.remove("active")); buttons[k].classList.add("active"); load(true); },
    }))));

  view.replaceChildren(
    el("h1", { text: "Logs" }),
    el("p", { class: "muted small", text: "Issues shows errors and warnings. Actions shows every file Dubarr changed or restored." }),
    el("div", { class: "toolbar" }, segmented,
      el("div", { style: "flex:1;min-width:180px" }, search),
      el("label", { class: "chip" }, auto, "Auto refresh"),
      el("a", { class: "button", href: "/api/logs/download", text: "Download all logs" })),
    box);

  let lines = [];
  function render(scroll) {
    const term = search.value.trim().toLowerCase();
    const filtered = lines.filter((l) => !term || l.message.toLowerCase().includes(term));
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.replaceChildren(...(filtered.length ? filtered.map((l) => el("div", { class: "line" },
      el("span", { class: "time", text: l.time }), el("span", { class: `lvl lvl-${l.level}`, text: l.level }), l.message))
      : [el("div", { class: "empty", text: kind === "issues" ? "No issues. All good!" : "Nothing to show yet." })]));
    if (scroll || atBottom) box.scrollTop = box.scrollHeight;
  }

  async function load(scroll) {
    try {
      lines = (await api(`/api/logs?filter=${kind}`)).lines;
      render(scroll);
    } catch (e) {
      box.replaceChildren(el("div", { class: "error-text", text: e.message }));
    }
  }

  search.addEventListener("input", () => render(false));
  await load(true);
  every(5000, () => { if (auto.checked) load(false); });
}

// ---------- Settings ----------

let settings = null;

const codesToNames = (value) => String(value || "").split(",").map((c) => c.trim()).filter(Boolean)
  .map((c) => settings.languages[c] || c).join(", ");

function librariesInput(field, value, onChange, help, error) {
  const rows = el("div");
  const entries = [];

  function addRow(lib = { path: "", audio: "", subtitles: "" }) {
    const path = el("input", { type: "text", value: lib.path, placeholder: "/movies", spellcheck: "false", "aria-label": "Folder", oninput: onChange });
    const audio = el("input", { type: "text", value: codesToNames(lib.audio), placeholder: "Default", "aria-label": "Audio languages", oninput: onChange });
    const subs = el("input", { type: "text", value: codesToNames(lib.subtitles), placeholder: "Default", "aria-label": "Subtitle languages", oninput: onChange });
    const remove = el("button", { type: "button", class: "danger", text: "Remove" });
    const row = el("div", { class: "lib-row" }, path, audio, subs, remove);
    const entry = { row, get: () => ({ path: path.value.trim(), audio: audio.value, subtitles: subs.value }) };
    remove.addEventListener("click", () => { entries.splice(entries.indexOf(entry), 1); row.remove(); onChange(); });
    entries.push(entry);
    rows.append(row);
  }

  (value || []).forEach(addRow);
  if (!entries.length) addRow();
  const add = el("button", { type: "button", text: "Add a library" });
  add.addEventListener("click", () => { addRow(); onChange(); });

  return {
    node: el("div", {},
      el("div", { class: "lib-row lib-head" }, el("span", { text: "Folder inside the container" }),
        el("span", { text: "Audio languages" }), el("span", { text: "Subtitle languages" }), el("span")),
      rows, el("p", {}, add), error),
    get: () => entries.map((e) => e.get()).filter((l) => l.path),
  };
}

function fieldInput(field, value, onChange) {
  const id = `f-${field.key}`;
  const help = field.help ? el("div", { class: "help", text: field.help }) : null;
  const error = el("div", { class: "error-text", id: `${id}-error` });

  if (field.type === "bool") {
    const input = el("input", { type: "checkbox", id, checked: !!value, onchange: onChange });
    return { node: el("label", { class: "switch", for: id }, input, el("span", { class: "track" }),
      el("span", { class: "text" }, field.label, help, error)), get: () => input.checked, input };
  }

  if (field.type === "libraries") return librariesInput(field, value, onChange, help, error);

  if (field.type === "slider") {
    const label = (v) => (!v ? "Off (change every file)" : v >= 1000 ? `${v / 1000} GB` : `${v} MB`);
    const start = Math.max(0, field.options.indexOf(value));
    const shown = el("strong", { text: label(field.options[start]) });
    const input = el("input", { type: "range", id, min: 0, max: field.options.length - 1, step: 1, value: start,
      "aria-valuetext": label(field.options[start]) });
    input.addEventListener("input", () => {
      const v = field.options[Number(input.value)];
      shown.textContent = label(v);
      input.setAttribute("aria-valuetext", label(v));
      onChange();
    });
    return {
      node: el("label", { class: "field", for: id },
        el("span", { class: "label" }, `${field.label}: `, shown), input, help, error),
      get: () => field.options[Number(input.value)],
      input,
    };
  }

  let input;
  if (field.type === "select") {
    input = el("select", { id, onchange: onChange }, field.options.map((o) => el("option", { value: o, text: o, selected: o === value })));
  } else if (field.type === "secret") {
    input = el("input", { type: "password", id, autocomplete: "new-password", oninput: onChange,
      placeholder: value && value.set ? "•••••••• saved (type to replace)" : "Not set" });
  } else if (field.type === "languages") {
    input = el("input", { type: "text", id, value: codesToNames(value), oninput: onChange, placeholder: "e.g. English, Japanese" });
  } else {
    input = el("input", { type: field.type === "int" ? "number" : "text", id, value: value ?? "", oninput: onChange,
      min: field.min, max: field.max, placeholder: field.placeholder, spellcheck: "false" });
  }
  return {
    node: el("label", { class: "field", for: id }, el("span", { class: "label", text: field.label }), input, help, error),
    get: () => field.type === "secret" ? (input.value === "" ? null : input.value) : input.value,
    input,
  };
}

function webhookPanel(tab) {
  const name = tab === "radarr" ? "Radarr" : "Sonarr";
  const url = el("input", { type: "text", readonly: true, value: location.origin + settings.webhook_path, "aria-label": "Webhook address" });
  const copy = el("button", { type: "button", text: "Copy" });
  copy.addEventListener("click", async () => {
    url.select();
    try { await navigator.clipboard.writeText(url.value); copy.textContent = "Copied"; } catch { document.execCommand("copy"); copy.textContent = "Copied"; }
    setTimeout(() => (copy.textContent = "Copy"), 2000);
  });
  return el("div", { class: "panel" },
    el("h2", { text: "Check new downloads straight away (optional)" }),
    el("p", { class: "muted small" },
      `In ${name}, go to Settings > Connect, add a Webhook, tick On Import and On Upgrade, `,
      "set the method to POST, and paste this address. Use the address ", name,
      " can reach Dubarr on (on Unraid, your server's IP). Without it, new files are picked up at the next scheduled scan."),
    el("div", { class: "copy-row" }, url, copy),
    el("p", { class: "help", text: "The address contains a secret. Keep it private, like an API key." }));
}

// The section on each tab that gets a Test connection button, and what it tests
const TESTABLE = {
  radarr: { Connection: "radarr" }, sonarr: { Connection: "sonarr" }, bazarr: { Connection: "bazarr" },
  jellyfin: { Connection: "jellyfin" }, detection: { "Audio: Whisper": "whisper", Subtitles: "ollama" },
};

const TAB_INTRO = {
  libraries: "The folders Dubarr checks, as they're mapped inside the Dubarr container. Leave the languages empty to use the ones on the General tab, or set them for one library, e.g. Japanese, English audio for anime. Separate languages with commas.",
  radarr: "Optional. After changing a film, Dubarr asks Radarr to rescan it, so Radarr shows the new tracks and size.",
  sonarr: "Optional. After changing an episode, Dubarr asks Sonarr to rescan the series, so Sonarr shows the new tracks and size.",
  bazarr: "Optional. After changing a film, Dubarr asks Bazarr to look at it again straight away, and to search for subtitles if the change left none in your languages. Test connection also checks Bazarr's language profiles: a language Bazarr downloads but Dubarr removes would keep coming back.",
  jellyfin: "Optional. After changing a file, Dubarr tells Jellyfin, so the audio and subtitle menus update without a full library scan.",
  detection: "Optional. Adds a Detect button to tracks under Listen & tag, which suggests their language. A suggestion is never saved until you choose it and press Save tags. For Whisper, run the whisper-asr-webservice container (onerahmet/openai-whisper-asr-webservice) with ASR_MODEL=small on a CPU, base on a low-power box, or medium or large-v3 with a GPU. Never a .en model: those only know English, so they can't tell languages apart. Ollama's Test connection checks the model is pulled.",
};

async function showSettings(tab, savedMessage) {
  if (!settings) settings = await api("/api/settings");
  languages = settings.languages;
  if (!settings.tabs[tab]) tab = "general";

  const fields = settings.fields.filter((f) => f.tab === tab);
  const getters = {};
  const status = el("span", { class: "muted small", role: "status" });
  let dirty = false;
  const markDirty = () => { dirty = true; status.textContent = "Unsaved changes"; status.style.color = "var(--warn)"; };

  const formValues = () => Object.fromEntries(Object.entries(getters).map(([k, g]) => [k, g()]));

  function testButton(target) {
    const result = el("span", { class: "result" });
    const button = el("button", { type: "button", text: "Test connection" });
    button.addEventListener("click", async () => {
      button.disabled = true;
      result.className = "result muted";
      result.textContent = "Testing…";
      try {
        const r = await post("/api/test", { target, values: formValues() });
        flash(result, r.ok, r.message);
      } catch (e) {
        flash(result, false, e.message);
      } finally {
        button.disabled = false;
      }
    });
    return el("div", { class: "card-actions" }, button, result);
  }

  const sections = [];
  for (const field of fields) {
    let section = sections.find((s) => s.title === field.section);
    if (!section) sections.push(section = { title: field.section, fields: [] });
    section.fields.push(field);
  }

  const content = sections.map((section) => {
    const rendered = section.fields.map((f) => {
      const input = fieldInput(f, settings.values[f.key], markDirty);
      getters[f.key] = input.get;
      return input;
    });
    const wide = section.fields.some((f) => f.type === "libraries");
    return el("div", { class: "panel" },
      tab === "libraries" ? null : el("h2", { text: section.title }),
      el("div", { class: wide ? "" : "grid-2" }, rendered.map((r) => r.node)),
      TESTABLE[tab] && TESTABLE[tab][section.title] ? testButton(TESTABLE[tab][section.title]) : null);
  });

  if (TAB_INTRO[tab]) content.unshift(el("p", { class: "muted", text: TAB_INTRO[tab] }));
  if (tab === "radarr" || tab === "sonarr") content.push(webhookPanel(tab));
  if (tab === "general") content.push(...accountPanels());

  const saveButton = el("button", { class: "primary", type: "button", text: `Save ${settings.tabs[tab]}` });
  saveButton.addEventListener("click", async () => {
    saveButton.disabled = true;
    view.querySelectorAll(".error-text[id$='-error']").forEach((e) => (e.textContent = ""));
    try {
      const r = await api("/api/settings", { method: "PUT", body: JSON.stringify({ values: formValues() }) });
      settings.values = r.values;
      dirty = false;
      status.style.color = "var(--ok)";
      status.textContent = "✓ Saved. Changes are in effect now.";
      if (tab === "libraries") await showSettings(tab, status.textContent);
      refreshHealth();
    } catch (e) {
      status.style.color = "var(--bad)";
      const errors = (e.data && e.data.errors) || {};
      for (const [key, message] of Object.entries(errors)) {
        const box = document.getElementById(`f-${key}-error`);
        if (box) box.textContent = message;
      }
      status.textContent = Object.keys(errors).length ? "Please fix the highlighted settings." : e.message;
    } finally {
      saveButton.disabled = false;
    }
  });

  view.replaceChildren(
    el("h1", { text: "Settings" }),
    el("div", { class: "tabs", role: "tablist" }, Object.entries(settings.tabs).map(([key, label]) =>
      el("button", { type: "button", role: "tab", class: key === tab ? "active" : "", "aria-selected": String(key === tab), text: label,
        onclick: () => {
          if (dirty && !confirm("You have unsaved changes on this tab. Leave without saving?")) return;
          location.hash = `#/settings/${key}`;
        } }))),
    ...content,
    el("div", { class: "savebar" }, saveButton, status));

  if (savedMessage) {
    status.style.color = "var(--ok)";
    status.textContent = savedMessage;
  }
  window.onbeforeunload = () => (dirty ? true : undefined);
}

function accountPanels() {
  const current = el("input", { type: "password", autocomplete: "current-password" });
  const user = el("input", { type: "text", value: settings.username, autocomplete: "username" });
  const password = el("input", { type: "password", autocomplete: "new-password" });
  const confirmBox = el("input", { type: "password", autocomplete: "new-password" });
  const rules = el("ul", { class: "rules" });
  const result = el("div", { class: "result", role: "status" });
  const RULES = [
    ["At least 8 characters", (p) => p.length >= 8],
    ["An uppercase letter", (p) => /[A-Z]/.test(p)],
    ["A lowercase letter", (p) => /[a-z]/.test(p)],
    ["A number", (p) => /[0-9]/.test(p)],
    ["A special character, like ! ? # or @", (p) => /[^A-Za-z0-9]/.test(p)],
  ];
  const renderRules = () => rules.replaceChildren(...RULES.map(([t, c]) => el("li", { class: c(password.value) ? "met" : "", text: t })));
  password.addEventListener("input", renderRules);
  renderRules();

  const change = el("button", { type: "button", text: "Change password" });
  change.addEventListener("click", async () => {
    result.className = "result bad";
    const missing = RULES.filter(([, c]) => !c(password.value));
    if (missing.length) { result.textContent = "The new password still needs: " + missing.map(([t]) => t.toLowerCase()).join(", "); return; }
    if (password.value !== confirmBox.value) { result.textContent = "The new passwords don't match"; return; }
    change.disabled = true;
    try {
      await post("/api/password", { current: current.value, username: user.value.trim(), password: password.value });
      result.className = "result ok";
      result.textContent = "✓ Password changed. Other devices have been logged out.";
      settings.username = user.value.trim();
      current.value = password.value = confirmBox.value = "";
      renderRules();
    } catch (e) {
      result.textContent = e.message;
    } finally {
      change.disabled = false;
    }
  });

  return [
    el("div", { class: "panel" },
      el("h2", { text: "Account" }),
      el("div", { class: "notice warn small" },
        el("strong", { text: "Keep Dubarr on your home network. " }),
        "We don't recommend putting it behind a reverse proxy (Nginx Proxy Manager, SWAG, Traefik, Cloudflare Tunnel and the like) for access from outside. Use a VPN such as WireGuard or Tailscale instead. The local network login bypass above never applies to requests that come through a proxy."),
      el("div", { class: "grid-2" },
        el("label", { class: "field" }, el("span", { class: "label", text: "Username" }), user),
        el("label", { class: "field" }, el("span", { class: "label", text: "Current password" }), current),
        el("label", { class: "field" }, el("span", { class: "label", text: "New password" }), password, rules),
        el("label", { class: "field" }, el("span", { class: "label", text: "Confirm new password" }), confirmBox)),
      el("div", { class: "card-actions" }, change, result)),
    el("div", { class: "panel" },
      el("h2", { text: "Log files" }),
      el("p", { class: "muted small", text: "Everything Dubarr has logged, including older rotated files, as one zip. Handy for reporting a problem." }),
      el("a", { class: "button", href: "/api/logs/download", text: "Download all logs" })),
  ];
}

// ---------- Contact ----------

function showContact() {
  view.replaceChildren(
    el("h1", { text: "Contact" }),
    el("div", { class: "panel" },
      el("h2", { text: "Report a problem or suggest an idea" }),
      el("p", { class: "muted", text: "Bugs and ideas go on GitHub, where you can follow the fix and others with the same problem can find it. Saying which versions of Radarr, Sonarr and Unraid you run, and what you expected to happen, helps a lot. Logs are under Settings > General > Log files." }),
      el("div", { class: "row-wrap" },
        el("a", { class: "button primary", href: `${REPO}/issues/new?title=%5BDubarr%5D%20`, target: "_blank", rel: "noopener", text: "Report an issue" }),
        el("a", { class: "button", href: `${REPO}/issues`, target: "_blank", rel: "noopener", text: "See open issues" }))),
    el("div", { class: "panel" },
      el("h2", { text: "Contact me" }),
      el("p", { class: "muted" }, "For anything that doesn't belong on a public page, email ",
        el("a", { href: `mailto:${EMAIL}?subject=Dubarr`, text: EMAIL }), ".")));
}

// ---------- Router ----------

async function route() {
  window.onbeforeunload = null;
  clearInterval(refreshTimer);
  const [, page = "", sub = ""] = (location.hash || "#/").split("/");
  const name = page || "dashboard";
  document.querySelectorAll("nav.main a").forEach((a) => a.classList.toggle("active", a.dataset.page === name));
  try {
    if (name === "history") await showHistory();
    else if (name === "logs") await showLogs();
    else if (name === "settings") await showSettings(sub || "general");
    else if (name === "contact") showContact();
    else await showDashboard(sub);
  } catch (e) {
    view.replaceChildren(el("div", { class: "notice bad", text: e.message }));
  }
}

window.addEventListener("hashchange", route);
route();
refreshHealth();
setInterval(refreshHealth, 15000);
