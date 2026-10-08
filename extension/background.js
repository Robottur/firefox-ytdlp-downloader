/**
 * Firefox background event page: the only thing that talks to the native host.
 *
 * Responsibilities
 *  - keep one long-lived native messaging port open, reconnecting with backoff
 *  - remember settings, the host self-check, and a mirror of the job queue
 *  - route requests from the popup/options page and rebroadcast host events
 *  - own the context menu entries
 *
 * The native host is authoritative for the queue; the mirror here only exists so
 * a freshly opened popup can render immediately and resync via `list_jobs`.
 */

const HOST_NAME = "ytdlp_downloader.host";

const DEFAULT_SETTINGS = {
  defaultDownloadDir: "",
  defaultFormat: "mp4",
  maxConcurrentDownloads: 2,
  notifyOnComplete: true,
};

const MENUS = [
  // Players often put an image, link or text overlay over the <video>. Keep
  // one visible entry across all page contexts, without a nested submenu.
  { id: "ytdlp-download", contexts: ["all"], title: "Download with &yt-dlp" },
];

const state = {
  connected: false,
  host: null, // host_ready payload
  linkError: null, // { code, message }
  settings: { ...DEFAULT_SETTINGS },
  jobs: new Map(), // jobId -> job record
  pendingUrl: null,
  pendingRetry: null,
  sourceContext: null, // url handed over by a context menu click
  collapsed: new Set(), // playlist groups the user folded away
};

let port = null;
let retryDelay = 1000;
let nextRequestId = 1;
let reconnectTimer = null;
let storedSettings = {};
let openingWindow = null;
let folderRequest = null;
let saveFileRequest = null;
const inflight = new Map(); // requestId -> { resolve, reject }

/* ----------------------------------------------------------------- state io */

async function loadSettings() {
  try {
    const stored = await browser.storage.local.get(["settings", "mp4DefaultMigrated"]);
    storedSettings = stored.settings || {};
    if (!stored.mp4DefaultMigrated) {
      if (!storedSettings.defaultFormat || storedSettings.defaultFormat === "bestvideo+bestaudio/best") {
        storedSettings.defaultFormat = "mp4";
      }
      await browser.storage.local.set({ settings: storedSettings, mp4DefaultMigrated: true });
    }
    state.settings = { ...DEFAULT_SETTINGS, ...storedSettings };
  } catch (error) {
    console.warn("Could not read settings:", error);
  }
}

async function saveSettings(settings) {
  const updated = { ...state.settings, ...settings };
  if (state.connected) {
    const reply = await send("update_settings", { settings: updated });
    state.settings = reply.settings;
  } else {
    state.settings = updated;
  }
  storedSettings = { ...state.settings };
  await browser.storage.local.set({ settings: state.settings });
  // Push to the host so a settings change applies even when this page is the
  // only thing talking to it.
}

/* ------------------------------------------------------------ host plumbing */

function broadcast(message) {
  browser.runtime.sendMessage({ kind: "host", message }).catch(() => {
    // No popup or options page is open; nothing to do.
  });
}

function setLinkError(error) {
  state.connected = false;
  state.linkError = error;
  broadcast({ type: "link", connected: false, error });
}

function connect() {
  if (port) {
    return;
  }
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  try {
    port = browser.runtime.connectNative(HOST_NAME);
  } catch (error) {
    port = null;
    setLinkError(describeConnectError(error));
    scheduleReconnect();
    return;
  }
  port.onMessage.addListener(onHostMessage);
  port.onDisconnect.addListener(onPortDisconnect);
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  const delay = retryDelay;
  retryDelay = Math.min(retryDelay * 2, 15000);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (!port) {
      connect();
    }
  }, delay);
}

function describeConnectError(error) {
  const message = String(error?.message || error || "Unknown error");
  if (/No such native application/i.test(message)) {
    return {
      code: "not_installed",
      message:
        "Install the companion host with install_linux.sh (Linux) or install_windows.ps1 (Windows).",
    };
  }
  if (/not allowed|does not have permission|not permitted/i.test(message)) {
    return {
      code: "not_allowed",
      message:
        "Firefox refused the connection: the native messaging manifest does not list this extension id.",
    };
  }
  return { code: "connect_failed", message };
}

