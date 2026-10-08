import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const source = (path) => readFileSync(new URL(`../extension/${path}`, import.meta.url), "utf8");
const tick = () => new Promise((resolve) => setImmediate(resolve));

async function background(saved = {}, migrated = false) {
  const sent = [], listeners = [], menus = [], windows = [], focused = [], broadcasts = [];
  const events = {};
  const cookieReads = [];
  let permission = true;
  const stored = { settings: saved, mp4DefaultMigrated: migrated };
  const port = { onMessage: {addListener() {}}, onDisconnect: {addListener() {}},
    postMessage(message) { sent.push(message); } };
  const browser = {
    permissions: {contains: async () => permission},
    cookies: {getAllCookieStores: async () => [{id: "firefox-default"}, {id: "firefox-container-1"}, {id: "firefox-private"}],
      getAll: async details => {cookieReads.push(details); return [{name:"session",value:"secret",domain:".example.test",path:"/",secure:true,session:true}];}},
    storage: {local: {get: async () => stored, set: async (values) => Object.assign(stored, values)}, onChanged: {addListener() {}}},
    runtime: {connectNative: () => port, sendMessage: async (message) => broadcasts.push(message), onMessage: {addListener: (fn) => listeners.push(fn)},
      onInstalled: {addListener() {}}, getURL: (s) => s},
    contextMenus: {removeAll: async () => {}, create: async (menu) => menus.push(menu), onClicked: {addListener(fn) { events.menu = fn; }}},
    action: {onClicked: {addListener(fn) { events.action = fn; }}},
    windows: {getAll: async () => windows,
      create: async (options) => { const win = {...options, id: windows.length + 1, tabs: [{url: options.url}]}; windows.push(win); return win; },
      update: async (id) => { focused.push(id); return windows.find((win) => win.id === id); }},
    notifications: {create: async () => {}}, tabs: {query: async () => []}
  };
  const api = new Function("browser", `${source("background.js")}\nreturn {state, onHostMessage, saveSettings, orderedJobs, urlFromMenu, openDownloader, chooseFolder, chooseSaveFile,
    dispose() { clearTimeout(reconnectTimer); for (const {timer} of inflight.values()) clearTimeout(timer); } };`)(browser);
  await tick();
  return {...api, events, cookieReads, setPermission(value) { permission = value; }, sent, stored, listeners, menus, windows, focused, broadcasts};
}

test("upgrades migrate the previous automatic default to MP4 once", async (t) => {
  const bg = await background({defaultFormat: "bestvideo+bestaudio/best"}); t.after(bg.dispose);
  assert.equal(bg.state.settings.defaultFormat, "mp4");
  assert.equal(bg.stored.settings.defaultFormat, "mp4");
  assert.equal(bg.stored.mp4DefaultMigrated, true);
  const custom = await background({defaultFormat: "bestvideo+bestaudio/best"}, true); t.after(custom.dispose);
  assert.equal(custom.state.settings.defaultFormat, "bestvideo+bestaudio/best");
});

test("toolbar and context menu use a persistent reusable window", async (t) => {
  const manifest = JSON.parse(source("manifest.json"));
  assert.equal(manifest.action.default_popup, undefined);
  const bg = await background(); t.after(bg.dispose);
  assert.deepEqual(bg.menus.map((m) => m.contexts), [["all"]]);
  await bg.openDownloader("https://youtube.test/watch");
  assert.equal(bg.windows.length, 1);
  assert.equal(bg.windows[0].type, "popup");
  await bg.openDownloader("https://x.test/status/123");
  assert.equal(bg.windows.length, 1);
  assert.deepEqual(bg.focused, [1]);
  assert.equal(bg.broadcasts.at(-1).message.url, "https://x.test/status/123");
});

