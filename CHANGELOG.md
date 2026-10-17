# Changelog

## 0.3.0 — 2026-10-08 (local release candidate)

- Added explicit per-operation, site-scoped Firefox cookie consent, container selection, in-memory credentials, and renewed consent for interrupted authenticated jobs.
- Removed broad tab access and disabled private-window downloads. Public downloads use fresh cookie jars and direct networking with a visible disclosure.
- Added no-finished-history defaults, opt-in retention, interrupted-job dismissal, legacy-history choices, and memory-only recovery for potentially signed URLs.
- Replaced raw logs with bounded operational diagnostics, previewed local export, legacy-log deletion, and generic notifications.
- Added bundled tracker-domain filtering for app requests. Disabled remote thumbnails, remote executable retrieval, and unfiltered external network downloaders; local ffmpeg conversion remains supported.
- Preserved persistent native Save As, video context menus, and MP4 defaults. Fixed source-container switching, authenticated retries, generic playlist subtitle context, and clearing in-flight metadata caches.
- Errors and diagnostics now name membership-required, expired-session, bot-check and YouTube challenge-solver failures instead of a generic failure; routine yt-dlp output is no longer recorded as a failure.
- Settings shows the yt-dlp-ejs challenge solver. Signed-in YouTube needs it; hand-built hosts without `yt-dlp[default]` failed with a generic error.
- `youtu.be` short links now request YouTube login cookies by switching to the canonical watch URL.
- The extension ID is now a random, unguessable value, so no other add-on can claim the ID the companion trusts or block signing. Reinstall the companion after updating.
- Added a quick-install section for end users (signed add-on plus companion from Releases).
- Linux, public-site and local authenticated checks passed. Member-only YouTube was confirmed with a real account. Windows runtime acceptance, authenticated X, and Mozilla signing remain pending.

## 0.2.2 — 2026-10-01

- Replaced the basic folder chooser with desktop Save As dialogs for individual downloads: browse locations, use folder shortcuts, and edit the suggested filename.
- Added visible Save As and filename controls. Playlists and default-folder settings use the desktop directory browser.
- Prefer kdialog/zenity on Linux; use Windows' native Save As dialog through Tk, with a portable fallback.
- Preserve chosen filenames through download/retry, treat percent signs literally, and honor confirmed replacement of existing files. Existing history is migrated automatically.
- Verified actual Firefox → desktop Save As → rename → download, plus cancellation, filename validation, history migration and replacement regressions.

## 0.2.1 — 2026-10-01

- Replaced the temporary toolbar popup with a reusable download window that survives the native folder dialog and stays open after adding a download.
- Persisted chosen folders in the background so closing a view cannot discard the selection; simultaneous requests share one dialog.
- Added one visible right-click download item across video, audio, links and player overlays, with embedded-player URL handling and a YouTube Shift + right-click hint.
- Made MP4 the default, including high-quality audio/video merging and remuxing fallback sources to a real MP4 container. Migrated the previous automatic default once while preserving custom preferences.
- Added regressions and actual Firefox folder-dialog checks; verified real MP4 files with audio and video from the user's X and YouTube samples.

## 0.2.0 — 2026-09-30

- Added a detached companion service, authenticated local bridge and reconnect support so browser closure does not stop downloads.
- Added Windows source installation, PyInstaller packaging, per-user registration and uninstall support.
- Made Linux installations independent of the source checkout and updated runtime dependencies for current YouTube support.
- Preserved settings across browser/service restarts and made settings failures visible.
- Added metadata caching with shared in-flight requests; reduced queue DOM updates.
- Fixed playlist entry cancellation, deleted-entry indexes, premature group completion, pagination, partial-failure reporting and history clearing during active playlists.
- Added searchable history and retries that preserve the original download options.
- Fixed format selection, selected audio streams, ffmpeg availability handling, current/first-video selection, caption source selection and subtitle conversion order.
- Fixed protocol EOF handling, Windows binary pipes, folder paths, file-manager behavior, output template validation and hidden UI sections.
- Added host/browser regressions, real local-media service tests, an actual Firefox smoke test, public-site test helpers and release/CI tooling.