function onPortDisconnect(disconnectedPort) {
  // Firefox's Port.onDisconnect is a plain Event with no port argument, so the
  // reason has to be tracked by us: `port` is the only handle we had.
  const failure = disconnectedPort?.error || port?.error;
  port = null;
  // Jobs may have been running; the host marks them interrupted when it exits,
  // so the mirror is stale until the next list_jobs.
  for (const job of state.jobs.values()) {
    if (job.status === "downloading" || job.status === "queued" || job.status === "preparing") {
      job.status = "unknown";
      job.phase = "Waiting for the host to come back…";
    }
  }
  // Anything in flight will never be answered now; fail it immediately instead
  // of leaving the popup waiting out the 180 second timeout.
  const error = failure ? describeConnectError(failure) :
    { message: "The connection to the native host was lost. Downloads continue in the companion app.", code: "disconnected" };
  for (const { reject, timer } of inflight.values()) {
    clearTimeout(timer);
    reject(new Error(error.message));
  }
  inflight.clear();
  setLinkError(error);
  scheduleReconnect();
}

/** Send a request to the host and resolve when its reply carries our id. */
function send(type, payload = {}) {
  if (!port) {
    return Promise.reject(new Error("The native host is not connected."));
  }
  const requestId = `r${nextRequestId++}`;
  const frame = { ...payload, type, requestId };
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      if (inflight.has(requestId)) {
        inflight.delete(requestId);
        reject(new Error("The host did not answer in time. Try again, or check the host log in Settings."));
      }
    }, ["pick_folder", "pick_save_file", "diagnostics_save"].includes(type) ? 600000 : 180000);
    inflight.set(requestId, { resolve, reject, timer });
    try {
      port.postMessage(frame);
    } catch (error) {
      inflight.delete(requestId);
      clearTimeout(timer);
      reject(error);
    }
  });
}

function onHostMessage(message) {
  if (!message || typeof message.type !== "string") {
    return;
  }
  if (message.requestId && message.final !== false && inflight.has(message.requestId)) {
    const { resolve, reject, timer } = inflight.get(message.requestId);
    inflight.delete(message.requestId);
    clearTimeout(timer);
    if (message.type === "error") {
      const failure = new Error(message.message || "The host reported an error.");
      failure.code = message.code;
      reject(failure);
    } else {
      resolve(message);
    }
  }

  switch (message.type) {
    case "session_authorized":
    case "diagnostics":
      // Sensitive replies belong only to the initiating UI request.
      break;
    case "host_ready":
      state.connected = true;
      state.linkError = null;
      state.host = message;
      retryDelay = 1000;
      if (message.settings) {
        state.settings = { ...DEFAULT_SETTINGS, ...message.settings, ...storedSettings };
        if (!state.settings.defaultDownloadDir) state.settings.defaultDownloadDir = message.settings.defaultDownloadDir;
      }
      broadcast({ type: "link", connected: true, host: message });
      // Resync: the host owns the queue and the authoritative settings.
      // list_jobs answers on its own; asking twice would just apply the same
      // list twice.
      send("update_settings", { settings: state.settings })
        .then(() => send("list_jobs"))
        .catch((error) => broadcast({ type: "error", message: error.message }));
      break;
    case "host_error":
      setLinkError({ code: message.code || "host_error", message: message.message });
      break;
    case "jobs":
      applyJobList(message);
      if (message.final !== false) broadcast({ type: "jobs", jobs: orderedJobs(), settings: message.settings });
      break;
    case "settings":
      state.settings = { ...state.settings, ...message.settings };
      broadcast(message);
      break;
    case "job":
      state.jobs.set(message.job.jobId, message.job);
      broadcast(message);
      break;
    case "progress":
      applyProgress(message);
      broadcast(message);
      break;
    case "complete":
      applyCompletion(message);
      notifyComplete(message);
      broadcast(message);
      break;
    case "error":
      broadcast(message);
      break;
    default:
      broadcast(message);
  }
}

