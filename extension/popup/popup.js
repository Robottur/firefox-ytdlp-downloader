/**
 * Popup: everything the user sees for one tab.
 *
 * The popup is a thin client. It never decides anything about a download by
 * itself -- it renders what the native host reported, collects a choice, and
 * sends one `download_start` frame. Queue updates arrive as `job` (full record)
 * and `progress` (throttled counters) events pushed by the background script.
 */

const ui = {
  hostPill: document.getElementById("host-pill"),
  hostText: document.getElementById("host-text"),
  banner: document.getElementById("banner"),
  url: document.getElementById("url-input"),
  probeButton: document.getElementById("probe-button"),
  status: document.getElementById("status-line"),
  media: document.getElementById("media"),
  thumb: document.getElementById("thumb"),
  title: document.getElementById("title"),
  meta: document.getElementById("meta"),
  playlist: document.getElementById("playlist"),
  playlistInfo: document.getElementById("playlist-info"),
  entryPicker: document.getElementById("entry-picker"),
  entryList: document.getElementById("entry-list"),
  entryCount: document.getElementById("entry-count"),
  options: document.getElementById("options"),
  format: document.getElementById("format-select"),
  audioOnly: document.getElementById("audio-only"),
  subtitleDetails: document.getElementById("subtitle-details"),
  subtitleList: document.getElementById("subtitle-list"),
  subtitleCount: document.getElementById("subtitle-count"),
  outputDir: document.getElementById("output-dir"),
  outputFilename: document.getElementById("output-filename"),
  filenameField: document.getElementById("filename-field"),
  pickFolder: document.getElementById("pick-folder"),
  actions: document.getElementById("actions"),
  download: document.getElementById("download-button"),
  queue: document.getElementById("queue"),
  queueEmpty: document.getElementById("queue-empty"),
  clearFinished: document.getElementById("clear-finished"),
  openOptions: document.getElementById("open-options"),
};

const DEFAULT_DIR = "";

/** Job states that are finished from the user's point of view. */
const TERMINAL_STATUSES = new Set(["complete", "error", "cancelled", "interrupted"]);

const state = {
  sourceContext: null,
  sessionToken: null,
  sessionUrl: null,
  retryJobId: null,
  enablingSession: false,
  connected: false,
  host: null,
  settings: null,
  jobs: [],
  collapsed: new Set(),
  probe: null,
  probeStatus: "idle", // idle | loading | ready | error
  probeError: "",
  probeToken: 0, // guards against a slow probe overwriting a newer one
  entries: null,
  entriesStatus: "idle",
  entriesError: "",
  nextOffset: null,
  sample: null,
  selected: new Set(),
  playlistMode: "single",
  formatId: "",
  audioOnly: false,
  subtitles: new Set(), // "lang:manual" | "lang:auto"
  outputDir: DEFAULT_DIR,
  outputFilename: "",
  overwrite: false,
  choosingDestination: false,
  busy: false,
};

/* ------------------------------------------------------------------ utils */

function ask(message) {
  return browser.runtime.sendMessage(message);
}

/** The background script normalises handler failures into `__error`. */
async function askChecked(message) {
  const reply = await ask(message);
  if (reply && reply.__error) {
    throw new Error(reply.__error.message);
  }
  return reply;
}

function formatBytes(bytes) {
  if (!bytes || bytes < 0) {
    return "";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value >= 100 || index === 0 ? Math.round(value) : value.toFixed(1)} ${units[index]}`;
}

function formatSpeed(bytesPerSecond) {
  return bytesPerSecond > 0 ? `${formatBytes(bytesPerSecond)}/s` : "";
}

function formatEta(seconds) {
  if (seconds === null || seconds === undefined || seconds < 0) {
    return "";
  }
  const total = Math.round(seconds);
  if (total < 60) {
    return `${total}s left`;
  }
  const minutes = Math.floor(total / 60);
  if (minutes < 60) {
    return `${minutes}m ${total % 60}s left`;
  }
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m left`;
}

function basename(path) {
  return (path || "").split(/[\\/]/).pop();
}

function setStatus(text, isError = false) {
  ui.status.textContent = text;
  ui.status.classList.toggle("status-line--error", Boolean(isError));
}

/**
 * Build a banner out of text nodes and <code> elements.
 *
 * No innerHTML anywhere in this popup: everything shown here can originate
 * from a remote site (extractor error messages, video titles), and the linter
 * is right to be suspicious of string-built HTML.
 */
function showBanner(parts, kind = "warn") {
  ui.banner.className = `banner banner--${kind}`;
  ui.banner.replaceChildren();
  for (const part of Array.isArray(parts) ? parts : [parts]) {
    if (typeof part === "string") {
      ui.banner.append(document.createTextNode(part));
    } else if (part?.code) {
      const code = document.createElement("code");
      code.textContent = part.code;
      ui.banner.append(code);
    }
  }
  ui.banner.hidden = false;
}

function clearBanner() {
  ui.banner.hidden = true;
  ui.banner.replaceChildren();
}

/* ------------------------------------------------------------- host state */

