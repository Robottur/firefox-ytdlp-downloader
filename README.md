# yt-dlp Downloader for Firefox

Download MP4 video, MP3 audio, subtitles and playlists from Firefox on Linux and Windows. Click the toolbar button or right-click a video, page or link to open the download window, choose a quality, and add the download to the queue. YouTube and X are the primary tested sites; extraction is provided by yt-dlp.

The extension needs a small companion app. The app owns the queue and keeps downloading when Firefox closes. Reopening Firefox reconnects to it. Downloads, settings and history stay on your computer.

> **Made with AI.** This tool's code, tests and documentation were written by AI (Anthropic's Claude), with direction and testing by a human maintainer.

## Quick install (users)

You need two parts: the companion app and the signed Firefox add-on. Both come from the project's **Releases** page.

1. **Install the companion app.**
   - **Windows:** install the tools with `winget install Gyan.FFmpeg` and `winget install DenoLand.Deno`. Extract `ytdlp-host-windows.zip` and run `powershell -NoProfile -ExecutionPolicy Bypass -File .\install_windows.ps1` in that folder. Windows may warn about an unknown publisher: the executable is not code-signed.
   - **Linux:** install Python 3.11+, ffmpeg and Deno (see below). Extract `ytdlp-host-linux-<version>.tar.gz` and run `bash native-host/install_linux.sh` inside it.
2. **Install the add-on.** Open the signed `ytdlp-downloader-<version>.xpi` in Firefox (drag it into a window, or use **Add-ons → ⚙ → Install Add-on From File…**), then accept the prompt.
3. **Restart Firefox** and click the toolbar button. Settings → Host status should show the companion as connected.

Only install the `.xpi` and companion from the same release; the add-on and companion check each other's identity.

## Install on Linux

Use desktop Firefox **142 or later** and **Python 3.11 or later**. For Arch Linux:

```sh
sudo pacman -S python python-pip ffmpeg deno tk kdialog
bash native-host/install_linux.sh
```