test("background saves a picked folder even with no UI listening", async (t) => {
  const bg = await background(); t.after(bg.dispose);
  const first = bg.chooseFolder("/before"), second = bg.chooseFolder("/before");
  assert.equal(bg.sent.length, 1, "share an already-open picker");
  bg.onHostMessage({type: "folder_picked", requestId: bg.sent[0].requestId, path: "/chosen/folder"});
  await Promise.all([first, second]);
  assert.equal(bg.stored.settings.defaultDownloadDir, "/chosen/folder");
  const cancel = bg.chooseFolder("/chosen/folder");
  bg.onHostMessage({type: "folder_picked", requestId: bg.sent[1].requestId, path: null});
  await cancel;
  assert.equal(bg.stored.settings.defaultDownloadDir, "/chosen/folder");
});

test("reconnection preserves saved preferences over host defaults", async (t) => {
  const bg = await background({defaultDownloadDir: "/custom", defaultFormat: "best", maxConcurrentDownloads: 5, notifyOnComplete: false});
  t.after(bg.dispose);
  bg.onHostMessage({type: "host_ready", settings: {defaultDownloadDir: "/Downloads", maxConcurrentDownloads: 2, notifyOnComplete: true}});
  assert.equal(bg.state.settings.defaultDownloadDir, "/custom");
  assert.equal(bg.state.settings.maxConcurrentDownloads, 5);
  assert.equal(bg.state.settings.notifyOnComplete, false);
  assert.equal(bg.sent[0].settings.defaultFormat, "best");
});

test("invalid settings are rejected without persisting a false success", async (t) => {
  const bg = await background({defaultFormat: "best"}); t.after(bg.dispose);
  bg.state.connected = true;
  const promise = bg.saveSettings({defaultFormat: "bad"});
  bg.onHostMessage({type: "error", requestId: bg.sent[0].requestId, message: "Invalid selector"});
  await assert.rejects(promise, /Invalid selector/);
  assert.equal(bg.stored.settings.defaultFormat, "best");
});

test("queue snapshots accumulate chunks and retain orphaned children", async (t) => {
  const bg = await background(); t.after(bg.dispose);
  bg.onHostMessage({type: "jobs", append: false, final: false, jobs: [{jobId: "child", groupId: "parent"}]});
  assert.equal(bg.orderedJobs().length, 1);
  bg.onHostMessage({type: "jobs", append: true, final: true, jobs: [{jobId: "parent"}]});
  assert.deepEqual(bg.orderedJobs().map((j) => j.jobId), ["parent", "child"]);
});

test("media context menu resolves the web page, link menu uses the link", async (t) => {
  const bg = await background(); t.after(bg.dispose);
  assert.equal(bg.urlFromMenu({srcUrl: "https://cdn/temporary", pageUrl: "https://x/video"}, {}), "https://x/video");
  assert.equal(bg.urlFromMenu({linkUrl: "https://x/link", pageUrl: "https://x/page"}, {}), "https://x/link");
  assert.equal(bg.urlFromMenu({mediaType: "video", linkUrl: "https://x/advert", frameUrl: "https://x/embed", pageUrl: "https://x/page"}, {}), "https://x/embed");
});

async function popup(respond = () => undefined, permissionResult = true) {
  const dom = new JSDOM(source("popup/popup.html"), {url: "https://extension.test"});
  const messages = [], hostListeners = [];
  const browser = {permissions: {request: async request => { messages.push({kind:"permission", ...request}); return permissionResult; }}, runtime: {sendMessage: async (message) => {
    messages.push(message);
    const reply = respond(message);
    if (reply !== undefined) return reply;
    if (message.kind === "ui.state") return {connected: true, host: {ffmpegFound: true}, settings: {}, jobs: []};
    if (message.kind === "ui.activeTabUrl") return {url: ""};
    return {};
  }, onMessage: {addListener(fn) { hostListeners.push(fn); }}, openOptionsPage() {}}};
  const api = new Function("browser", "document", "window", `${source("popup/popup.js")}\nreturn {state, renderOptions, sortJobs, renderQueue, renderPlaylist, startDownload, chooseFolder, enableSession, releaseSession};`)(browser, dom.window.document, dom.window);
  await tick();
  return {...api, dom, messages, emitHost(event) { for (const fn of hostListeners) fn({kind:"host", message:event}); }, $: (selector) => dom.window.document.querySelector(selector)};
}