function renderHost() {
  const pill = ui.hostPill;
  pill.className = "pill";
  if (!state.connected) {
    pill.classList.add("pill--error");
    ui.hostText.textContent = "Host unavailable";
    return;
  }
  const host = state.host;
  if (!host) {
    pill.classList.add("pill--pending");
    ui.hostText.textContent = "Connecting…";
    return;
  }
  pill.classList.add("pill--ok");
  ui.hostText.textContent = `yt-dlp ${host.ytDlpVersion} · host ${host.hostVersion}`;
  if (host.ffmpegFound === false) {
    pill.classList.remove("pill--ok");
    pill.classList.add("pill--warn");
  }
  pill.title = [
    `yt-dlp ${host.ytDlpVersion}`,
    `Python ${host.pythonVersion}`,
    host.ffmpegFound ? `ffmpeg: ${host.ffmpegVersion}` : "ffmpeg: not found",
    `state: ${host.stateDir}`,
  ].join("\n");
}

function renderBanners() {
  clearBanner();
  if (!state.connected) {
    const error = state.hostError || {};
    let hint;
    if (error.code === "not_installed") {
      hint = ["Run install_linux.sh on Linux or install_windows.ps1 on Windows, then reopen this popup."];
    } else if (error.code === "not_allowed") {
      hint = ["The native messaging manifest must list ", { code: "ytdlp-downloader@local" }, "."];
    } else {
      hint = [error.message || "The background script is retrying automatically."];
    }
    showBanner(["yt-dlp host not connected. ", ...hint], "error");
    return;
  }
  if (state.host && state.host.ffmpegFound === false) {
    showBanner(
      [
        "ffmpeg was not found, so audio-only extraction and merging separate video+audio streams are unavailable. ",
        { code: "sudo pacman -S ffmpeg" },
        " (Arch) or ",
        { code: "sudo apt install ffmpeg" },
        " (Debian), or ", { code: "winget install Gyan.FFmpeg" }, " (Windows).",
      ],
      "warn",
    );
  } else if (state.host?.jsRuntimes?.length === 0) {
    showBanner("YouTube needs Deno or Node.js. Install Deno, then restart the companion host. See Settings for setup details.");
  }
}

/* ---------------------------------------------------------------- probing */

async function probe(url, refresh = false) {
  if (state.sessionUrl && state.sessionUrl !== url) releaseSession(true);
  if (!/^https?:\/\//i.test(url)) {
    setStatus("Enter an http:// or https:// address.", true);
    return;
  }
  // A slow probe for a previous URL must not be allowed to overwrite a newer
  // one, so every run carries a token and only the newest one may commit.
  const token = ++state.probeToken;
  const mine = () => token === state.probeToken;

  state.probe = null;
  state.probeStatus = "loading";
  state.entries = null;
  state.entriesStatus = "idle";
  state.nextOffset = null;
  state.sample = null;
  state.selected.clear();
  state.playlistMode = "single";
  state.audioOnly = false;
  state.subtitles.clear();
  state.outputFilename = "";
  state.overwrite = false;
  render();
  setStatus("Reading metadata with yt-dlp…");
  let result;
  try {
    result = await askChecked({ kind: "ui.probe", url, refresh, sessionToken: state.sessionToken });
  } catch (error) {
    if (!mine()) {
      return;
    }
    state.probeStatus = "error";
    state.probeError = error.message || String(error);
    setStatus(state.probeError, true);
    render();
    return;
  }
  if (!mine()) {
    return;
  }
  state.probe = result;
  state.probeStatus = "ready";
  state.formatId = pickDefaultFormat(result);
  if (result.isPlaylist) {
    state.playlistMode = "all";
  }
  setStatus("");
  render();
  if (result.isPlaylist) loadPlaylistSample(token);
}

async function loadPlaylistSample(token) {
  const first = state.probe?.entries?.find((entry) => entry.url);
  // A watch URL containing a playlist still refers to the current video.
  const original = state.probe.url;
  const target = /[?&]v=/.test(original) ? original : first?.url;
  if (!target) return;
  try {
    const sample = await askChecked({ kind: "ui.probe", url: target, single: true, sessionToken: state.sessionToken });
    if (token !== state.probeToken || sample.isPlaylist) return;
    state.sample = sample;
    renderOptions();
  } catch {
    // Playlist download remains available if its first video is unavailable.
  }
}

/** The host resolves the MP4 preset for the available tools and formats. */
function pickDefaultFormat() {
  const preferred = state.settings?.defaultFormat || "mp4";
  return state.host?.ffmpegFound === false && preferred.includes("+") ? "mp4" : preferred;
}

async function loadEntries(more = false) {
  if ((!more && state.entries) || state.entriesStatus === "loading") {
    return;
  }
  // Bind the request to the URL it was asked for: a new probe resets the
  // entry list, and a late reply would otherwise fill the picker with the
  // previous playlist's entries -- which are then submitted as entry indexes
  // for a completely different video.
  const url = state.probe?.url;
  const token = state.probeToken;
  state.entriesStatus = "loading";
  render();
  try {
    const result = await askChecked({ kind: "ui.probeEntries", url, offset: more ? state.nextOffset : 0, sessionToken: state.sessionToken });
    if (token !== state.probeToken || url !== state.probe?.url) {
      return;
    }
    state.entries = [...(more ? state.entries : []), ...(result.entries || [])];
    state.nextOffset = result.nextOffset ?? null;
    state.entriesStatus = "ready";
    for (const entry of result.entries || []) {
      state.selected.add(entry.index);
    }
  } catch (error) {
    if (token !== state.probeToken || url !== state.probe?.url) {
      return;
    }
    state.entriesStatus = "error";
    state.entriesError = error.message || String(error);
  }
  render();
}

