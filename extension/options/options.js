/**
 * Options page.
 *
 * Writes go through the background script (`ui.saveSettings`), which persists
 * them to browser.storage.local and pushes the same values to the native host,
 * so the two never drift apart.
 */

const el = {
  dir: document.getElementById("default-dir"),
  dirHint: document.getElementById("dir-hint"),
  pickFolder: document.getElementById("pick-folder"),
  format: document.getElementById("default-format"),
  concurrency: document.getElementById("concurrency"),
  concurrencyValue: document.getElementById("concurrency-value"),
  notify: document.getElementById("notify"),
  hostStatus: document.getElementById("host-status"),
  hostError: document.getElementById("host-error"),
  saved: document.getElementById("saved"),
};

let settings = {};
let savedTimer = null;
let jobs = [];

function ask(message) {
  return browser.runtime.sendMessage(message);
}

async function askChecked(message) {
  const reply = await ask(message);
  if (reply && reply.__error) {
    throw new Error(reply.__error.message);
  }
  return reply;
}

function flashSaved(text = "Saved") {
  el.saved.textContent = text;
  clearTimeout(savedTimer);
  savedTimer = setTimeout(() => {
    el.saved.textContent = "";
  }, 2000);
}

function fill(settings) {
  el.dir.value = settings.defaultDownloadDir || "";
  el.format.value = settings.defaultFormat || "mp4";
  el.concurrency.value = String(settings.maxConcurrentDownloads || 2);
  el.concurrencyValue.textContent = el.concurrency.value;
  el.notify.checked = settings.notifyOnComplete !== false;
}

function renderHost(state) {
  if (!state.connected || !state.host) {
    el.hostStatus.className = "status-card status-card--error";
    el.hostStatus.textContent = "Native host not connected.";
    el.hostError.textContent =
      (state.error?.message || "") +
      (state.error?.code === "not_installed"
        ? " Run install_linux.sh (Linux) or install_windows.ps1 (Windows)."
        : " The background script keeps retrying.");
    return;
  }
  const host = state.host;
  el.hostStatus.className = "status-card";
  el.hostStatus.textContent = [
    `Host          ${host.hostVersion} (${host.hostName})`,
    `yt-dlp        ${host.ytDlpVersion}`,
    `Python        ${host.pythonVersion}`,
    `ffmpeg        ${host.ffmpegFound ? host.ffmpegVersion : "not found — audio-only is disabled"}`,
    `Platform      ${host.platform}`,
    `JS runtimes   ${(host.jsRuntimes || []).join(", ") || "none — install Deno for YouTube"}`,
    `EJS solver    ${host.challengeSolver ? `yt-dlp-ejs ${host.challengeSolver}` : "missing — re-run the installer for YouTube"}`,
    `State folder  ${host.stateDir}`,
  ].join("\n");
  el.hostError.textContent = "";
}

async function persist(patch) {
  settings = { ...settings, ...patch };
  try {
    await askChecked({ kind: "ui.saveSettings", settings: patch });
    flashSaved();
  } catch (error) {
    el.saved.textContent = `Could not save: ${error.message}`;
  }
}

el.pickFolder.addEventListener("click", async () => {
  try {
    const reply = await askChecked({ kind: "ui.pickFolder", initialDir: el.dir.value });
    if (reply?.path) {
      el.dir.value = reply.path;
      el.dirHint.textContent = "";
      persist({ defaultDownloadDir: reply.path });
    }
  } catch (error) {
    el.saved.textContent = `Folder picker failed: ${error.message}`;
  }
});

el.dir.addEventListener("change", () => persist({ defaultDownloadDir: el.dir.value.trim() }));

el.format.addEventListener("change", () => persist({ defaultFormat: el.format.value.trim() }));

el.concurrency.addEventListener("input", () => {
  el.concurrencyValue.textContent = el.concurrency.value;
});
el.concurrency.addEventListener("change", () =>
  persist({ maxConcurrentDownloads: Number(el.concurrency.value) }),
);

el.notify.addEventListener("change", () => persist({ notifyOnComplete: el.notify.checked }));