test("Save As sends a title and MP4 extension, retains the chosen filename and saves its directory", async (t) => {
  const bg = await background(); t.after(bg.dispose);
  const saving = bg.chooseSaveFile({initialDir: "/old", filename: "My clip", extension: "mp4"});
  assert.equal(bg.sent[0].type, "pick_save_file");
  bg.onHostMessage({type: "save_file_picked", requestId: bg.sent[0].requestId,
    path: "/new/renamed.mp4", outputDir: "/new", filename: "renamed.mp4", overwrite: false});
  const reply = await saving;
  assert.equal(bg.stored.settings.defaultDownloadDir, "/new");
  const ui = await popup((message) => message.kind === "ui.saveAs" ? reply : undefined);
  t.after(() => ui.dom.window.close());
  Object.assign(ui.state, {probeStatus: "ready", probe: {url: "https://x/video", title: "My clip", formats: []}});
  ui.renderOptions();
  await ui.chooseFolder();
  const request = ui.messages.find((m) => m.kind === "ui.saveAs");
  assert.equal(request.filename, "My clip");
  assert.equal(request.extension, "mp4");
  assert.equal(ui.$("#output-filename").value, "renamed.mp4");
  await ui.startDownload();
  assert.equal(ui.messages.find((m) => m.kind === "ui.download").outputFilename, "renamed.mp4");
});

test("cancelled Save As keeps filename and directory, and playlist uses a folder browser", async (t) => {
  const ui = await popup((m) => ["ui.saveAs", "ui.pickFolder"].includes(m.kind) ? {cancelled: true} : undefined);
  t.after(() => ui.dom.window.close());
  Object.assign(ui.state, {probeStatus: "ready", outputDir: "/old", outputFilename: "original.mp4",
    probe: {url: "https://x/video", formats: []}});
  ui.renderOptions(); await ui.chooseFolder();
  assert.equal(ui.state.outputDir, "/old");
  assert.equal(ui.state.outputFilename, "original.mp4");
  ui.state.probe.isPlaylist = true; ui.state.playlistMode = "all";
  ui.renderOptions(); await ui.chooseFolder();
  assert.equal(ui.$("#filename-field").hidden, true);
  assert.equal(ui.messages.at(-1).kind, "ui.pickFolder");
});

test("changing format updates the filename extension and revokes replacement confirmation", async (t) => {
  const ui = await popup(); t.after(() => ui.dom.window.close());
  Object.assign(ui.state, {probeStatus: "ready", outputFilename: "My clip.mp4", overwrite: true,
    probe: {url: "https://x/video", formats: []}});
  ui.renderOptions();
  ui.$("#audio-only").click();
  assert.equal(ui.$("#output-filename").value, "My clip.mp3");
  assert.equal(ui.state.overwrite, false);
});

test("video-only choices include audio and MP3 retains the selected stream", async (t) => {
  const ui = await popup(); t.after(() => ui.dom.window.close());
  ui.state.probeStatus = "ready";
  ui.state.probe = {url: "https://x/video", formats: [
    {formatId: "137", kind: "video only", label: "1080p · video only", height: 1080},
    {formatId: "140", kind: "audio", isAudio: true, label: "Audio m4a"}
  ]};
  ui.renderOptions();
  assert.ok([...ui.$("#format-select").options].some((o) => o.value === "137+bestaudio/best"));
  ui.state.audioOnly = true; ui.state.formatId = "140"; ui.renderOptions();
  assert.equal(ui.$("#format-select").value, "140");
  assert.equal(ui.$("#format-select").options.length, 2);
  ui.dom.window.close = () => {};
  await ui.startDownload();
  assert.equal(ui.messages.find((m) => m.kind === "ui.download").formatId, "140");
});