/* --------------------------------------------------------------- rendering */

function render() {
  renderHost();
  renderBanners();
  renderMedia();
  renderPlaylist();
  renderOptions();
  renderActions();
  renderQueue();
}

function renderMedia() {
  const probe = state.probe;
  const show = state.probeStatus === "ready" && probe;
  ui.media.hidden = !show;
  if (!show) {
    return;
  }
  ui.title.textContent = probe.title || probe.url;
  const bits = [];
  if (probe.durationString) {
    bits.push(probe.durationString);
  }
  if (probe.uploader) {
    bits.push(probe.uploader);
  }
  if (probe.extractor) {
    bits.push(probe.extractor);
  }
  if (probe.isLive) {
    bits.push("live");
  }
  ui.meta.textContent = bits.join(" · ");
  // Remote image loads would bypass the companion's download request policy.
  ui.thumb.removeAttribute("src");
  ui.thumb.hidden = true;
}

function renderPlaylist() {
  const probe = state.probe;
  const show = state.probeStatus === "ready" && probe?.isPlaylist;
  ui.playlist.hidden = !show;
  if (!show) {
    return;
  }
  const count = probe.playlistCount || state.entries?.length || 0;
  const approximate = probe.playlistCountIsExact === false ? " (at least)" : "";
  ui.playlistInfo.textContent = count
    ? `Part of a playlist — ${count} video${count === 1 ? "" : "s"}${approximate}.`
    : "This URL is a playlist.";
  for (const button of document.querySelectorAll(".seg")) {
    button.setAttribute("aria-pressed", String(button.dataset.mode === state.playlistMode));
  }

  const picking = state.playlistMode === "select";
  document.getElementById("load-more-entries").hidden = state.nextOffset === null || state.entriesStatus === "loading";
  ui.entryPicker.hidden = !picking;
  if (!picking) {
    return;
  }
  if (state.entriesStatus === "loading") {
    ui.entryList.replaceChildren(placeholderItem("Reading playlist entries…"));
    ui.entryCount.textContent = "";
    return;
  }
  if (state.entriesStatus === "error") {
    ui.entryList.replaceChildren(placeholderItem(state.entriesError, true));
    ui.entryCount.textContent = "";
    return;
  }
  ui.entryList.replaceChildren();
  for (const entry of state.entries || []) {
    const item = document.createElement("li");
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = state.selected.has(entry.index);
    box.dataset.index = String(entry.index);
    box.addEventListener("change", () => {
      if (box.checked) {
        state.selected.add(entry.index);
      } else {
        state.selected.delete(entry.index);
      }
      renderSelection();
    });
    const index = document.createElement("span");
    index.className = "entry-index";
    index.textContent = String(entry.index + 1);
    const name = document.createElement("span");
    name.className = "entry-name";
    name.textContent = entry.title || entry.id || `Entry ${entry.index + 1}`;
    const dur = document.createElement("span");
    dur.className = "entry-dur";
    dur.textContent = entry.durationString || "";
    label.append(box, index, name, dur);
    item.append(label);
    ui.entryList.append(item);
  }
  ui.entryCount.textContent = `${state.selected.size} of ${(state.entries || []).length} selected`;
}

/**
 * Re-render everything that depends on which playlist entries are ticked.
 *
 * The picker and the download button both read ``state.selected``; updating one
 * without the other leaves a button that promises the wrong number of videos.
 */
function renderSelection() {
  ui.entryCount.textContent = `${state.selected.size} of ${(state.entries || []).length} selected`;
  for (const box of ui.entryList.querySelectorAll("input[type=checkbox]")) {
    box.checked = state.selected.has(Number(box.dataset.index));
  }
  renderActions();
}

