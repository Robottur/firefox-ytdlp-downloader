/**
 * Extension harness: runs the real background script and popup against the real
 * native host, with no browser involved.
 *
 * It exists because the interesting bugs in this project live in the seam
 * between three pieces that never run together in CI: popup.js, background.js
 * and the Python host. Here they do run together:
 *
 *   popup.js  --(runtime.sendMessage)-->  background.js
 *                                              |
 *                                       native port
 *                                              |
 *                                     stdin/stdout framing
 *                                              |
 *                                          ytdlp_host
 *
 * Usage:
 *   node tools/extension_harness.mjs [url]
 *
 * Requires jsdom (NODE_PATH, or a local install). Exits non-zero on failure.
 */

import { spawn, spawnSync } from "node:child_process";
import { createServer } from "node:http";
import { mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
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

/* ------------------------------------------------------- native messaging */

function frame(payload) {
  const body = Buffer.from(JSON.stringify(payload), "utf8");
  const header = Buffer.alloc(4);
  header.writeUInt32LE(body.length, 0); // little endian == this machine
  return Buffer.concat([header, body]);
}

class FakePort {
  constructor(child) {
    this.child = child;
    this.onMessage = { addListener: (fn) => (this._onMessage = fn) };
    this.onDisconnect = { addListener: (fn) => (this._onDisconnect = fn) };
    this.listeners = [];
    this.onMessage.addListener = (fn) => this.listeners.push(fn);
    this.error = null;
    this._buffer = Buffer.alloc(0);
    this._closed = false;
    child.stdout.on("data", (chunk) => this._consume(chunk));
    child.on("exit", (code) => {
      this._closed = true;
      this.error = { message: `host exited with code ${code}` };
      this._onDisconnect?.(this);
    });
  }

  _consume(chunk) {
    this._buffer = Buffer.concat([this._buffer, chunk]);
    for (;;) {
      if (this._buffer.length < 4) {
        return;
      }
      const length = this._buffer.readUInt32LE(0);
      if (this._buffer.length < 4 + length) {
        return;
      }
      const body = this._buffer.subarray(4, 4 + length).toString("utf8");
      this._buffer = this._buffer.subarray(4 + length);
      const message = JSON.parse(body);
      for (const listener of this.listeners) {
        listener(message);
      }
    }
  }

  postMessage(message) {
    if (this._closed) {
      throw new Error("port is closed");
    }
    this.child.stdin.write(frame(message));
  }

  disconnect() {
    this._closed = true;
    this.child.stdin.end();
  }
}

/* ------------------------------------------------------------ fake browser */

function makeBrowser({ hostProcess, profile }) {
  const storage = new Map();
  storage.set("settings", { defaultDownloadDir: profile.outputDir });
  const event = () => ({ addListener: () => {}, removeListener: () => {}, hasListener: () => false });

  const browser = {
    runtime: {
      id: "ytdlp-downloader-573c69b6aa75@local",
      lastError: null,
      getURL: (path) => `moz-extension://harness/${path}`,
      connectNative: (name) => {
        profile.connectedName = name;
        return new FakePort(hostProcess);
      },
      sendMessage: async (message) => {
        for (const listener of profile.messageListeners) {
          const result = listener(message, { id: "ytdlp-downloader-573c69b6aa75@local" });
          if (result !== undefined) {
            return result;
          }
        }
        return undefined;
      },
      openOptionsPage: () => profile.optionsOpened++,
      onMessage: {
        addListener: (fn) => profile.messageListeners.push(fn),
        removeListener: () => {},
        hasListener: (fn) => profile.messageListeners.includes(fn),
      },
      onInstalled: event(),
    },
    storage: {
      local: {
        get: async (keys) => {
          const result = {};
          for (const [key, value] of storage) {
            result[key] = value;
          }
          return result;
        },
        set: async (items) => {
          for (const [key, value] of Object.entries(items)) {
            storage.set(key, value);
            for (const fn of profile.storageListeners) {
              fn({ [key]: { newValue: value } }, "local");
            }
          }
        },
        remove: async (key) => storage.delete(key),
      },
      onChanged: {
        addListener: (fn) => profile.storageListeners.push(fn),
        removeListener: () => {},
      },
    },
    contextMenus: {
      removeAll: async () => profile.menus.length,
      create: (menu) => {
        profile.menus.push(menu);
        return menu.id;
      },
      onClicked: {
        addListener: (fn) => (profile.menuClickListener = fn),
        removeListener: () => {},
      },
    },
    notifications: {
      create: async (options) => {
        profile.notifications.push(options);
        return "n1";
      },
    },
    windows: {getAll: async () => [], create: async (options) => options},
    tabs: {
      query: async () => [{ url: profile.tabUrl, active: true }],
      sendMessage: async () => undefined,
      onUpdated: event(),
    },
    action: {
      onClicked: event(),
      openPopup: async () => {
        profile.popupOpened++;
        if (profile.popupOpenShouldFail) {
          throw new Error("popup may only be opened from a user gesture");
        }
      },
    },
  };
  return browser;
}

/* --------------------------------------------------------------- utilities */

const checks = [];
function check(name, condition, detail = "") {
  checks.push({ name, ok: Boolean(condition), detail });
  console.log(`${condition ? "  ok  " : "  FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(label, predicate, timeout = 120000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = await predicate(); // predicates may be async
    if (value) {
      return value;
    }
    await sleep(50);
  }
  throw new Error(`timed out waiting for ${label}`);
}

/* ------------------------------------------------- local media for testing */

/** A one-second clip served over HTTP: fast, offline, and a real download. */
async function serveSample() {
  const dir = mkdtempSync(join(tmpdir(), "ytdlp-harness-"));
  const clip = join(dir, "sample.mp4");
  const built = spawnSync(
    "ffmpeg",
    ["-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
     "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
     "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", clip],
    { stdio: "ignore" },
  );
  if (built.status !== 0) {
    throw new Error("ffmpeg is required to build the sample clip");
  }
  writeFileSync(
    join(dir, "index.html"),
    '<!doctype html><title>harness</title><video controls src="sample.mp4"></video>',
  );
  const requests = [];
  const server = createServer((request, response) => {
    requests.push({url: request.url, cookie: request.headers.cookie, authorization: request.headers.authorization});
    const path = request.url === "/" || request.url === "/index.html" ? "index.html" : request.url.slice(1);
    try {
      const body = readFileSync(join(dir, path));
      response.writeHead(200, { "content-type": path.endsWith(".html") ? "text/html" : "video/mp4" });
      response.end(body);
    } catch {
      response.writeHead(404).end("not found");
    }
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const { port } = server.address();
  return { requests, url: `http://127.0.0.1:${port}/index.html`, dir, close: () => server.close() };
}

/* -------------------------------------------------------------------- main */

async function main() {
  const workdir = mkdtempSync(join(tmpdir(), "ytdlp-extension-test-"));
  const sample = process.argv[2] ? null : await serveSample();
  const url = process.argv[2] || sample.url;
  const profile = {
    messageListeners: [],
    storageListeners: [],
    menus: [],
    notifications: [],
    popupOpened: 0,
    optionsOpened: 0,
    tabUrl: url,
    outputDir: join(workdir, "downloads"),
  };

  const hostProcess = spawn(process.env.PYTHON || "python3", ["-X", "utf8", "-m", "ytdlp_host.main", "--stdio"], {
    cwd: HOST_DIR,
    stdio: ["pipe", "pipe", "inherit"],
    env: { ...process.env, YTDLP_HOST_STATE_DIR: join(workdir, "state") },
  });

  const browser = makeBrowser({ hostProcess, profile });
  globalThis.browser = browser;

  console.log("\n== background script ==");
  const backgroundSource = readFileSync(join(EXTENSION, "background.js"), "utf8");
  // The background script is an ES module with no imports; evaluating it in a
  // function scope is equivalent for our purposes.
  const dispose = await new Function("browser", `${backgroundSource}\nreturn () => { clearTimeout(reconnectTimer); for (const item of inflight.values()) clearTimeout(item.timer); };`)(browser);
  await sleep(200);

  check("context menu registered", profile.menus.length === 1, `${profile.menus.length} menus`);
  check(
    "menu contexts cover links, media and pages",
    profile.menus.map((m) => m.contexts.join("+")).join(",") === "all",
    profile.menus.map((m) => m.contexts.join("+")).join(","),
  );
  check("native host connected", profile.connectedName === "ytdlp_downloader.host", profile.connectedName);

  const ready = await waitFor("host_ready", async () => (await askState())?.host, 20000);
  check("host self-check available", Boolean(ready), `yt-dlp ${ready?.ytDlpVersion}, ffmpeg=${ready?.ffmpegFound}`);

  async function askState() {
    for (const listener of profile.messageListeners) {
      const reply = listener({ kind: "ui.state" }, {});
      if (reply !== undefined) {
        return await reply;
      }
    }
    return null;
  }

  // Start from a clean queue: rows from earlier runs are real history, not
  // what this run produced. The host answers clear_finished with a fresh job
  // list, so waiting on the mirror proves the resync happened.
  for (const listener of profile.messageListeners) {
    const reply = listener({ kind: "ui.clearFinished" }, {});
    if (reply !== undefined) {
      await reply;
    }
  }
  const emptied = await waitFor("queue cleared", async () => {
    const snapshot = await askState();
    return snapshot?.jobs.length === 0;
  }, 20000);
  check("clear-finished resyncs the extension mirror", emptied === true);

  if (sample) check("idle startup does not contact the source", sample.requests.length === 0);
  profile.menuClickListener({pageUrl: url}, {url, incognito: false});
  await sleep(20);

  console.log("\n== popup ==");
  const dom = new JSDOM(readFileSync(join(EXTENSION, "popup", "popup.html"), "utf8"), {
    url: "moz-extension://harness/popup/popup.html",
    pretendToBeVisual: true,
  });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  dom.window.close = () => {};
  // Route browser.runtime.sendMessage from the popup into the background script.
  const backgroundSend = browser.runtime.sendMessage;
  browser.runtime.sendMessage = async (message) => backgroundSend(message);

  const popupSource = readFileSync(join(EXTENSION, "popup", "popup.js"), "utf8");
  await new Function("browser", "document", "window", popupSource)(browser, dom.window.document, dom.window);

  const $ = (selector) => dom.window.document.querySelector(selector);
  const visible = (selector) => {
    const node = $(selector);
    return node && !node.hidden;
  };

  // The context-menu action selected this source; wait for metadata.
  const title = await waitFor("probe result", () => {
    const node = $("#title");
    return node && node.textContent ? node.textContent : null;
  }, 180000);
  check("popup probed the explicitly selected source", Boolean(title), title.slice(0, 60));
  check("media panel visible", visible("#media"));
  check("options panel visible", visible("#options"));
  const formatCount = $("#format-select").options.length;
  const firstFormat = $("#format-select").options[0]?.textContent || "";
  check("format dropdown has a real label", formatCount >= 1 && firstFormat.length > 3, `${formatCount}: ${firstFormat}`);
  if (process.argv[2]) {
    // A real site must offer a list, not just one synthetic "best" entry.
    check("format dropdown populated from real formats", formatCount > 3, `${formatCount} options`);
  }
  check("download button enabled", !$("#download-button").disabled);
  const outputDir = $("#output-dir").value;
  check("output dir shown", (outputDir || "").length > 0, outputDir);

  // Subtitles section (this video has none, so it must stay hidden).
  check("subtitle section hidden when there are none", !visible("#subtitle-details"));

  console.log("\n== queue ==");
  $("#download-button").click();
  await sleep(400);
  const jobTitle = await waitFor("queue row", () => {
    const node = $(".job__title");
    return node && node.textContent ? node.textContent : null;
  }, 30000);
  check("job appears in the queue", Boolean(jobTitle), jobTitle.slice(0, 50));

  const rowForJob = (state_) =>
    [...dom.window.document.querySelectorAll(".job")].find(
      (row) => row.querySelector(".job__title")?.textContent === jobTitle,
    );
  const completed = await waitFor("job completion", () => {
    const row = rowForJob();
    return row && row.className.includes("job--complete") ? row : null;
  }, 300000);
  check("job reached complete state", Boolean(completed), completed?.className);
  const fileName = completed?.querySelector(".job__file")?.textContent || "";
  check("finished job shows a filename", fileName.length > 0 && fileName.endsWith(".mp4"), fileName);
  check(
    "download landed in the download folder",
    statSync(join(outputDir, fileName)).size > 0,
    join(outputDir, fileName),
  );
  check(
    "completion notification raised",
    profile.notifications.length >= 1,
    `${profile.notifications.length}: ${profile.notifications.map((n) => n.title + "/" + n.message).join(", ")}`,
  );

  console.log("\n== background state ==");
  const finalState = await askState();
  check("background mirrors the finished job", finalState.jobs.length >= 1, `${finalState.jobs.length} jobs`);
  check(
    "finished job has a filepath",
    finalState.jobs.some((job) => job.status === "complete" && job.filename),
  );
  check("settings came from the host", Boolean(finalState.settings.defaultDownloadDir), finalState.settings.defaultDownloadDir);

  console.log("\n== error path ==");
  const badReply = await (async () => {
    for (const listener of profile.messageListeners) {
      const reply = listener({ kind: "ui.probe", url: "file:///etc/passwd" }, {});
      if (reply !== undefined) {
        return await reply;
      }
    }
    return null;
  })();
  check("host rejects non-http URLs", Boolean(badReply?.__error), badReply?.__error?.message);

  console.log("\n== teardown ==");
  hostProcess.stdin.end();
  const exitCode = await new Promise((resolve) => {
    const timer = setTimeout(() => {
      hostProcess.kill("SIGKILL");
      resolve("killed");
    }, 8000);
    hostProcess.on("exit", (code) => {
      clearTimeout(timer);
      resolve(code);
    });
  });
  check("host exits cleanly when the pipe closes", exitCode === 0, `exit ${exitCode}`);
  dispose();

  sample?.close();
  const failed = checks.filter((item) => !item.ok);
  console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
  if (failed.length) {
    process.exitCode = 1;
  }
}

main().catch((error) => {
  console.error("\nharness error:", error);
  process.exitCode = 1;
});