test("no ffmpeg hides merge choices and disables MP3", async (t) => {
  const ui = await popup(); t.after(() => ui.dom.window.close());
  Object.assign(ui.state, {probeStatus: "ready", host: {ffmpegFound: false},
    probe: {formats: [{formatId: "137", kind: "video only", label: "silent video"}]}});
  ui.renderOptions();
  assert.equal(ui.$("#audio-only").disabled, true);
  assert.deepEqual([...ui.$("#format-select").options].map((o) => o.value), ["mp4"]);
});

test("MP4 is selected by default and starting a download keeps the window open", async (t) => {
  const ui = await popup(); t.after(() => ui.dom.window.close());
  Object.assign(ui.state, {probeStatus: "ready", probe: {url: "https://x/video", formats: []}});
  ui.renderOptions();
  assert.equal(ui.$("#format-select").value, "mp4");
  assert.match(ui.$("#format-select").selectedOptions[0].textContent, /MP4/);
  let closed = false; const close = ui.dom.window.close;
  ui.dom.window.close = () => {closed = true;};
  await ui.startDownload();
  assert.equal(closed, false);
  assert.equal(ui.messages.find((m) => m.kind === "ui.download").formatId, "mp4");
  ui.dom.window.close = close;
});

test("collapsing a playlist never removes children from popup state", async (t) => {
  const ui = await popup(); t.after(() => ui.dom.window.close());
  ui.state.jobs = [{jobId: "g", isGroup: true}, {jobId: "c", groupId: "g"}];
  ui.state.collapsed.add("g"); ui.sortJobs(); ui.renderQueue();
  assert.equal(ui.state.jobs.length, 2);
  assert.equal(ui.$("#queue").children.length, 1);
  ui.state.collapsed.clear(); ui.renderQueue();
  assert.equal(ui.$("#queue").children.length, 2);
});

test("progress updates preserve unchanged queue rows", async (t) => {
  const ui = await popup(); t.after(() => ui.dom.window.close());
  ui.state.jobs = [{jobId: "a", status: "downloading"}, {jobId: "b", status: "complete"}];
  ui.renderQueue();
  const unchanged = ui.$("#queue").lastChild;
  ui.state.jobs[0].percent = 25; ui.renderQueue();
  assert.equal(ui.$("#queue").lastChild, unchanged);
});

test("caption choices use the correct manual/automatic language variant", async (t) => {
  const ui = await popup(); t.after(() => ui.dom.window.close());
  Object.assign(ui.state, {probeStatus: "ready", probe: {formats: [], subtitles: [
    {lang: "en", name: "English", manual: true, auto: true,
     variants: [{lang: "en", manual: true}, {lang: "en-abc12345678", manual: false}]}
  ]}});
  ui.renderOptions();
  const boxes = ui.dom.window.document.querySelectorAll("#subtitle-list input");
  boxes[1].click();
  assert.ok(ui.state.subtitles.has("en-abc12345678:auto"));
});

test("history renders on opening Settings and filters titles without interpreting HTML", async (t) => {
  const dom = new JSDOM(source("options/options.html"));
  t.after(() => dom.window.close());
  const jobs = [
    {jobId: "a", status: "complete", title: "<img src=x> Clip", url: "https://x/clip", updatedAt: 1},
    {jobId: "b", status: "error", title: "Trailer", url: "https://x/trailer", updatedAt: 2}
  ];
  const browser = {runtime: {sendMessage: async () => ({connected: false, settings: {}, jobs}), onMessage: {addListener() {}}}};
  new Function("browser", "document", source("options/options.js"))(browser, dom.window.document);
  await tick();
  assert.equal(dom.window.document.querySelectorAll("#history li").length, 2);
  assert.equal(dom.window.document.querySelector("#history img"), null);
  const search = dom.window.document.querySelector("#history-search");
  search.value = "trailer"; search.dispatchEvent(new dom.window.Event("input"));
  assert.equal(dom.window.document.querySelectorAll("#history li").length, 1);
  assert.equal(dom.window.document.querySelector("#history strong").textContent, "Trailer");
});