On Debian/Ubuntu, install `python3`, `python3-venv`, `python3-tk`, `zenity`, and `ffmpeg` with apt; install [Deno](https://docs.deno.com/runtime/getting_started/installation/) separately if it is unavailable in your repositories. Node.js 22+ is also supported as a JavaScript runtime.

The installer creates a private virtual environment under `~/.local/share/ytdlp-host`, installs the host and `yt-dlp[default]`, and registers it with Firefox. It respects `XDG_DATA_HOME`. It does not alter system Python packages and remains usable if this source directory is moved.

Install the extension for development:

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on…**.
3. Choose `extension/manifest.json`, or the built `.xpi` in `dist/`.
4. Open a video page and click the extension’s toolbar button. Pin it using Firefox’s Extensions menu if needed.

Temporary add-ons disappear when Firefox exits. For permanent installation in standard Firefox, the XPI must be [signed by Mozilla](https://extensionworkshop.com/documentation/publish/signing-and-distribution-overview/). The local build is **unsigned**; signing and publishing require your own Mozilla developer account. The extension ID is fixed at `ytdlp-downloader-573c69b6aa75@local`.

These instructions target a normal desktop Firefox installation. Flatpak/Snap builds have additional native-messaging sandbox integration requirements and are not part of the tested setup.

## Install on Windows

Use Windows 10/11, desktop Firefox 142+, ffmpeg and Deno:

```powershell
winget install Gyan.FFmpeg
winget install DenoLand.Deno
```

Restart Firefox after installing these tools so its PATH is refreshed.

**With a built Windows bundle:** extract `ytdlp-host-windows.zip`, then run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install_windows.ps1
```

The bundle includes Python, yt-dlp and EJS in `ytdlp-host.exe` and its `_internal` folder. It needs no separate Python installation. ffmpeg and Deno remain separate dependencies.

**From this source checkout:** install Python 3.11+ first, then run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\native-host\install_windows.ps1 -FromSource
```

Both installers are per-user, require no administrator rights themselves, and register `HKCU\Software\Mozilla\NativeMessagingHosts\ytdlp_downloader.host`. Host files go under `%LOCALAPPDATA%\ytdlp-host`. Load the extension using the same `about:debugging` steps as Linux.

Build the bundle **on Windows** with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\native-host\build_windows.ps1
```

The Windows executable is not cross-compiled on Linux. The included GitHub Actions workflow builds it and runs the service tests on Windows when the workflow is executed. This checkout was verified on Linux; a Windows machine is still required for Windows runtime validation.

## Using the extension

- **Window:** the toolbar button opens a separate download window. It stays open when you choose a folder, switch applications, or start a download. Repeating the action focuses the existing window.
- **Right-click:** select **Download with yt-dlp** from Firefox's context menu. On YouTube or another player with its own menu, use **Shift + right-click** to show Firefox's menu ([Firefox behavior](https://developer.mozilla.org/en-US/docs/Web/API/Element/contextmenu_event)). For X, open the individual post/video before downloading.
- **Source:** toolbar and context-menu actions pass the selected page/video to the window. A manually opened window waits for a pasted URL. You can paste another URL. **Read** forces a fresh lookup; reopening the same video normally uses a two-minute metadata cache.
- **Quality:** the default is **MP4 · best available video + audio**. It prefers MP4/M4A streams and uses ffmpeg to merge or remux when needed, without re-encoding. Without ffmpeg, the MP4 preset requires a combined MP4 stream; choose another available format if the site does not provide one. Explicit format choices remain available. In Settings, `mp4` selects this preset; custom yt-dlp selectors are also supported.
- **Audio only:** select the audio stream and convert it to MP3 at 192 kbps. The selected stream is respected.
- **Subtitles:** choose the language and either official or automatic captions. Available tracks are saved beside the media; ffmpeg converts them to SRT. Playlist languages are based on the representative video and may not exist on every entry.
- **Playlists:** choose the current/first video, the whole playlist, or individual entries. The entry picker loads up to 500 at a time and offers **Load more**. Whole-playlist downloads continue through subsequent pages. Quality limits apply separately to each entry.
- **Save As:** click **Save As…** to open your desktop file browser, choose a folder, and edit the suggested video filename. Click **Save**, then **Download** in the extension. The chosen folder becomes the default for future downloads. Cancelling keeps the previous name and folder. You can also edit both fields directly; the file extension follows the chosen format. Existing files are replaced only after confirmation in Save As. Changing the name, folder or format clears that confirmation.
- **Desktop dialogs:** Linux uses the full [KDE file browser](https://develop.kde.org/docs/administration/kdialog/#--getsavefilename-dialog-box) via `kdialog`, or the GTK browser via `zenity` (preferred on GNOME/Cinnamon/Xfce). Windows uses its native Save As dialog through Tk. Tk's basic browser is the fallback when neither Linux helper is installed. Playlist downloads and Settings use the same desktop integration to choose a folder; each playlist video keeps its own filename.
- **Queue:** progress includes transfer speed and ETA when the site reports them. Cancelling an entry affects that entry; cancelling its group affects the playlist. Completed files have an **Open folder** button. Failed/cancelled jobs can be retried.
- **Privacy:** public downloads do not use your browser session. Downloads connect directly to the source site and its media providers; they see the connection IP. Firefox proxy settings and environment HTTP proxies are not inherited. Private-window downloads are disabled in this release.
- **Signed-in videos:** click **Use signed-in session** and grant access to the selected HTTPS site. The source tab's Firefox container is selected when available; for pasted URLs choose the session explicitly. Consent covers metadata and one download/playlist operation. Cookies remain in memory and are discarded after the operation; unused grants expire after ten minutes without activity. Restarted authenticated jobs need fresh consent. Use **Use without account** to discard a pending grant. Membership/login and yt-dlp support are still required; this does not bypass account access or DRM. Synthetic authenticated downloads are tested and member-only YouTube was confirmed with a real account; authenticated X acceptance remains pending.
- **History:** finished records are not retained on disk by default. This session can still show results and retry failures. Active/interrupted jobs persist for recovery when their source URL is safe to retain. URLs with unknown query parameters or fragments remain in memory only to avoid saving signed links; the queue labels them “Session only.” Settings offers explicit history retention and a keep/remove choice for old records. Clearing records preserves downloaded and partial files.
- **Tracker filtering:** app-owned HTTP(S) extraction, redirects, subtitles and native HTTP/HLS/DASH media requests use a local domain denylist. No remote blocklist, URL reputation service, or automatic executable-component download is used. Browser-wide blocking is outside scope. Remote thumbnails are disabled. External network downloaders (including ffmpeg-based live HLS) are rejected; local ffmpeg merging/conversion remains available. Mixed-purpose endpoints and unlisted trackers may still be contacted. Some sources that require a blocked domain, including TenPlay DAI, are unsupported; there is no filtering bypass switch. This provides no anonymity guarantee.
- **Diagnostics:** default local diagnostics contain bounded operational codes rather than raw extractor messages. Settings offers a fifteen-minute, size-limited diagnostic capture, a preview before local export, and deletion controls including old raw logs. Preview/export is limited to 256 KiB of sanitized records to fit native messaging. Nothing is automatically uploaded. Completion notifications do not show video titles or filenames.

Cancellation is observed at the next download or postprocessor callback. A pending extraction/network request or an ffmpeg operation can take time to return. Partial files are kept. Retry clears old overwrite authorization; use Save As again to deliberately replace an existing destination. Retry uses yt-dlp’s normal resume behavior. A machine shutdown or companion crash marks unfinished jobs as interrupted; retry them after restarting.

The supplied live YouTube URL was checked for metadata only. Live-stream metadata is supported, but formats requiring external network tools are rejected by the request policy. Automatic live recording is outside this release. Cookie-based access is available with explicit consent; unsupported extractors, expired login sessions, and account restrictions may still prevent downloads. Supported extractors and available formats also depend on the current yt-dlp release.

## Updates and troubleshooting

**Updating to 0.3.0:** update both the companion and the extension. After finishing active downloads, close Firefox, run the platform installer, and load `extension/manifest.json` or `dist/ytdlp-downloader-0.3.0.xpi` again in `about:debugging`. The old automatic quality default migrates to MP4 once; custom quality preferences are preserved.

Installation and requested updates contact Python/package and distribution providers. Ordinary download operations do not update dependencies or fetch executable components. OS DNS/TLS services and routing remain outside the app policy.

Finish or cancel downloads, close Firefox, and re-run your platform installer to update the host and yt-dlp. For a bundled Windows installation, build/download a new executable bundle. Install/update yt-dlp and its EJS component together: [yt-dlp’s current YouTube setup guide](https://github.com/yt-dlp/yt-dlp/wiki/EJS).

| Problem | Action |
| --- | --- |
| Native host unavailable | Re-run the platform installer. Confirm you loaded the extension with its fixed ID. |
| Missing ffmpeg | Install ffmpeg, then restart the companion and Firefox. |
| Missing JS runtime / YouTube challenge errors | Install Deno or Node.js 22+, update the host, then restart it. |
| Settings shows **EJS solver missing**, or signed-in YouTube fails | Re-run the platform installer; it installs `yt-dlp[default]`, including the `yt-dlp-ejs` challenge solver. Then restart the companion. |
| `--stop-service` does not stop the companion | The launcher predates 0.3.0 and ignores arguments. Re-run the platform installer. |
| Basic or unavailable file dialog | Install `kdialog` (KDE/Arch) or `zenity` (GTK/GNOME), then restart the companion. Manual folder/filename fields remain available. |
| Some playlist entries failed | Errors stay on the affected entries; successful files are kept. Retry failed entries individually. |
| Site extraction failed | An error category appears on the job; update the host if the site recently changed. |

The companion exits after five minutes with no clients or active downloads. To restart it immediately after installing dependencies, close Firefox and run:

```sh
# Linux
~/.local/share/ytdlp-host/launcher.sh --stop-service
```

```powershell
# Windows bundle (source installs use venv\Scripts\ytdlp-host.exe)
& "$env:LOCALAPPDATA\ytdlp-host\ytdlp-host.exe" --stop-service
```

Stopping is refused while downloads are active. State lives under `~/.local/share/ytdlp-host` (Linux, respecting XDG) or `%LOCALAPPDATA%\ytdlp-host` (Windows):

| File | Purpose |
| --- | --- |
| `jobs.db` | Download queue/history; SQLite |
| `settings.json` | Companion settings |
| `diagnostics.jsonl` | Bounded operational diagnostic codes |
| `diagnostic-capture.jsonl` | Optional bounded capture; review before export |
| `host.log` | Legacy raw log from older versions; remove explicitly in Settings |
| `service.json` | Private connection details for the local service; do not share this file |

To uninstall on Windows, run `native-host/uninstall_windows.ps1`. On Linux, stop the idle host and remove `~/.mozilla/native-messaging-hosts/ytdlp_downloader.host.json`. Remove the extension through Firefox. Installed host files, history and downloaded files are retained until you choose to remove them.

## Development and validation

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e ./native-host
npm ci

# Native host regressions
.venv/bin/python -m unittest discover -s native-host/tests -t native-host

# Browser logic/DOM regressions and extension lint
npm test
npm run lint

# Generated local media: real protocol, service reconnect, MP3, subtitles,
# separate audio/video merge, whole/selected playlists, cancellation, persistence
.venv/bin/python tools/service_integration.py

# Synthetic authenticated HTTPS download (requires openssl and ffmpeg)
python3 tools/auth_integration.py

# Real popup/background JavaScript wired to the real Python host
PYTHON="$PWD/.venv/bin/python" npm run test:integration

# Real Firefox, isolated profile/state; requires an installed native manifest
node tools/firefox_smoke.mjs

# Linux: real video context menu and native folder dialog in a virtual display
# Requires Xvfb, xdotool, ImageMagick and kdialog or zenity
xvfb-run -a -s '-screen 0 1440x1000x24' node tools/firefox_smoke.mjs --folder

# Build unsigned XPI, source archives and checksums
npm run build
python3 tools/package_release.py
```

On Windows use `.venv\Scripts\python.exe`. Local integration tests need permission to bind loopback sockets. They generate their own media and use temporary state/download folders. The Firefox check saves its screenshot and report in `test-results/`.

Public-site checks are opt-in:

```sh
python3 tools/check_sites.py 'https://www.youtube.com/watch?v=VIDEO_ID'
python3 tools/check_downloads.py 'https://example.com/short-public-video'
```

The second command downloads short clips into a temporary directory and removes them afterward. It refuses live streams and videos longer than five minutes.

## Architecture

```text
Firefox popup/options ⇄ background event page ⇄ native stdio bridge
                                                    ⇅ authenticated local socket
                                         detached Python download service
                                            ⇅              ⇅
                                     yt-dlp + ffmpeg   SQLite + settings
```

Firefox terminates its native child when the connection closes. The bridge starts a detached service (`start_new_session` on Linux; `CREATE_BREAKAWAY_FROM_JOB` on Windows) and relays bounded JSON frames. A random secret in the private state directory authenticates the loopback connection. A process lock prevents multiple services writing the same queue. No HTTP command endpoint is exposed.

The host uses yt-dlp’s Python API, a configurable 1–8 download limit, independent cancellation events, bounded metadata caching and per-entry playlist jobs. Startup/settings changes and job state transitions persist to disk. Active queue snapshots are chunked to fit Firefox’s native-messaging limit. The extension ships without granted site access; its optional HTTPS host permission is requested for the selected origin only when you enable signed-in access. Firefox can retain that permission, but each new download still requires a fresh in-app session grant. Metadata/URLs supplied by sites are rendered as text.

See [CHANGELOG.md](CHANGELOG.md) for changes and remaining platform limits.