browser.runtime.onMessage.addListener((message) => {
  if (message?.kind === "host" && message.message?.type === "link") {
    refreshHost();
  }
  if (message?.kind !== "host") return;
  const event = message.message;
  if (event.type === "jobs") { jobs = event.jobs || []; renderHistory(); }
  if (event.type === "job") {
    const index = jobs.findIndex((job) => job.jobId === event.job.jobId);
    if (index < 0) jobs.push(event.job); else jobs[index] = event.job;
    if (["complete", "error", "cancelled", "interrupted"].includes(event.job.status)) {
      renderHistory();
      if (pendingHistoryEnabled === null) refreshHistoryPolicy();
    }
  }
  if (event.type === "history_policy") renderHistoryPolicy(event);
  if (event.type === "settings") { settings = event.settings; fill(settings); }
});

function renderHistory() {
  const query = document.getElementById("history-search").value.toLowerCase();
  const list = document.getElementById("history");
  list.replaceChildren();
  const finished = jobs.filter((job) => ["complete", "error", "cancelled", "interrupted"].includes(job.status) &&
    `${job.title} ${job.url}`.toLowerCase().includes(query)).sort((a, b) => b.updatedAt - a.updatedAt);
  document.getElementById("history-empty").hidden = finished.length > 0;
  for (const job of finished) {
    const row = document.createElement("li");
    const title = document.createElement("strong");
    title.textContent = job.title || job.url;
    const detail = document.createElement("p");
    detail.className = "hint";
    detail.textContent = `${job.status} · ${new Date(job.updatedAt * 1000).toLocaleString()}${job.error ? ` · ${job.error}` : ""}`;
    row.append(title, detail);
    const link = document.createElement("a");
    if (/^https?:\/\//i.test(job.url)) { link.href = job.url; link.target = "_blank"; link.rel = "noreferrer"; }
    link.textContent = "Source";
    row.append(link);
    const action = (label, message) => {
      const button = document.createElement("button");
      button.className = "button";
      button.textContent = label;
      button.addEventListener("click", async () => {
        try { await askChecked(message); } catch (error) { el.saved.textContent = error.message; }
      });
      row.append(button);
    };
    if (job.filename) action("Open folder", {kind: "ui.openPath", path: job.filename});
    if (job.status !== "complete") action(job.authRequired ? "Renew session and retry" : "Retry", {kind: job.authRequired ? "ui.retryWithConsent" : "ui.retry", jobId: job.jobId});
    action("Dismiss", {kind: "ui.dismiss", jobId: job.jobId});
    list.append(row);
  }
}
document.getElementById("history-search").addEventListener("input", renderHistory);
document.getElementById("clear-history").addEventListener("click", async () => {
  try {
    await askChecked({kind: "ui.clearFinished"});
    await refreshHistoryPolicy();
  } catch (error) { el.saved.textContent = error.message; }
});

async function refreshHost() {
  const state = await ask({ kind: "ui.state" });
  if (state) {
    renderHost(state);
    jobs = state.jobs || [];
    renderHistory();
  }
}

(async function boot() {
  const state = await ask({ kind: "ui.state" });
  if (state) {
    settings = state.settings || {};
    fill(settings);
    renderHost(state);
    jobs = state.jobs || [];
    renderHistory();
    if (state.settings?.defaultDownloadDir) {
      el.dirHint.textContent = "Type a folder path or use Change….";
    }
  }
})();


let diagnosticPreviewId = null;
let legacyLogsKept = false;
function clearDiagnosticPreview() {
  diagnosticPreviewId = null;
  document.getElementById("diagnostics-save").disabled = true;
  document.getElementById("diagnostics-content").textContent = "";
  document.getElementById("diagnostics-content").hidden = true;
}
function renderDiagnostics(status) {
  document.getElementById("diagnostics-status").textContent = status.captureActive
    ? `Capture active: up to ${status.captureSecondsRemaining} seconds remaining.` : "Optional capture is off.";
  document.getElementById("diagnostics-legacy").hidden = legacyLogsKept || !status.legacyLogsPresent;
}
async function diagnosticAction(action) {
  try {
    const reply = await askChecked({kind: `ui.diagnostics${action}`, previewId: diagnosticPreviewId});
    if (action === "Preview") {
      diagnosticPreviewId = reply.previewId;
      document.getElementById("diagnostics-content").textContent = reply.text || "No retained diagnostics.";
      document.getElementById("diagnostics-content").hidden = false;
      document.getElementById("diagnostics-save").disabled = !diagnosticPreviewId;
    } else if (action === "Save") {
      clearDiagnosticPreview();
      flashSaved(reply?.saved ? "Diagnostics saved locally" : "Save cancelled");
    } else {
      if (["Delete", "Start"].includes(action)) clearDiagnosticPreview();
      renderDiagnostics(reply);
    }
  } catch (error) {
    el.saved.textContent = error.message;
  }
}
for (const [id, action] of [["start", "Start"], ["stop", "Stop"], ["preview", "Preview"],
  ["save", "Save"], ["delete", "Delete"], ["delete-legacy", "DeleteLegacy"]]) {
  document.getElementById(`diagnostics-${id}`).addEventListener("click", () => diagnosticAction(action));
}
document.getElementById("diagnostics-keep-legacy").addEventListener("click", () => {
  legacyLogsKept = true;
  document.getElementById("diagnostics-legacy").hidden = true;
  flashSaved("Old raw logs kept; remove them here at any time.");
});
diagnosticAction("Status");
document.defaultView.setInterval(() => diagnosticAction("Status"), 15000);


let historyPolicy = null;
let pendingHistoryEnabled = null;
function renderHistoryPolicy(policy) {
  historyPolicy = policy;
  pendingHistoryEnabled = null;
  const checkbox = document.getElementById("retain-history");
  checkbox.disabled = false;
  checkbox.checked = policy.retainFinishedHistory === true;
  document.getElementById("existing-history-choice").hidden = !policy.historyMigrationPending;
  document.getElementById("history-policy-status").textContent =
    `${checkbox.checked ? "Finished history is enabled." : "New finished history is off."} ` +
    `${policy.retainedHistoryCount || 0} finished records are currently stored.` +
    (policy.historyMigrationPending ? " Choose whether to keep or remove older records." : "");
}
async function refreshHistoryPolicy() {
  try {
    const policy = await askChecked({kind: "ui.historyPolicy"});
    if (policy && typeof policy.retainFinishedHistory === "boolean") renderHistoryPolicy(policy);
  } catch (error) {
    document.getElementById("history-policy-status").textContent = `Could not read history preference: ${error.message}`;
  }
}
async function configureHistory(enabled, existing) {
  try {
    const policy = await askChecked({kind: "ui.configureHistory", enabled, existing});
    renderHistoryPolicy(policy);
    flashSaved(existing === "remove" ? "Finished records removed; media files preserved" : "History preference saved");
  } catch (error) {
    if (historyPolicy) document.getElementById("retain-history").checked = historyPolicy.retainFinishedHistory;
    document.getElementById("history-policy-status").textContent = `Could not change history: ${error.message}`;
  }
}
document.getElementById("retain-history").addEventListener("change", (event) => {
  const enabled = event.target.checked;
  if (historyPolicy?.historyMigrationPending || (!enabled && historyPolicy?.retainedHistoryCount > 0)) {
    pendingHistoryEnabled = enabled;
    document.getElementById("existing-history-choice").hidden = false;
    document.getElementById("history-policy-status").textContent =
      "Preference not yet changed. Choose whether to keep or remove existing finished records.";
    return;
  }
  configureHistory(enabled);
});
for (const choice of ["keep", "remove"]) {
  document.getElementById(`history-${choice}-existing`).addEventListener("click", () =>
    configureHistory(pendingHistoryEnabled ?? historyPolicy?.retainFinishedHistory ?? false, choice));
}
document.getElementById("dismiss-interrupted").addEventListener("click", async () => {
  try {
    await askChecked({kind: "ui.clearFinished", includeInterrupted: true});
    await refreshHistoryPolicy();
    flashSaved("Finished and interrupted records cleared; media files preserved");
  } catch (error) { document.getElementById("history-policy-status").textContent = error.message; }
});
refreshHistoryPolicy();