test("public download permissions exclude broad tabs and private browsing", () => {
  const manifest = JSON.parse(source("manifest.json"));
  assert.equal(manifest.permissions.includes("tabs"), false);
  assert.equal(manifest.incognito, "not_allowed");
});

test("private actions cannot open a downloader or leak a pending URL", async (t) => {
  const bg = await background(); t.after(bg.dispose);
  bg.events.action({url: "https://private.test/secret", incognito: true});
  bg.events.menu({pageUrl: "https://private.test/secret"}, {incognito: true});
  await tick();
  assert.equal(bg.windows.length, 0);
  assert.equal(bg.state.pendingUrl, null);
});

test("private UI messages are rejected before exposing state or contacting the host", async (t) => {
  const bg = await background(); t.after(bg.dispose);
  for (const kind of ["ui.state", "ui.probe", "ui.download", "ui.consumePending"]) {
    const result = await bg.listeners[0]({kind, url: "https://private.test"}, {tab: {incognito: true}});
    assert.equal(result.__error.code, "private_window_disabled");
  }
  assert.equal(bg.sent.length, 0);
});

test("manual download window startup does not discover or probe a browsing tab", async (t) => {
  const ui = await popup((m) => m.kind === "ui.activeTabUrl" ? {url: "https://unselected.test"} : undefined);
  t.after(() => ui.dom.window.close());
  assert.equal(ui.messages.some(m => ["ui.activeTabUrl", "ui.probe"].includes(m.kind)), false);
  assert.match(ui.$("#privacy-notice").textContent, /IP address/);
  assert.match(ui.$("#privacy-notice").textContent, /proxy/);
});


test("cookie access requires consent and uses only the chosen container and URL", async (t) => {
  const bg = await background(); t.after(bg.dispose);
  bg.setPermission(false);
  const message = {kind:"ui.authorizeSession", url:"https://example.test/watch", storeId:"firefox-container-1"};
  assert.ok((await bg.listeners[0](message, {})).__error);
  assert.equal(bg.cookieReads.length, 0);
  bg.setPermission(true);
  const request = bg.listeners[0](message, {});
  await tick();
  assert.deepEqual(bg.cookieReads, [{url:message.url,storeId:"firefox-container-1",partitionKey:{}}]);
  assert.equal(bg.sent[0].type, "authorize_session");
  bg.onHostMessage({type:"session_authorized",requestId:bg.sent[0].requestId,sessionToken:"opaque"});
  assert.equal((await request).sessionToken, "opaque");
  assert.equal(JSON.stringify(bg.stored).includes("secret"), false);
  assert.equal(bg.broadcasts.some(m => JSON.stringify(m).includes("secret")), false);
  assert.equal(bg.broadcasts.some(m => JSON.stringify(m).includes("opaque")), false);
  assert.ok((await bg.listeners[0]({...message,storeId:"firefox-private"}, {})).__error);
});


test("signed-in UI grants one operation and releases consent after download", async (t) => {
  const url = "https://example.test/watch";
  const ui = await popup(m => {
    if (m.kind === "ui.state") return {connected:true,settings:{},jobs:[],sourceContext:{url,storeId:"firefox-container-1"}};
    if (m.kind === "ui.cookieStores") return {stores:[{id:"firefox-container-1"}]};
    if (m.kind === "ui.authorizeSession") return {sessionToken:"opaque"};
    if (m.kind === "ui.probe") return {url,formats:[],title:"Signed in"};
  });
  t.after(() => ui.dom.window.close());
  ui.$("#url-input").value = url;
  await ui.enableSession();
  assert.deepEqual(ui.messages.find(m => m.kind === "permission").origins, ["https://example.test/*"]);
  assert.equal(ui.messages.find(m => m.kind === "ui.authorizeSession").storeId, "firefox-container-1");
  assert.equal(ui.messages.find(m => m.kind === "ui.probe").sessionToken, "opaque");
  await ui.startDownload();
  assert.equal(ui.messages.find(m => m.kind === "ui.download").sessionToken, "opaque");
  assert.equal(ui.state.sessionToken, null);
  assert.equal(ui.state.probe, null);
});

