/**
 * Playlist harness: drives the popup's playlist UI against a real playlist.
 *
 *   node tools/extension_playlist_harness.mjs <playlist-url> [entries]
 *
 * Playlists cannot be faked locally -- the flat extraction only reports a
 * playlist when the site really is one -- so this mode takes a URL. Use a
 * playlist with short videos; it downloads the entries you ask for.
 */

import { spawn } from "node:child_process";
import { readFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..");
const EXTENSION = join(ROOT, "extension");
const HOST_DIR = join(ROOT, "native-host");

const require = createRequire(import.meta.url);
let JSDOM;
try {
  ({ JSDOM } = require("jsdom"));
} catch {
  console.error("jsdom is required:  npm install jsdom   (or set NODE_PATH)");
  process.exit(2);
}

const PL_URL = process.argv[2];
const WANTED = Number(process.argv[3] || 2);
if (!PL_URL) {
  console.error("usage: node tools/extension_playlist_harness.mjs <playlist-url> [entries]");
  process.exit(2);
}

function frame(payload) {
  const body = Buffer.from(JSON.stringify(payload), "utf8");
  const header = Buffer.alloc(4);
  header.writeUInt32LE(body.length, 0);
  return Buffer.concat([header, body]);
}

const checks = [];
function check(name, condition, detail = "") {
  checks.push({ ok: Boolean(condition), name });
  console.log(`${condition ? "  ok  " : "  FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(label, predicate, timeout = 180000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = await predicate();
    if (value) {
      return value;
    }
    await sleep(60);
  }
  throw new Error(`timed out waiting for ${label}`);
}

async function main() {
  const workdir = mkdtempSync(join(tmpdir(), "ytdlp-playlist-harness-"));
  const profile = { messageListeners: [], storageListeners: [], menus: [], notifications: [] };
  const host = spawn(process.env.PYTHON || "python3", ["-X", "utf8", "-m", "ytdlp_host.main", "--stdio"], {
    cwd: HOST_DIR,
    stdio: ["pipe", "pipe", "inherit"],
    env: {...process.env, YTDLP_HOST_STATE_DIR: join(workdir, "state")},
  });

  const port = {
    onMessage: { addListener: (fn) => (port._onMessage = fn) },
    onDisconnect: { addListener: () => {} },
    postMessage: (message) => host.stdin.write(frame(message)),
  };
  let buffer = Buffer.alloc(0);
  host.stdout.on("data", (chunk) => {
    buffer = Buffer.concat([buffer, chunk]);
    for (;;) {
      if (buffer.length < 4) return;
      const length = buffer.readUInt32LE(0);
      if (buffer.length < 4 + length) return;
      const message = JSON.parse(buffer.subarray(4, 4 + length).toString("utf8"));
      buffer = buffer.subarray(4 + length);
      if (process.env.HARNESS_TRACE) {
        console.log(`  host->ext ${message.type} ${message.jobId || message.requestId || ""} ${message.job?.status || ""}`);
      }
      port._onMessage?.(message);
    }
  });

  const storage = new Map();
  storage.set("settings", {defaultDownloadDir: join(workdir, "downloads")});
  const browser = {
    runtime: {
      getURL: (p) => `moz-extension://harness/${p}`,
      connectNative: () => port,
      sendMessage: async (message) => {
        for (const listener of profile.messageListeners) {
          const result = listener(message, {});
          if (result !== undefined) {
            return result;
          }
        }
        return undefined;
      },
      openOptionsPage: () => {},
      onMessage: {
        addListener: (fn) => profile.messageListeners.push(fn),
        removeListener: () => {},
      },
      onInstalled: { addListener: () => {} },
    },
    storage: {
      local: {
        get: async () => Object.fromEntries(storage),
        set: async (items) => {
          for (const [k, v] of Object.entries(items)) {
            storage.set(k, v);
            for (const fn of profile.storageListeners) {
              fn({ [k]: { newValue: v } }, "local");
            }
          }
        },
        remove: async (k) => storage.delete(k),
      },
      onChanged: { addListener: (fn) => profile.storageListeners.push(fn), removeListener: () => {} },
    },
    contextMenus: {
      removeAll: async () => 0,
      create: (m) => profile.menus.push(m),
      onClicked: { addListener: () => {}, removeListener: () => {} },
    },
    notifications: { create: async (o) => profile.notifications.push(o) },
    tabs: { query: async () => [{ url: PL_URL, active: true }] },
    action: { openPopup: async () => {} },
  };
  globalThis.browser = browser;

  await new Function("browser", readFileSync(join(EXTENSION, "background.js"), "utf8"))(browser);
  await sleep(300);

  const dom = new JSDOM(readFileSync(join(EXTENSION, "popup", "popup.html"), "utf8"), {
    url: "moz-extension://harness/popup/popup.html",
    pretendToBeVisual: true,
  });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  await new Function("browser", "document", "window", readFileSync(join(EXTENSION, "popup", "popup.js"), "utf8"))(
    browser,
    dom.window.document,
    dom.window,
  );

  const $ = (selector) => dom.window.document.querySelector(selector);
  const visible = (selector) => {
    const node = $(selector);
    return node && !node.hidden;
  };

  async function ask(message) {
    for (const listener of profile.messageListeners) {
      const result = listener(message, {});
      if (result !== undefined) {
        return await result;
      }
    }
    return null;
  }

  // Start from an empty queue. Without this the checks below can be satisfied
  // by rows left over from a previous run, and the harness exits before the
  // download it just started has even been acknowledged.
  for (const listener of profile.messageListeners) {
    const reply = listener({ kind: "ui.clearFinished" }, {});
    if (reply !== undefined) {
      await reply;
    }
  }
  const emptied = await waitFor("queue cleared", async () => (await ask({ kind: "ui.state" }))?.jobs.length === 0, 20000);
  check("previous run's jobs cleared", emptied === true);

  // 1. playlist detection ----------------------------------------------------
  const info = await waitFor("playlist banner", () => (visible("#playlist") ? $("#playlist-info").textContent : null), 180000);
  check("playlist detected", /playlist/i.test(info), info);

  // Audio-only keeps the download small; it is also the ffmpeg path.
  const audioToggle = $("#audio-only");
  audioToggle.checked = true;
  audioToggle.dispatchEvent(new dom.window.Event("change"));

  // 2. entry picker ----------------------------------------------------------
  const chooseButton = [...dom.window.document.querySelectorAll(".seg")].find(
    (b) => b.dataset.mode === "select",
  );
  chooseButton.click();
  const entries = await waitFor(
    "entry list",
    () => {
      const rows = dom.window.document.querySelectorAll("#entry-list label");
      return rows.length > 1 ? rows : null;
    },
    240000,
  );
  check("entries listed", entries.length > WANTED, `${entries.length} entries`);
  check(
    "all entries preselected",
    $("#entry-count").textContent.startsWith(`${entries.length} of ${entries.length}`),
    $("#entry-count").textContent,
  );

  // Keep only the first N.
  $("#select-none").click();
  await sleep(50);
  const boxes = [...dom.window.document.querySelectorAll("#entry-list input[type=checkbox]")];
  for (let i = 0; i < WANTED; i += 1) {
    boxes[i].checked = true;
    boxes[i].dispatchEvent(new dom.window.Event("change"));
  }
  check("selection summary updates", $("#entry-count").textContent === `${WANTED} of ${entries.length} selected`, $("#entry-count").textContent);
  check("download button reflects the selection", /selected/.test($("#download-button").textContent), $("#download-button").textContent);

  // 3. start and watch the group fan out ------------------------------------
  $("#download-button").click();
  const groupRow = await waitFor(
    "group row",
    () => {
      const rows = [...dom.window.document.querySelectorAll(".job")];
      return rows.find((row) => row.querySelector(".job__disclose"));
    },
    60000,
  );
  check("playlist job rendered as a group", Boolean(groupRow), groupRow?.querySelector(".job__title")?.textContent);
  const groupState = await ask({ kind: "ui.state" });
  const groupJob = (groupState?.jobs || []).find((job) => job.isGroup);
  check("audio-only applied to the group", groupJob?.audioOnly === true, JSON.stringify(groupJob?.formatId));

  const childRows = await waitFor(
    "child rows",
    () => {
      const rows = [...dom.window.document.querySelectorAll(".job--child")];
      // Wait for the exact count: returning as soon as one appears would pass
      // this check before the second child had been rendered.
      return rows.length === WANTED ? rows : null;
    },
    120000,
  );
  check("each selected video got its own row", childRows.length === WANTED, `${childRows.length} rows`);

  // Wait for every row to reach a terminal state, not just "complete": a site
  // that rate-limits us (YouTube answers 403 after a few runs) is a legitimate
  // outcome, and should be reported as such rather than as a timeout.
  const settled = await waitFor(
    "playlist completion",
    () => {
      const rows = [...dom.window.document.querySelectorAll(".job")];
      const done = rows.filter((row) => /job--(complete|error|cancelled)/.test(row.className));
      return done.length >= WANTED + 1 ? done : null;
    },
    600000,
  );
  const completed = settled.filter((row) => row.className.includes("job--complete"));
  const failed = settled.filter((row) => /job--(error|cancelled)/.test(row.className));
  check("group and all entries completed", completed.length === WANTED + 1, `${completed.length} done, ${failed.length} failed`);
  if (failed.length) {
    console.log(
      "  note: the site refused some entries; the host reported them as errors:",
      failed
        .map((row) => row.querySelector(".job__title")?.textContent)
        .join(", "),
    );
  }
  // The host sends the group's state change (which is what paints the row)
  // before the separate `complete` frame that raises the notification, so the
  // notification can legitimately land a moment after the last row turns green.
  await waitFor("completion notification", () => profile.notifications.length > 0, 15000).catch(
    () => {},
  );
  if (process.env.HARNESS_TRACE) {
    const snapshot = await ask({ kind: "ui.state" });
    console.log("  settings:", JSON.stringify(snapshot.settings));
    console.log("  jobs:", snapshot.jobs.map((j) => `${j.jobId}:${j.status}:${j.isGroup ? "group" : "child"}`).join(" "));
  }
  check(
    "one notification for the whole playlist",
    profile.notifications.length === 1,
    `${profile.notifications.length}: ${profile.notifications.map((n) => n.message).join(", ")}`,
  );

  host.stdin.end();
  const failures = checks.filter((item) => !item.ok);
  console.log(`\n${checks.length - failures.length}/${checks.length} checks passed`);
  if (failures.length) {
    process.exitCode = 1;
  }
}

main().catch((error) => {
  console.error("harness error:", error);
  process.exitCode = 1;
});
