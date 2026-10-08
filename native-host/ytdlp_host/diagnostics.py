"""Local diagnostics accept an allowlist, never extractor text or exception payloads."""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from pathlib import Path

CODES = frozenset({'operation_failed', 'download_failed', 'download_complete', 'host_started',
                   'host_stopped', 'connection_closed', 'protocol_error', 'dialog_failed',
                   'permission_denied', 'authentication_required', 'network_failed',
                   'membership_required', 'session_expired', 'bot_check', 'challenge_failed'})

# Checked before the generic sign-in rule: these messages also mention signing in or cookies.
_ACCOUNT_RULES = (
    (('members-only', 'join this channel', "channel's members"), 'membership_required',
     'This video needs a channel membership. Use a signed-in session from an account with that membership.'),
    (('cookies are no longer valid',), 'session_expired',
     'The signed-in session expired. Reload the site in Firefox, then enable the signed-in session again.'),
    (('not a bot',), 'bot_check',
     'The site asked for bot verification. Open the video in Firefox, complete any check, then retry signed in.'),
    # Signed-in YouTube clients need yt-dlp-ejs; without it YouTube answers "page needs to be reloaded".
    (('page needs to be reloaded', 'challenge solving failed'), 'challenge_failed',
     'YouTube challenge solving failed. Reinstall the native host so yt-dlp-ejs is present, and check the JavaScript runtime in Settings.'),
)


def error_code(error: object) -> str:
    message = str(error).lower()
    return next((code for words, code, _ in _ACCOUNT_RULES if any(w in message for w in words)), 'operation_failed')


def safe_error(error: object) -> str:
    """Actionable categories without reflecting any untrusted error text."""
    message = str(error).lower()
    if any(word in message for word in ('tracker request was blocked', 'request filtering',
                                       'request policy', 'unfiltered external tool')):
        return 'Request filtering blocked this transfer or its network downloader. Choose another format or source; filtering stays enabled.'
    if isinstance(error, PermissionError) or 'permission denied' in message:
        return 'Permission denied. Choose a writable download folder and check file permissions.'
    code = error_code(error)
    if code != 'operation_failed':
        return next(text for _, c, text in _ACCOUNT_RULES if c == code)
    if any(word in message for word in ('sign in', 'login', 'cookie', 'authentication', '403', '401')):
        return 'Authentication may be required. Retry with explicit signed-in access, or check access on the site.'
    if any(word in message for word in ('ffmpeg', 'ffprobe', 'javascript runtime', 'deno')):
        return 'A media dependency is unavailable. Check ffmpeg and the JavaScript runtime in Settings.'
    if any(word in message for word in ('timed out', 'timeout', 'network', 'connection', 'resolve')):
        return 'Connection failed. Check your network and retry.'
    if any(word in message for word in ('dialog', 'tkinter', 'kdialog', 'zenity')):
        return 'File browser unavailable. Install kdialog, zenity, or tkinter, then retry.'
    if any(word in message for word in ('filename', 'directory', 'folder', 'path', 'disk', 'space')):
        return 'Cannot use the destination. Check the folder, filename, permissions, and free space.'
    if 'format' in message:
        return 'The requested format is unavailable. Refresh formats and choose another.'
    return 'The operation failed. Retry, check access on the site, or review local diagnostics in Settings.'


class Diagnostics:
    def __init__(self, directory: Path):
        self.directory = directory
        self._lock = threading.RLock()
        self._deadline = 0.0
        self._preview: tuple[str, str] | None = None

    def _file(self, capture=False):
        return self.directory / ('diagnostic-capture.jsonl' if capture else 'diagnostics.jsonl')

    def record(self, code: str, versions: dict | None = None) -> None:
        record = {'code': code if code in CODES else 'operation_failed'}
        clean = {key: value for key, value in (versions or {}).items()
                 if key in {'python', 'yt-dlp', 'ffmpeg', 'host'} and isinstance(value, str)
                 and re.fullmatch(r'\d+(?:\.\d+){0,3}', value)}
        if clean:
            record['versions'] = clean
        line = (json.dumps(record, separators=(',', ':')) + '\n').encode()
        with self._lock:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._append(self._file(), line, 64 * 1024, rotate=True)
            if self._deadline > time.monotonic():
                if not self._append(self._file(True), line, 1024 * 1024):
                    self._deadline = 0
            else:
                self._deadline = 0

    def _append(self, path, line, limit, rotate=False):
        size = path.stat().st_size if path.exists() else 0
        if size + len(line) > limit and not rotate:
            return False
        with path.open('wb' if size + len(line) > limit else 'ab') as handle:
            handle.write(line)
        path.chmod(0o600)
        return True

    def status(self):
        with self._lock:
            remaining = max(0, self._deadline - time.monotonic())
            return {'captureActive': remaining > 0, 'captureSecondsRemaining': int(remaining),
                    'legacyLogsPresent': (self.directory / 'host.log').exists()}

    def start_capture(self):
        with self._lock:
            self._file(True).unlink(missing_ok=True)
            self._deadline = time.monotonic() + 15 * 60
            return self.status()

    def stop_capture(self):
        with self._lock:
            self._deadline = 0
            return self.status()

    def preview(self):
        with self._lock:
            # Revalidate persisted records too: exports must not trust edited local files.
            lines = []
            for path, limit in ((self._file(), 64 * 1024), (self._file(True), 1024 * 1024)):
                if not path.exists():
                    continue
                with path.open('rb') as handle:
                    data = handle.read(limit)
                for line in data.decode('utf-8', errors='replace').splitlines():
                    try:
                        item = json.loads(line)
                        if not isinstance(item, dict) or item.get('code') not in CODES:
                            continue
                        safe = {'code': item['code']}
                        versions = item.get('versions', {})
                        if isinstance(versions, dict):
                            versions = {k: v for k, v in versions.items() if k in {'python', 'yt-dlp', 'ffmpeg', 'host'}
                                        and isinstance(v, str) and re.fullmatch(r'\d+(?:\.\d+){0,3}', v)}
                            if versions:
                                safe['versions'] = versions
                        lines.append(json.dumps(safe, separators=(',', ':')) + '\n')
                    except (ValueError, TypeError):
                        continue
            # Keep the preview plus JSON framing well below native messaging's 1 MiB limit.
            # Save exports exactly what the user reviewed, never undisplayed records.
            text = ''.join(lines)
            if len(text) > 256 * 1024:
                text = text[:256 * 1024].rsplit('\n', 1)[0] + '\n'

            token = secrets.token_urlsafe(24)
            self._preview = (token, text)
            return {'previewId': token, 'text': text}

    def save_preview(self, preview_id: str, path: Path):
        with self._lock:
            if self._preview is None or preview_id != self._preview[0]:
                raise ValueError('Preview diagnostics before saving.')
            Path(path).write_text(self._preview[1], encoding='utf-8')
            Path(path).chmod(0o600)
            self._preview = None

    def delete(self):
        with self._lock:
            self._deadline = 0
            self._preview = None
            self._file().unlink(missing_ok=True)
            self._file(True).unlink(missing_ok=True)
            return self.status()

    def delete_legacy(self):
        with self._lock:
            (self.directory / 'host.log').unlink(missing_ok=True)
            return self.status()