test("YouTube short links request YouTube cookies and keep the source container", async (t) => {
  const short = "https://youtu.be/VIDEO_ID_01?t=30";
  const canonical = "https://www.youtube.com/watch?t=30&v=VIDEO_ID_01";
  const ui = await popup(m => {
    if (m.kind === "ui.state") return {connected:true,settings:{},jobs:[],sourceContext:{url:short,storeId:"firefox-container-1"}};
    if (m.kind === "ui.cookieStores") return {stores:[{id:"firefox-container-1"}]};
    if (m.kind === "ui.authorizeSession") return {sessionToken:"youtube-consent"};
    if (m.kind === "ui.probe") return {url:m.url,formats:[],title:"Member video"};
  });
  t.after(() => ui.dom.window.close());
  ui.$("#url-input").value = short;
  await ui.enableSession();
  assert.deepEqual(ui.messages.find(m => m.kind === "permission").origins, ["https://www.youtube.com/*"]);
  const grant = ui.messages.find(m => m.kind === "ui.authorizeSession");
  assert.equal(grant.url, canonical);
  assert.equal(grant.storeId, "firefox-container-1");
  assert.equal(ui.$("#url-input").value, canonical);
  await ui.startDownload();
  assert.equal(ui.messages.find(m => m.kind === "ui.download").url, canonical);
  assert.equal(ui.messages.find(m => m.kind === "ui.download").sessionToken, "youtube-consent");
});

test("declined cookie permission never authorizes or probes a signed-in source", async (t) => {
  const ui = await popup(() => undefined, false); t.after(() => ui.dom.window.close());
  ui.$("#url-input").value = "https://example.test/watch";
  await ui.enableSession();
  assert.equal(ui.messages.some(m => m.kind === "ui.authorizeSession" || m.kind === "ui.probe"), false);
  assert.match(ui.$("#status-line").textContent, /declined/);
});


test("selecting the same source from a new container clears the previous account context", async (t) => {
  const url = "https://example.test/watch";
  const ui = await popup(m => {
    if (m.kind === "ui.state") return {connected:true,settings:{},jobs:[],sourceContext:{url,storeId:"firefox-container-1"}};
    if (m.kind === "ui.cookieStores") return {stores:[{id:"firefox-container-1"},{id:"firefox-container-2"}]};
    if (m.kind === "ui.authorizeSession") return {sessionToken:"opaque"};
    if (m.kind === "ui.probe") return {url,formats:[],title:"Source"};
  });
  t.after(() => ui.dom.window.close());
  ui.$("#url-input").value = url;
  await ui.enableSession();
  const before = ui.messages.filter(m => m.kind === "ui.probe").length;
  ui.emitHost({type:"pending",url,sourceContext:{url,storeId:"firefox-container-2"}});
  await tick();
  assert.equal(ui.$("#session-store").value, "");
  assert.equal(ui.messages.filter(m => m.kind === "ui.probe").length, before + 1);
  assert.equal(ui.messages.filter(m => m.kind === "ui.probe").at(-1).sessionToken, null);
  await ui.enableSession();
  assert.equal(ui.messages.filter(m => m.kind === "ui.authorizeSession").at(-1).storeId, "firefox-container-2");
});


test("remote thumbnails cannot issue independent browser requests", async (t) => {
  const ui = await popup(m => m.kind === "ui.state" ? {connected:true,settings:{},jobs:[],pendingUrl:"https://example.test/video"} :
    m.kind === "ui.probe" ? {url:"https://example.test/video",thumbnail:"https://tracker.test/image",formats:[]} : undefined);
  t.after(() => ui.dom.window.close()); await tick();
  assert.equal(ui.$("#thumb").getAttribute("src"), null);
});