function renderOptions() {
  const probe = state.probe;
  // A playlist URL is not itself downloadable, so the format list describes
  // what its *entries* look like -- and the choice applies to every entry.
  const show = state.probeStatus === "ready" && !!probe;
  ui.options.hidden = !show;
  if (!show) {
    return;
  }

  // Formats -----------------------------------------------------------------
  const audioOnly = state.audioOnly;
  const ffmpegAvailable = state.host?.ffmpegFound !== false;
  const sample = probe.isPlaylist ? state.sample : probe;
  ui.format.replaceChildren();
  const options = [];
  const defaultFormat = audioOnly ? "bestaudio/best" : pickDefaultFormat(probe);
  options.push({formatId: defaultFormat, label: audioOnly ? "Best audio → MP3" :
    defaultFormat === "mp4" ? "MP4 · best available video + audio" : "Automatic · saved quality preference"});
  if (probe.isPlaylist && state.playlistMode !== "single") {
    // Format IDs belong to individual videos. Derive portable height limits
    // from the representative entry instead of applying its IDs to siblings.
    const heights = [...new Set((sample?.formats || []).map((f) => f.height).filter(Boolean))];
    if (!audioOnly) for (const height of heights) {
      const selector = ffmpegAvailable ? `bv*[height<=${height}]+ba/b[height<=${height}]` : `b[height<=${height}]`;
      options.push({formatId: selector, label: `Up to ${height}p · each playlist video`});
    }
  } else {
    for (const f of sample?.formats || []) {
      if (audioOnly && !f.isAudio) continue;
      const merge = !audioOnly && f.kind === "video only";
      if (merge && !ffmpegAvailable) continue;
      options.push({...f, formatId: merge ? `${f.formatId}+bestaudio/best` : f.formatId,
        label: merge ? f.label.replace("video only", "video + best audio") : f.label});
    }
  }
  for (const format of options) {
    const option = document.createElement("option");
    option.value = format.formatId;
    option.textContent = format.label;
    ui.format.append(option);
  }
  if (!options.length) {
    const option = document.createElement("option");
    option.value = "best";
    option.textContent = "Best available";
    ui.format.append(option);
  }
  // Toggling audio-only changes which formats are listed, so the previous
  // choice may no longer exist. Falling back to whatever is now first keeps
  // state and the visible dropdown in agreement -- otherwise the download
  // would silently use a format the user cannot see.
  if ([...ui.format.options].some((o) => o.value === state.formatId)) {
    ui.format.value = state.formatId;
  } else {
    state.formatId = ui.format.value;
  }
  ui.audioOnly.checked = audioOnly;

  // Subtitles ---------------------------------------------------------------
  const subtitles = sample?.subtitles || [];
  ui.subtitleDetails.hidden = subtitles.length === 0;
  ui.subtitleCount.textContent = String(state.subtitles.size);
  ui.subtitleList.replaceChildren();
  for (const track of subtitles) {
    for (const kind of ["manual", "auto"]) {
      if (!track[kind]) {
        continue;
      }
      const variant = track.variants?.find((v) => v.manual === (kind === "manual"));
      const language = probe.isPlaylist && state.playlistMode !== "single" ? track.base || track.lang : variant?.lang || track.lang;
      const key = `${language}:${kind}`;
      const label = document.createElement("label");
      label.className = "checkbox";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = state.subtitles.has(key);
      box.addEventListener("change", () => {
        if (box.checked) {
          // One source per language; yt-dlp saves one subtitle file per language.
          for (const other of track.variants || [{lang: track.lang}]) {
            state.subtitles.delete(`${other.lang}:manual`);
            state.subtitles.delete(`${other.lang}:auto`);
          }
          state.subtitles.delete(`${track.base || track.lang}:manual`);
          state.subtitles.delete(`${track.base || track.lang}:auto`);
          state.subtitles.add(key);
        } else {
          state.subtitles.delete(key);
        }
        ui.subtitleCount.textContent = String(state.subtitles.size);
        renderOptions();
      });
      const text = document.createElement("span");
      text.textContent = track.name;
      const tag = document.createElement("span");
      tag.className = "sub-tag";
      tag.textContent = kind === "manual" ? "official" : "auto";
      label.append(box, text, tag);
      ui.subtitleList.append(label);
    }
  }

  // Destination -------------------------------------------------------------
  ui.outputDir.value = state.outputDir || state.settings?.defaultDownloadDir || "";
  const multiple = probe.isPlaylist && state.playlistMode !== "single";
  ui.filenameField.hidden = multiple;
  ui.outputFilename.value = state.outputFilename;
  ui.pickFolder.textContent = multiple ? "Choose folder…" : "Save As…";
  ui.pickFolder.title = multiple ? "Choose a folder for all playlist files" : "Choose where to save and name your file";
  ui.pickFolder.disabled = state.choosingDestination;
  ui.outputDir.disabled = state.choosingDestination;
  ui.outputFilename.disabled = state.choosingDestination;
  ui.format.disabled = state.choosingDestination;
  ui.probeButton.disabled = state.choosingDestination;
  ui.url.disabled = state.choosingDestination;
  for (const button of document.querySelectorAll(".seg")) button.disabled = state.choosingDestination;
  const ffmpeg = state.host?.ffmpegFound !== false;
  ui.audioOnly.disabled = !ffmpeg || state.choosingDestination;
  if (!ffmpeg && state.audioOnly) {
    state.audioOnly = false;
    ui.audioOnly.checked = false;
  }
}

/** The one place that decides whether Download is clickable, and its label. */
function renderActions() {
  const show = state.probeStatus === "ready" && !!state.probe;
  ui.actions.hidden = !show;
  if (!show) {
    return;
  }
  if (state.busy) {
    ui.download.disabled = true;
    ui.download.textContent = "Starting…";
    return;
  }
  const selecting = state.playlistMode === "select";
  if (selecting && !state.selected.size) {
    ui.download.disabled = true;
    ui.download.textContent = "Pick at least one video";
    return;
  }
  ui.download.disabled = !state.connected || state.choosingDestination;
  const isPlaylist = state.probe.isPlaylist && state.playlistMode !== "single";
  if (!isPlaylist) {
    ui.download.textContent = "Download";
    return;
  }
  const count = selecting ? state.selected.size : state.probe.playlistCount;
  ui.download.textContent = selecting
    ? `Download ${count} selected`
    : `Download playlist (${count ?? "all"})`;
}