function applyJobList(message) {
  if (!message.append) state.jobs.clear();
  for (const job of message.jobs || []) {
    state.jobs.set(job.jobId, job);
  }
  if (message.settings) {
    state.settings = { ...state.settings, ...message.settings };
  }
}

function applyProgress(message) {
  const job = state.jobs.get(message.jobId);
  if (!job) {
    return;
  }
  job.status = message.status ?? job.status;
  job.phase = message.phase ?? job.phase;
  job.percent = message.percent ?? job.percent;
  job.speed = message.speed ?? 0;
  job.eta = message.eta ?? null;
  job.downloadedBytes = message.downloadedBytes ?? job.downloadedBytes;
  job.totalBytes = message.totalBytes ?? job.totalBytes;
}

function applyCompletion(message) {
  const job = state.jobs.get(message.jobId);
  if (job) {
    job.status = "complete";
    job.phase = "Done";
    job.percent = 100;
    job.speed = 0;
    job.eta = null;
    job.filename = message.filepath || job.filename;
    if (message.title) {
      job.title = message.title;
    }
  }
}

async function notifyComplete(message) {
  if (!state.settings.notifyOnComplete) {
    return;
  }
  const job = state.jobs.get(message.jobId);
  if (job && job.groupId) {
    // Playlist entries finish one by one; the group completion is announced
    // instead, otherwise a 50-video playlist means 50 toasts.
    return;
  }
  try {
    await browser.notifications.create({
      type: "basic",
      iconUrl: browser.runtime.getURL("icons/icon-96.png"),
      title: "Download finished",
      message: "Your download is ready.",
    });
  } catch (error) {
    console.warn("Notification failed:", error);
  }
}

/* ------------------------------------------------------------------- menus */

async function ensureMenus() {
  try {
    await browser.contextMenus.removeAll();
    for (const menu of MENUS) {
      await browser.contextMenus.create(menu);
    }
  } catch (error) {
    console.warn("Could not create context menus:", error);
  }
}

function urlFromMenu(info, tab) {
  const page = info.frameUrl || info.pageUrl || tab?.url || "";
  const candidate = info.mediaType ? page : info.linkUrl || page;
  // A <video> whose source is a blob: URL carries no media; the page URL is
  // what yt-dlp can actually resolve.
  return /^https?:\/\//i.test(candidate) ? candidate : /^https?:\/\//i.test(page) ? page : "";
}

async function onMenuClicked(info, tab) {
  if (tab?.incognito) return;
  const url = urlFromMenu(info, tab);
  if (!url) {
    return;
  }
  await openDownloader(url, tab);
}

/** A normal extension window survives focus moving to a native folder dialog. */
async function openDownloader(url, tab, retryJob = null) {
  state.pendingRetry = retryJob;
  state.sourceContext = tab ? {url, storeId: tab.cookieStoreId || "firefox-default"} : null;
  state.pendingUrl = url;
  if (openingWindow) await openingWindow;
  openingWindow = (async () => {
    const page = browser.runtime.getURL("popup/popup.html");
    const windows = await browser.windows.getAll({ populate: true });
    const existing = windows.find((win) => win.tabs?.some((tab) => tab.url === page));
    if (existing) {
      broadcast({ type: "pending", url, sourceContext: state.sourceContext, retryJob });
      await browser.windows.update(existing.id, { focused: true });
      return existing;
    }
    return browser.windows.create({ url: page, type: "popup", width: 460, height: 740 });
  })();
  try {
    return await openingWindow;
  } finally {
    openingWindow = null;
  }
}

async function chooseFolder(initialDir) {
  if (saveFileRequest) throw new Error("Finish the open Save As dialog first.");
  if (folderRequest) return folderRequest;
  folderRequest = (async () => {
    const reply = await send("pick_folder", { initialDir });
    // Own persistence here: even closing the downloader manually while the
    // dialog is open must not discard the selected destination.
    if (reply.path) await saveSettings({ defaultDownloadDir: reply.path });
    return reply;
  })();
  try { return await folderRequest; } finally { folderRequest = null; }
}

