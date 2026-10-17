#!/usr/bin/env bash
# Per-user install. Overrides support isolated packaging tests.
set -euo pipefail
umask 077
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${YTDLP_HOST_STATE_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/ytdlp-host}"
NATIVE_DIR="${YTDLP_MANIFEST_DIR:-$HOME/.mozilla/native-messaging-hosts}"
VENV_DIR="$DATA_DIR/venv"
PYTHON="${YTDLP_PYTHON:-python3}"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ is required"'
mkdir -p "$DATA_DIR" "$NATIVE_DIR"
if [ -x "$VENV_DIR/bin/python" ]; then
    if "$VENV_DIR/bin/python" -c 'import importlib.util; assert importlib.util.find_spec("ytdlp_host") and importlib.util.find_spec("ytdlp_host.bootstrap")' >/dev/null 2>&1; then
        "$VENV_DIR/bin/python" -m ytdlp_host.bootstrap --stop-service
        sleep 2
    fi
else
    "$PYTHON" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip
# Regular installation survives moving/deleting the source checkout.
"$VENV_DIR/bin/python" -m pip install --upgrade "$REPO_DIR" 'yt-dlp[default]'
"$VENV_DIR/bin/python" - "$DATA_DIR" "$NATIVE_DIR" <<'PY'
import json
from pathlib import Path
import shlex
import sys
data, native = map(lambda s: Path(s).resolve(), sys.argv[1:])
launcher = data / 'launcher.sh'
text = ('#!/bin/sh\n'
        f'export YTDLP_HOST_STATE_DIR={shlex.quote(str(data))}\n'
        'export PATH="$HOME/.deno/bin:$HOME/.local/bin:$PATH"\n'
        f'exec {shlex.quote(str(data / "venv/bin/python"))} -X utf8 -m ytdlp_host.bootstrap "$@"\n')
launcher.write_text(text, encoding='utf-8')
launcher.chmod(0o700)
manifest = {'name': 'ytdlp_downloader.host', 'description': 'yt-dlp Downloader companion',
            'path': str(launcher), 'type': 'stdio',
            'allowed_extensions': ['ytdlp-downloader-573c69b6aa75@local']}
(native / 'ytdlp_downloader.host.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
print(f'Installed host: {launcher}')
print(f'Firefox registration: {native}')
PY
if ! command -v ffmpeg >/dev/null; then
    printf '%s\n' 'Install ffmpeg: sudo pacman -S ffmpeg (Arch), sudo apt install ffmpeg (Debian).'
fi
if ! command -v deno >/dev/null && ! command -v node >/dev/null; then
    printf '%s\n' 'Install Deno for YouTube: sudo pacman -S deno, or https://deno.com/.'
fi
if ! command -v kdialog >/dev/null && ! command -v zenity >/dev/null; then
    printf '%s\n' 'For a full Save As file browser, install kdialog (Arch/KDE) or zenity (GNOME/GTK).'
    if ! "$PYTHON" -c 'import tkinter' >/dev/null 2>&1; then
        printf '%s\n' 'No fallback dialog is available. You can still type a folder and filename.'
    fi
fi
printf '\n%s\n' 'Done. Firefox → about:debugging → This Firefox → Load Temporary Add-on → extension/manifest.json.'
printf '%s\n' 'For permanent installation, use an AMO-signed XPI. See README.md.'