const queueRows = new Map();
function renderQueue() {
  const jobs = visibleJobs();
  ui.queueEmpty.hidden = jobs.length > 0;
  ui.clearFinished.hidden = !jobs.some((job) => TERMINAL_STATUSES.has(job.status));
  const keep = new Set(jobs.map((job) => job.jobId));
  for (const [id, cached] of queueRows) {
    if (!keep.has(id)) { cached.node.remove(); queueRows.delete(id); }
  }
  let cursor = ui.queue.firstChild;
  for (const job of jobs) {
    const key = JSON.stringify(job) + state.collapsed.has(job.jobId);
    let cached = queueRows.get(job.jobId);
    if (!cached || cached.key !== key) {
      const node = renderJob(job);
      if (cached) {
        if (cursor === cached.node) cursor = node;
        cached.node.replaceWith(node);
      }
      cached = {key, node};
      queueRows.set(job.jobId, cached);
    }
    if (cached.node !== cursor) ui.queue.insertBefore(cached.node, cursor);
    cursor = cached.node.nextSibling;
  }
}

/**
 * Group jobs with their children, hiding the children of a collapsed group.
 *
 * The mirror keeps every job; collapsing only affects what is drawn, so
 * expanding again works without asking the background for a fresh list. This
 * is also used to re-order after a status change, so it must be lossless: a
 * child whose group record has not arrived yet is kept, not dropped.
 */
function visibleJobs() {
  const children = new Map();
  const roots = [];
  for (const job of state.jobs) {
    if (!job.groupId) {
      roots.push(job);
    } else {
      children.set(job.groupId, [...(children.get(job.groupId) || []), job]);
    }
  }
  roots.sort((a, b) => rankJob(a) - rankJob(b) || (b.createdAt || 0) - (a.createdAt || 0));
  const rootIds = new Set(roots.map((job) => job.jobId));
  const out = [];
  for (const root of roots) {
    out.push(root);
    if (state.collapsed.has(root.jobId)) {
      continue;
    }
    out.push(
      ...(children.get(root.jobId) || []).sort((a, b) => (a.entryIndex ?? 0) - (b.entryIndex ?? 0)),
    );
  }
  for (const [groupId, kids] of children) {
    if (!rootIds.has(groupId)) {
      out.push(...kids);
    }
  }
  return out;
}

/** Running work first, then failures and cancellations, then successes. */
function rankJob(job) {
  if (job.status === "complete") {
    return 3;
  }
  if (job.status === "error" || job.status === "cancelled") {
    return 2;
  }
  return 1;
}

/** Keep the queue in the same order the background script hands it out in. */
function sortJobs() {
  state.jobs.sort((a, b) => rankJob(a) - rankJob(b) || (b.createdAt || 0) - (a.createdAt || 0));
}

function renderJob(job) {
  const item = document.createElement("li");
  const isGroup = job.isGroup;
  const status = job.status || "queued";
  item.className = [
    "job",
    `job--${status}`,
    job.groupId ? "job--child" : "",
    status === "downloading" || status === "queued" || status === "preparing"
      ? "job--active"
      : "",
    status === "complete" ? "job--done" : "",
  ]
    .filter(Boolean)
    .join(" ");

  const head = document.createElement("div");
  head.className = "job__head";

  if (isGroup) {
    const disclose = document.createElement("button");
    disclose.className = "job__disclose";
    disclose.type = "button";
    disclose.textContent = state.collapsed.has(job.jobId) ? "▶" : "▼";
    disclose.title = "Show or hide the videos in this playlist";
    disclose.addEventListener("click", async () => {
      const reply = await ask({ kind: "ui.toggleGroup", jobId: job.jobId });
      state.collapsed = new Set(reply?.collapsed || []);
      renderQueue();
    });
    head.append(disclose);
  }

  const title = document.createElement("span");
  title.className = "job__title";
  title.textContent = job.title || job.url;
  title.title = job.title || job.url;
  head.append(title);

  const actions = document.createElement("div");
  actions.className = "job__actions";

  const active = ["queued", "preparing", "downloading"].includes(status);
  if (active && !isGroup) {
    actions.append(jobButton("✕", "Cancel", () => askChecked({ kind: "ui.cancel", jobId: job.jobId })));
  } else if (isGroup && active) {
    actions.append(
      jobButton("✕", "Cancel the whole playlist", () =>
        askChecked({ kind: "ui.cancel", jobId: job.jobId }),
      ),
    );
  }
  if (status === "complete" && job.filename) {
    actions.append(
      jobButton("📂", "Show in the file manager", () =>
        askChecked({ kind: "ui.openPath", path: job.filename }),
      ),
    );
  }
  if (status === "error" || status === "cancelled") {
    // A child is always in a group, and the group's own retry covers the lot.
    actions.append(jobButton("↻", "Try again", () => retryJob(job)));
  }
  head.append(actions);
  item.append(head);

  // Progress bar -----------------------------------------------------------
  const bar = document.createElement("div");
  const percent = Number(job.percent) || 0;
  const indeterminate = active && !job.totalBytes;
  bar.className = `bar${indeterminate ? " bar--indeterminate" : ""}${
    status === "complete" ? " bar--done" : status === "error" ? " bar--error" : ""
  }`;
  const fill = document.createElement("div");
  fill.className = "bar__fill";
  fill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
  bar.append(fill);
  item.append(bar);

  // Footer -----------------------------------------------------------------
  const foot = document.createElement("div");
  foot.className = "job__foot";
  const phase = document.createElement("span");
  phase.className = "job__phase";
  const details = [];
  if (job.phase) {
    details.push(job.phase);
  }
  if (job.recoveryAvailable === false) details.push("Session only: this URL is not saved for restart recovery");
  if (status === "downloading" && !indeterminate) {
    details.push(`${percent.toFixed(0)}%`);
  }
  if (status === "downloading" && job.speed) {
    details.push(formatSpeed(job.speed));
  }
  if (status === "downloading" && job.eta) {
    details.push(formatEta(job.eta));
  }
  if (status === "queued" || status === "preparing") {
    details.push(job.kind === "playlist" ? "playlist" : "waiting");
  }
  phase.textContent = details.join(" · ");
  foot.append(phase);
  if (job.totalBytes) {
    const size = document.createElement("span");
    size.className = "spacer";
    size.textContent = `${formatBytes(job.downloadedBytes)} / ${formatBytes(job.totalBytes)}`;
    foot.append(size);
  }
  item.append(foot);

  if (status === "error" && job.error) {
    const error = document.createElement("div");
    error.className = "job__error";
    error.textContent = job.error;
    item.append(error);
  }
  if (status === "unknown") {
    // The host went away mid-download; the row keeps its place and explains
    // itself rather than looking stuck.
    const note = document.createElement("div");
    note.className = "job__error";
    note.textContent = job.phase || "The host disconnected before this finished.";
    item.append(note);
  }
  if (status === "complete" && job.filename) {
    const file = document.createElement("div");
    file.className = "job__file";
    file.textContent = basename(job.filename);
    file.title = job.filename;
    item.append(file);
  }
  return item;
}