async function chooseSaveFile({ initialDir, filename, extension }) {
  if (folderRequest) throw new Error("Finish the open folder dialog first.");
  if (saveFileRequest) return saveFileRequest;
  saveFileRequest = (async () => {
    const reply = await send("pick_save_file", { initialDir, filename, extension });
    if (reply.outputDir) await saveSettings({ defaultDownloadDir: reply.outputDir });
    return reply;
  })();
  try { return await saveFileRequest; } finally { saveFileRequest = null; }
}

/* --------------------------------------------------------------- UI bridge */

async function authorizeSession({url, storeId}) {
  const target = new URL(url);
  if (target.protocol !== "https:") throw new Error("Signed-in sessions require HTTPS.");
  const allowed = await browser.permissions.contains({permissions: ["cookies"], origins: [target.origin + "/*"]});
  if (!allowed) throw new Error("Allow session access for this site first.");
  const stores = await browser.cookies.getAllCookieStores();
  if (!stores.some(store => store.id === storeId && !store.id.includes("private"))) {
    throw new Error("Choose an available non-private Firefox session.");
  }
  // Firefox limits results to the requested URL and permitted site. Preserve
  // the chosen container; partitioned cookies are not mixed across top sites.
  const cookies = await browser.cookies.getAll({url, storeId, partitionKey: {}});
  const matching = cookies.filter(cookie => {
    if (!cookie.partitionKey?.topLevelSite) return true;
    const top = new URL(cookie.partitionKey.topLevelSite);
    const domain = cookie.domain.replace(/^\./, "");
    return top.protocol === target.protocol &&
      (target.hostname === top.hostname || target.hostname.endsWith("." + top.hostname)) &&
      (top.hostname === domain || top.hostname.endsWith("." + domain) || domain.endsWith("." + top.hostname));
  });
  return send("authorize_session", {url, cookies: matching});
}

const HANDLERS = {
  "ui.retryWithConsent": ({jobId}) => {
    const job = state.jobs.get(jobId);
    if (!job?.authRequired) throw new Error("The saved authenticated job is unavailable.");
    return openDownloader(job.url, null, job);
  },
  "ui.historyPolicy": () => send("history_policy"),
  "ui.configureHistory": ({enabled, existing}) => send("configure_history", {enabled, existing}),
  "ui.dismiss": ({jobId}) => send("dismiss", {jobId}),
  "ui.diagnosticsStatus": () => send("diagnostics_status"),
  "ui.diagnosticsStart": () => send("diagnostics_start"),
  "ui.diagnosticsStop": () => send("diagnostics_stop"),
  "ui.diagnosticsPreview": () => send("diagnostics_preview"),
  "ui.diagnosticsSave": ({previewId}) => send("diagnostics_save", {previewId}),
  "ui.diagnosticsDelete": () => send("diagnostics_delete"),
  "ui.diagnosticsDeleteLegacy": () => send("diagnostics_delete_legacy"),
  "ui.authorizeSession": authorizeSession,
  "ui.releaseSession": ({sessionToken}) => send("release_session", {sessionToken}),
  "ui.cookieStores": async () => ({stores: (await browser.cookies.getAllCookieStores()).filter(s => !s.id.includes("private")).map(s => ({id: s.id}))}),
  "ui.state": () => ({
    connected: state.connected,
    host: state.host,
    error: state.linkError,
    settings: state.settings,
    jobs: orderedJobs(),
    pendingUrl: state.pendingUrl,
    pendingRetry: state.pendingRetry,
    sourceContext: state.sourceContext,
    collapsed: [...state.collapsed],
  }),

  "ui.probe": ({ url, refresh, single, sessionToken }) => send("probe", { url, refresh, single, sessionToken }),

  "ui.probeEntries": ({ url, limit, offset, sessionToken }) => send("probe_entries", { url, limit, offset, sessionToken }),

  "ui.download": (message) => send("download_start", message),

  "ui.cancel": ({ jobId }) => send("cancel", { jobId }),

  "ui.retry": ({ jobId, sessionToken }) => send("retry", { jobId, sessionToken }),

  "ui.clearFinished": ({includeInterrupted}) => send("clear_finished", {includeInterrupted}),

  "ui.pickFolder": ({ initialDir }) => chooseFolder(initialDir),

  "ui.saveAs": (message) => chooseSaveFile(message),

  "ui.openPath": ({ path }) => send("open_path", { path }),

  "ui.saveSettings": async ({ settings }) => {
    await saveSettings(settings);
    return { settings: state.settings };
  },

  "ui.toggleGroup": ({ jobId }) => {
    if (state.collapsed.has(jobId)) {
      state.collapsed.delete(jobId);
    } else {
      state.collapsed.add(jobId);
    }
    return { collapsed: [...state.collapsed] };
  },

  "ui.consumePending": () => {
    const url = state.pendingUrl;
    state.pendingUrl = null;
    state.pendingRetry = null;
    return { url };
  },

};

