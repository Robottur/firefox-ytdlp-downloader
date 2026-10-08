/** Real Firefox/native-messaging smoke test, using an isolated profile and state.
 * Requires the Linux/Windows host registration to have been installed already.
 * The temporary extension copy adds test-only drivers and capture permission.
 */
import { spawn, spawnSync } from "node:child_process";
import { createServer } from "node:http";
import { cpSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync, appendFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";

const root = resolve(import.meta.dirname, "..");
const testFolder = process.argv.includes("--folder");
const dir = mkdtempSync(join(tmpdir(), "ytdlp-firefox-test-"));
const extension = join(dir, "extension");
cpSync(join(root, "extension"), extension, {recursive: true});
mkdirSync(join(dir, "profile"));
const chosenFolder = join(dir, "chosen folder");
const chosenFilename = "Renamed from Save As.mp4";
mkdirSync(chosenFolder);
const built = spawnSync("ffmpeg", ["-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12:duration=2",
  "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", join(dir, "sample.mp4")]);
if (built.status) throw new Error(built.stderr.toString());
const clip = readFileSync(join(dir, "sample.mp4"));
let finish;
const result = new Promise((resolve) => { finish = resolve; });
const server = createServer(async (req, res) => {
  if (testFolder && req.url === "/context-menu") {
    for (let i = 0; i < 150; i++) {
      const found = spawnSync("xdotool", ["search", "--onlyvisible", "--name", "Firefox integration clip"]);
      const id = found.stdout.toString().trim().split("\n")[0];
      if (id) {
        spawnSync("xdotool", ["windowfocus", id, "mousemove", "--window", id, "160", "160", "keydown", "Shift", "click", "3", "keyup", "Shift"]);
        await new Promise(r => setTimeout(r, 400));
        const clicked = spawnSync("xdotool", ["key", "y"]);
        res.writeHead(clicked.status ? 500 : 200).end(clicked.stderr.toString()); return;
      }
      await new Promise(r => setTimeout(r, 100));
    }
    res.writeHead(500).end("Video page did not appear"); return;
  }
  if (testFolder && req.url === "/choose-folder") {
    for (let i = 0; i < 150; i++) {
      const found = spawnSync("xdotool", ["search", "--onlyvisible", "--name", "^Save As"]);
      const id = found.stdout.toString().trim().split("\n")[0];
      if (id) {
        mkdirSync(join(root, "test-results"), {recursive: true});
        spawnSync("magick", ["import", "-window", id, join(root, "test-results/save-as-dialog.png")]);
        spawnSync("magick", ["import", "-window", "root", join(root, "test-results/save-as-desktop.png")]);
        spawnSync("xdotool", ["windowfocus", id, "key", "--clearmodifiers", "ctrl+a"]);
        spawnSync("xdotool", ["type", "--clearmodifiers", "--", chosenFilename]);
        const accepted = spawnSync("xdotool", ["key", "--clearmodifiers", "Return"]);
        res.writeHead(accepted.status ? 500 : 200).end(accepted.stderr.toString()); return;
      }
      await new Promise(r => setTimeout(r, 100));
    }
    res.writeHead(500).end("Save As dialog did not appear"); return;
  }
  if (req.method === "POST" && req.url === "/result") {
    let body = ""; for await (const chunk of req) body += chunk;
    res.writeHead(200).end("ok"); finish(JSON.parse(body)); return;
  }
  if (req.url === "/sample.mp4") { res.writeHead(200, {"content-type": "video/mp4"}); res.end(clip); return; }
  res.writeHead(200, {"content-type": "text/html"});
  res.end('<title>Firefox integration clip</title><video src="sample.mp4" controls></video>');
});
await new Promise((resolve, reject) => {server.on("error", reject); server.listen(0, "127.0.0.1", resolve);});
const origin = `http://127.0.0.1:${server.address().port}`;
const manifest = JSON.parse(readFileSync(join(extension, "manifest.json")));
manifest.host_permissions = ["<all_urls>"]; // test-only screenshot and synthetic cookie permissions
manifest.permissions.push("cookies", "contextualIdentities");
manifest.optional_permissions = [];
writeFileSync(join(extension, "manifest.json"), JSON.stringify(manifest));
const waitCode = `async function waitFor(check) { for (let i = 0; i < 600; i++) { if (await check()) return; await new Promise(r => setTimeout(r, 100)); } throw new Error("Timeout waiting for Firefox UI"); }`;
appendFileSync(join(extension, "background.js"), `\n${waitCode}
(async () => {
  try {
    await waitFor(() => state.connected);
    // Test only: synthetic cookie in this disposable Firefox profile. No site
    // navigation or real-account cookies are involved.
    const identity = await browser.contextualIdentities.create({name: "Fixture account", color: "blue", icon: "fingerprint"});
    const containerTab = await browser.tabs.create({url: "about:blank", cookieStoreId: identity.cookieStoreId, active: false});
    await browser.cookies.set({url: "https://example.test/", name: "member", value: "synthetic-only", secure: true, storeId: identity.cookieStoreId});
    const grant = await authorizeSession({url: "https://example.test/watch", storeId: identity.cookieStoreId});
    if (!grant.sessionToken) throw new Error("Synthetic cookie authorization failed");
    await send("release_session", {sessionToken: grant.sessionToken});
    await browser.tabs.remove(containerTab.id);
    await browser.contextualIdentities.remove(identity.cookieStoreId);
    await saveSettings({defaultDownloadDir: ${JSON.stringify(join(dir, "downloads"))}});
    if (${testFolder}) {
      const tab = await browser.tabs.create({url: ${JSON.stringify(origin + "/index.html")}, active: true});
      await waitFor(async () => (await browser.tabs.get(tab.id)).status === "complete");
      const clicked = await fetch(${JSON.stringify(origin + "/context-menu")});
      if (!clicked.ok) throw new Error(await clicked.text());
    } else {
      await openDownloader(${JSON.stringify(origin + "/index.html")});
    }
  } catch (error) { await fetch(${JSON.stringify(origin + "/result")}, {method: "POST", body: JSON.stringify({error: error.message, linkError: state.linkError})}); }
})();`);
appendFileSync(join(extension, "popup/popup.html"), '<script type="module" src="test-ui.js"></script>');
writeFileSync(join(extension, "popup/test-ui.js"), `${waitCode}
(async () => {
  try {
    await waitFor(() => document.querySelector("#title").textContent && !document.querySelector("#download-button").disabled);
    const title = document.querySelector("#title").textContent;
    const formats = document.querySelector("#format-select").options.length;
    const win = await browser.windows.getCurrent();
    if (win.type !== "popup") throw new Error("Downloader must be a separate persistent window");
    if (document.querySelector("#format-select").value !== "mp4") throw new Error("MP4 must be selected by default");
    if (${testFolder}) {
      document.querySelector("#output-dir").value = ${JSON.stringify(chosenFolder)};
      document.querySelector("#pick-folder").click();
      const accepted = await fetch(${JSON.stringify(origin + "/choose-folder")});
      if (!accepted.ok) throw new Error(await accepted.text());
      await waitFor(() => !document.querySelector("#pick-folder").disabled);
      const picked = await browser.runtime.sendMessage({kind: "ui.state"});
      if (picked.settings.defaultDownloadDir !== ${JSON.stringify(chosenFolder)}) throw new Error("Picked folder not saved: " + document.querySelector("#status-line").textContent);
      if (document.querySelector("#title").textContent !== title) throw new Error("Folder picker lost the selected video");
      if (document.querySelector("#output-filename").value !== ${JSON.stringify(chosenFilename)}) throw new Error("Save As did not preserve the filename");
    }
    if (getComputedStyle(document.querySelector("#playlist")).display !== "none") throw new Error("Single video must hide playlist controls");
    document.querySelector("#download-button").click();
    await waitFor(() => document.querySelector(".job--complete"));
    const snapshot = await browser.runtime.sendMessage({kind: "ui.state"});
    const screenshot = await browser.tabs.captureVisibleTab(win.id, {format: "png"});
    await fetch(${JSON.stringify(origin + "/result")}, {method: "POST", body: JSON.stringify({title, formats, folderPicker: ${testFolder}, windowType: win.type, jobs: snapshot.jobs, screenshot})});
  } catch (error) { await fetch(${JSON.stringify(origin + "/result")}, {method: "POST", body: JSON.stringify({error: error.message, status: document.querySelector("#status-line").textContent})}); }
})();`);
const child = spawn(process.execPath, [join(root, "node_modules/web-ext/bin/web-ext.js"), "run", "--source-dir", extension,
  "--firefox-profile", join(dir, "profile"), "--keep-profile-changes", "--no-reload", "--no-input", "--no-config-discovery",
  ...(!testFolder ? ["--args=-headless"] : []), "--start-url=about:blank"], {
    cwd: root, stdio: ["ignore", "pipe", "pipe"],
    env: {...process.env, YTDLP_HOST_STATE_DIR: join(dir, "state"), YTDLP_HOST_IDLE_SECONDS: "2",
      MOZ_HEADLESS: testFolder ? undefined : "1", GDK_BACKEND: "x11", QT_QPA_PLATFORM: "xcb", MOZ_ENABLE_WAYLAND: "0", NO_UPDATE_NOTIFIER: "1"}
  });
let log = "";
child.stdout.on("data", (s) => {log += s;}); child.stderr.on("data", (s) => {log += s;});
let timer;
try {
  const report = await Promise.race([result, new Promise((_, reject) => {timer = setTimeout(() => reject(new Error(`Firefox test timed out\n${log}`)), 90000);})]);
  if (report.error) throw new Error(JSON.stringify(report));
  const output = join(root, "test-results"); mkdirSync(output, {recursive: true});
  writeFileSync(join(output, "firefox-popup.png"), Buffer.from(report.screenshot.split(",")[1], "base64"));
  delete report.screenshot;
  if (!report.jobs.some((job) => job.status === "complete" && readFileSync(job.filename).equals(clip))) throw new Error("Downloaded file did not match the source");
  if (testFolder && !report.jobs.some((job) => job.filename.startsWith(chosenFolder))) throw new Error("Download did not use the picked folder");
  if (testFolder && !report.jobs.some((job) => job.filename === join(chosenFolder, chosenFilename))) throw new Error("Download did not use the Save As filename");
  writeFileSync(join(output, "firefox-report.json"), JSON.stringify(report, null, 2));
  console.log("PASS: actual Firefox scoped container-cookie API authorization with a synthetic cookie; host grant released.");
  console.log("PASS: actual Firefox loads extension, connects native host, probes, downloads, and renders completion.");
  if (testFolder) console.log("PASS: real video context menu and desktop Save As dialog; chosen folder and renamed file used; window remains open.");
  console.log(`Screenshot: ${join(output, "firefox-popup.png")}`);
} finally {
  clearTimeout(timer); child.kill("SIGINT"); server.close();
}