function jobButton(glyph, title, onClick) {
  const button = document.createElement("button");
  button.className = "job__icon";
  button.type = "button";
  button.textContent = glyph;
  button.title = title;
  button.addEventListener("click", onClick);
  return button;
}

function placeholderItem(text, isError = false) {
  const item = document.createElement("li");
  item.className = isError ? "empty status-line--error" : "empty";
  item.textContent = text;
  return item;
}

function releaseSession(resetSource = false) {
  if (resetSource) {
    document.getElementById("session-store").value = "";
    state.retryJobId = null;
    document.getElementById("use-session").textContent = "Use signed-in session";
  }
  if (state.sessionToken) ask({kind: "ui.releaseSession", sessionToken: state.sessionToken}).catch(() => {});
  state.sessionToken = null;
  state.sessionUrl = null;
  document.getElementById("forget-session").hidden = true;
  document.getElementById("session-status").textContent = "Without signed-in session. Enable only for videos your account can access.";
}

async function enableSession() {
  if (state.enablingSession) return;
  state.enablingSession = true;
  document.getElementById("use-session").disabled = true;
  const typed = ui.url.value.trim();
  let url = typed;
  try {
    let target = new URL(url);
    // youtu.be carries no YouTube login cookies; sign in against the canonical watch page.
    if (target.hostname === "youtu.be" && target.pathname.length > 1) {
      const canonical = new URL("https://www.youtube.com/watch" + target.search);
      canonical.searchParams.append("v", target.pathname.slice(1));
      target = canonical;
      url = ui.url.value = canonical.href;
    }
    if (target.protocol !== "https:") throw new Error("Signed-in sessions require an HTTPS video URL.");
    // Called directly from a click: Firefox permission prompts require a gesture.
    const granted = await browser.permissions.request({permissions: ["cookies"], origins: [target.origin + "/*"]});
    if (!granted) throw new Error("Session access was declined. Public downloads remain available.");
    const select = document.getElementById("session-store");
    const {stores} = await askChecked({kind: "ui.cookieStores"});
    const chosen = select.value;
    select.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = ""; placeholder.textContent = "Choose Firefox session"; select.append(placeholder);
    for (const store of stores) {
      const option = document.createElement("option"); option.value = store.id;
      option.textContent = store.id === "firefox-default" ? "Default Firefox session" : store.id;
      select.append(option);
    }
    const context = state.sourceContext;
    select.value = chosen || ([typed, url].includes(context?.url) ? context.storeId : "");
    select.hidden = false;
    if (!select.value) {
      setStatus("Choose the Firefox session you use for this site, then click Use signed-in session again.");
      return;
    }
    releaseSession();
    const result = await askChecked({kind: "ui.authorizeSession", url, storeId: select.value});
    if (ui.url.value.trim() !== url) {
      await ask({kind: "ui.releaseSession", sessionToken: result.sessionToken});
      return;
    }
    state.sessionToken = result.sessionToken;
    state.sessionUrl = url;
    document.getElementById("session-status").textContent = "Signed-in session enabled for this operation. The site can identify your account.";
    document.getElementById("forget-session").hidden = false;
    if (state.retryJobId) {
      await askChecked({kind: "ui.retry", jobId: state.retryJobId, sessionToken: state.sessionToken});
      releaseSession(true);
      setStatus("Retry queued with the saved choices. Existing files will not be replaced automatically.");
    } else {
      await probe(url, true);
    }
  } catch (error) {
    setStatus(error.message || "Session access failed.", true);
  } finally {
    state.enablingSession = false;
    document.getElementById("use-session").disabled = false;
  }
}

document.getElementById("use-session").addEventListener("click", enableSession);
document.getElementById("forget-session").addEventListener("click", () => {
  releaseSession();
  state.probe = null; state.probeStatus = "idle"; render();
  setStatus("Read the source again to continue without your account.");
});
window.addEventListener("pagehide", releaseSession);

/* ---------------------------------------------------------------- actions */