/**
 * Active jobs first, then finished ones; children follow their group.
 *
 * Collapsing is a *rendering* concern and deliberately not applied here: the
 * popup owns the collapsed set and needs every child record in order to redraw
 * a group as it is expanded again.
 */
function orderedJobs() {
  const all = [...state.jobs.values()];
  const byGroup = new Map();
  const roots = [];
  for (const job of all) {
    if (job.groupId) {
      byGroup.set(job.groupId, [...(byGroup.get(job.groupId) || []), job]);
    } else {
      roots.push(job);
    }
  }
  roots.sort((a, b) => rankJob(a) - rankJob(b) || (b.createdAt || 0) - (a.createdAt || 0));
  const out = [];
  for (const root of roots) {
    out.push(root);
    out.push(...(byGroup.get(root.jobId) || []).sort((a, b) => (a.entryIndex ?? 0) - (b.entryIndex ?? 0)));
  }
  const rootIds = new Set(roots.map((job) => job.jobId));
  for (const [groupId, children] of byGroup) {
    if (!rootIds.has(groupId)) out.push(...children);
  }
  return out;
}

/** Running work first, then failures, then successes. */
function rankJob(job) {
  switch (job.status) {
    case "complete":
      return 3;
    case "error":
    case "cancelled":
      return 2;
    default:
      return 1;
  }
}

browser.runtime.onMessage.addListener((message, sender) => {
  if (sender?.tab?.incognito) {
    return Promise.resolve({ __error: {code: "private_window_disabled", message: "Downloads are disabled in private windows."} });
  }
  const handler = HANDLERS[message?.kind];
  if (!handler) {
    return undefined;
  }
  return Promise.resolve()
    .then(() => handler(message))
    .catch((error) => ({ __error: { message: error?.message || String(error), code: error?.code } }));
});

/* ------------------------------------------------------------------ startup */

browser.runtime.onInstalled.addListener(() => {
  ensureMenus();
});

browser.contextMenus.onClicked.addListener((info, tab) => {
  onMenuClicked(info, tab).catch((error) => console.error("Could not open downloader:", error));
});

browser.action.onClicked.addListener((tab) => {
  if (tab?.incognito) return;
  openDownloader(tab?.url || "", tab).catch((error) => console.error("Could not open downloader:", error));
});

browser.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.settings) {
    return;
  }
  // The options page writes storage itself; mirror the change and tell the
  // host. Writes made by saveSettings() above land here too, so compare first
  // and skip the redundant round trip.
  const incoming = { ...DEFAULT_SETTINGS, ...(changes.settings.newValue || {}) };
  if (JSON.stringify(incoming) === JSON.stringify(state.settings)) {
    return;
  }
  state.settings = incoming;
  storedSettings = incoming;
  if (state.connected) {
    send("update_settings", { settings: state.settings }).catch(() => {});
  }
});

(async function start() {
  await loadSettings();
  ensureMenus();
  connect();
})();