async function startDownload() {
  const probe = state.probe;
  if (!probe || state.busy || state.choosingDestination) {
    return;
  }
  if (probe.isPlaylist && state.playlistMode === "select" && !state.selected.size) {
    return;
  }
  const subtitles = [...state.subtitles].map((key) => {
    const [lang, kind] = key.split(":");
    return { lang, auto: kind === "auto" };
  });
  const mode = probe.isPlaylist ? state.playlistMode : "single";
  const spec = {
    url: mode === "single" && probe.isPlaylist ? state.sample?.url || probe.url : probe.url,
    title: probe.title,
    // renderOptions keeps state.formatId in step with the visible dropdown, so
    // this is exactly the format on screen.
    formatId: state.formatId || (state.audioOnly ? "bestaudio/best" : "best"),
    audioOnly: state.audioOnly,
    subtitles,
    playlistMode: mode,
    outputDir: state.outputDir || state.settings?.defaultDownloadDir || "",
    outputFilename: mode === "single" ? state.outputFilename || undefined : undefined,
    overwrite: mode === "single" && state.overwrite,
  };
  if (mode === "select") {
    spec.entryIndexes = [...state.selected].sort((a, b) => a - b);
  }

  state.busy = true;
  renderActions();
  try {
    await askChecked({ kind: "ui.download", ...spec, sessionToken: state.sessionToken });
    if (state.sessionToken) {
      releaseSession();
      state.probe = null;
      state.probeStatus = "idle";
      render();
    }
    state.busy = false;
    renderActions();
    setStatus("Added to the download queue.");
  } catch (error) {
    setStatus(error.message || String(error), true);
    state.busy = false;
    renderActions();
  }
}

async function retryJob(job) {
  if (job.authRequired) {
    releaseSession(true);
    state.retryJobId = job.jobId;
    document.getElementById("use-session").textContent = "Renew session and retry saved download";
    ui.url.value = job.url;
    state.sourceContext = null;
    state.probe = null;
    state.probeStatus = "idle";
    render();
    setStatus("Choose your Firefox session, then renew access to retry with the saved download choices.");
    return;
  }
  try {
    await askChecked({
      kind: "ui.retry", jobId: job.jobId,
    });
  } catch (error) {
    setStatus(error.message || String(error), true);
  }
}

function outputExtension() {
  if (state.audioOnly) return "mp3";
  if (state.formatId === "mp4") return "mp4";
  const sample = state.probe?.isPlaylist ? state.sample : state.probe;
  // A merged/custom selector's final container is determined by yt-dlp.
  return sample?.formats?.find((f) => f.formatId === state.formatId)?.ext || "";
}

function updateFilenameExtension() {
  const extension = outputExtension();
  if (state.outputFilename && extension) {
    state.outputFilename = state.outputFilename.replace(/\.(mp4|m4v|mkv|webm|mov|avi|flv|mp3|m4a|aac|opus|ogg|wav|flac|ts)$/i, "") + "." + extension;
    ui.outputFilename.value = state.outputFilename;
  }
  state.overwrite = false;
}

async function chooseFolder() {
  if (state.choosingDestination) return;
  const multiple = state.probe?.isPlaylist && state.playlistMode !== "single";
  const initialDir = ui.outputDir.value;
  const token = state.probeToken;
  state.choosingDestination = true;
  renderOptions();
  renderActions();
  setStatus(multiple ? "Choose a folder in the file browser…" : "Choose a filename and location in Save As…");
  try {
    const reply = await askChecked(multiple ? { kind: "ui.pickFolder", initialDir } : {
      kind: "ui.saveAs", initialDir,
      filename: state.outputFilename || state.sample?.title || state.probe?.title || "Download",
      extension: outputExtension(),
    });
    if (token !== state.probeToken) return;
    if (reply?.path && !reply.cancelled) {
      state.outputDir = reply.outputDir || reply.path;
      if (!multiple) {
        state.outputFilename = reply.filename;
        state.overwrite = !!reply.overwrite;
      }
      renderOptions();
    }
    setStatus(reply?.path ? "Save location selected. Click Download to start." : "Selection cancelled.");
  } catch (error) {
    setStatus(error.message || String(error), true);
  } finally {
    state.choosingDestination = false;
    renderOptions();
    renderActions();
  }
}

/* ------------------------------------------------------------------ wiring */

ui.probeButton.addEventListener("click", () => probe(ui.url.value.trim(), true));
document.getElementById("load-more-entries").addEventListener("click", () => loadEntries(true));
document.getElementById("open-history").addEventListener("click", () => browser.runtime.openOptionsPage());
ui.outputDir.addEventListener("input", () => { state.outputDir = ui.outputDir.value.trim(); state.overwrite = false; });
ui.outputFilename.addEventListener("input", () => { state.outputFilename = ui.outputFilename.value; state.overwrite = false; });
ui.url.addEventListener("input", () => { releaseSession(true); state.sourceContext = null; });
ui.url.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    probe(ui.url.value.trim());
  }
});
ui.url.addEventListener("change", () => {
  const value = ui.url.value.trim();
  if (value && value !== state.probe?.url) {
    probe(value);
  }
});
ui.format.addEventListener("change", () => {
  state.formatId = ui.format.value;
  updateFilenameExtension();
});
ui.audioOnly.addEventListener("change", () => {
  state.audioOnly = ui.audioOnly.checked;
  state.formatId = state.audioOnly ? "bestaudio/best" : pickDefaultFormat(state.probe);
  updateFilenameExtension();
  renderOptions();
});
ui.pickFolder.addEventListener("click", chooseFolder);
ui.download.addEventListener("click", startDownload);
ui.openOptions.addEventListener("click", () => browser.runtime.openOptionsPage());
ui.clearFinished.addEventListener("click", async () => {
  await ask({ kind: "ui.clearFinished" });
});
document.getElementById("select-all").addEventListener("click", () => {
  for (const entry of state.entries || []) {
    state.selected.add(entry.index);
  }
  renderSelection();
});
document.getElementById("select-none").addEventListener("click", () => {
  state.selected.clear();
  renderSelection();
});
for (const button of document.querySelectorAll(".seg")) {
  button.addEventListener("click", async () => {
    if (state.playlistMode !== button.dataset.mode) state.subtitles.clear();
    state.playlistMode = button.dataset.mode;
    state.formatId = state.audioOnly ? "bestaudio/best" : pickDefaultFormat(state.probe);
    updateFilenameExtension();
    if (state.playlistMode === "select" && !state.entries) {
      await loadEntries();
    }
    renderPlaylist();
    renderSelection();
    renderOptions();
  });
}

browser.runtime.onMessage.addListener((message) => {
  if (message?.kind !== "host") {
    return;
  }
  const event = message.message;
  switch (event.type) {
    case "link":
      state.connected = Boolean(event.connected);
      if (event.connected) {
        state.host = event.host;
        state.hostError = null;
      } else {
        state.hostError = event.error;
      }
      renderHost();
      renderBanners();
      renderOptions();
      renderActions();
      if (event.connected && !state.retryJobId && ui.url.value && ["idle", "error"].includes(state.probeStatus)) probe(ui.url.value);
      break;
    case "pending":
      state.sourceContext = event.sourceContext || null;
      releaseSession(true);
      ask({ kind: "ui.consumePending" });
      if (event.retryJob) { retryJob(event.retryJob); break; }
      if (event.url && /^https?:/i.test(event.url)) {
        ui.url.value = event.url;
        probe(event.url);
      }
      break;
    case "jobs":
      state.jobs = event.jobs || [];
      if (event.settings) {
        state.settings = { ...state.settings, ...event.settings };
        if (!state.outputDir) {
          state.outputDir = state.settings.defaultDownloadDir || "";
        }
      }
      renderQueue();
      renderOptions();
      renderActions();
      break;
    case "settings":
      if (event.settings) {
        state.settings = { ...state.settings, ...event.settings };
        renderOptions();
      }
      break;
    case "job":
      upsertJob(event.job);
      renderQueue();
      break;
    case "progress": {
      const job = state.jobs.find((item) => item.jobId === event.jobId);
      if (job) {
        Object.assign(job, {
          status: event.status ?? job.status,
          phase: event.phase ?? job.phase,
          percent: event.percent ?? job.percent,
          speed: event.speed ?? 0,
          eta: event.eta ?? null,
          downloadedBytes: event.downloadedBytes ?? job.downloadedBytes,
          totalBytes: event.totalBytes ?? job.totalBytes,
        });
        renderQueue();
      }
      break;
    }
    case "complete": {
      const job = state.jobs.find((item) => item.jobId === event.jobId);
      if (job) {
        Object.assign(job, {
          status: "complete",
          phase: "Done",
          percent: 100,
          speed: 0,
          eta: null,
          filename: event.filepath || job.filename,
        });
        sortJobs();
        renderQueue();
      }
      break;
    }
    case "error": {
      const job = state.jobs.find((item) => item.jobId === event.jobId);
      if (job) {
        Object.assign(job, { status: "error", error: event.message, phase: "Failed", speed: 0 });
        sortJobs();
        renderQueue();
      }
      if (!event.jobId) {
        setStatus(event.message, true);
      }
      break;
    }
    default:
      if (event.message) {
        setStatus(event.message, true);
      }
      break;
  }
});

function upsertJob(job) {
  const index = state.jobs.findIndex((item) => item.jobId === job.jobId);
  if (index === -1) {
    state.jobs = [...state.jobs, job];
  } else {
    state.jobs[index] = { ...state.jobs[index], ...job };
  }
  sortJobs();
}

/* ------------------------------------------------------------------- boot */

(async function boot() {
  const initial = await ask({ kind: "ui.state" });
  if (initial) {
    state.sourceContext = initial.sourceContext || null;
    state.connected = initial.connected;
    state.host = initial.host;
    state.hostError = initial.error;
    state.settings = initial.settings;
    state.jobs = initial.jobs || [];
    state.collapsed = new Set(initial.collapsed || []);
    state.outputDir = initial.settings?.defaultDownloadDir || "";
  }

  // Only an explicit toolbar/context-menu action supplies a browsing URL.
  const url = initial?.pendingUrl;
  if (url) await ask({ kind: "ui.consumePending" });
  if (url && /^https?:/i.test(url)) {
    ui.url.value = url;
  }
  render();
  if (initial?.pendingRetry) {
    await retryJob(initial.pendingRetry);
    return;
  }
  if (ui.url.value && state.connected) {
    probe(ui.url.value);
  } else {
    setStatus("Open a video page, or paste a URL above.");
  }
})();
