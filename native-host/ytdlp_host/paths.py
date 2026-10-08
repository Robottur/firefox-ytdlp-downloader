"""OS-specific locations: state directory, default download folder, pickers.

Everything here is pure path logic plus two thin OS integrations:

* the default download directory (XDG user dirs on Linux, the Downloads known
  folder on Windows),
* a "choose a folder" dialog, because the extension sandbox has no filesystem
  access of its own.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

DEFAULT_TEMPLATE = "%(title).200B [%(id)s].%(ext)s"

#: Keeps every spawned helper from touching the protocol pipes. stdout belongs
#: to the message stream: a single line from ``xdg-open`` would desynchronise
#: every frame after it.
_DETACHED = {
    "stdin": subprocess.DEVNULL,
    "stdout": subprocess.DEVNULL,
    "stderr": subprocess.DEVNULL,
    "close_fds": True,
}

_log_lock = threading.Lock()
_diagnostics = None


def state_dir() -> Path:
    """Directory holding the job database and the log file."""
    override = os.environ.get("YTDLP_HOST_STATE_DIR")
    if override:
        path = Path(override).resolve()
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    path = Path(base) / "ytdlp-host"
    # The state directory is private to the current user.
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def default_download_dir() -> str:
    """Best guess at the user's Downloads folder."""
    if IS_WINDOWS:
        folder = _windows_downloads_folder()
        if folder:
            return folder
    else:
        folder = _xdg_user_dir("XDG_DOWNLOAD_DIR")
        if folder:
            return folder
        xdg = os.environ.get("XDG_DOWNLOAD_DIR")
        if xdg:
            return xdg
    return str(Path.home() / "Downloads")


def _xdg_user_dir(name: str) -> str | None:
    """Read one entry out of ``~/.config/user-dirs.dirs``.

    That file contains shell-style assignments such as
    ``XDG_DOWNLOAD_DIR="$HOME/Downloads"``.
    """
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    config_file = Path(config_home) / "user-dirs.dirs"
    try:
        text = config_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(f"{name}="):
            continue
        value = line.split("=", 1)[1].strip().strip('"')
        value = value.replace("$HOME", str(Path.home()))
        value = os.path.expandvars(value)
        return value if Path(value).is_dir() else None
    return None


def _windows_downloads_folder() -> str | None:
    """Resolve the Downloads known folder without extra dependencies."""
    # Downloads has a Known Folder GUID, not a CSIDL value (0x28 is Profile).
    try:
        import winreg  # type: ignore

        key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            value, _ = winreg.QueryValueEx(handle, "{374DE290-123F-4565-9164-39C4925E467B}")
        return os.path.expandvars(str(value))
    except Exception:
        return None


def _dialog_start(initial_dir: str | None) -> str:
    start = Path(os.path.expanduser(initial_dir or default_download_dir()))
    return str(start.resolve() if start.is_dir() else Path.home())


def _desktop_dialog(start: str, filename: str | None = None, extension: str = "") -> str | None:
    """Use the installed desktop's file browser; Tk is the portable fallback.

    Helpers have private pipes, never the native messaging protocol streams.
    Cancellation is final, so it never opens a second fallback dialog.
    """
    title = "Save As" if filename is not None else "Choose download folder"
    if not IS_WINDOWS and sys.platform != "darwin":
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        names = ("zenity", "kdialog") if any(d in desktop for d in ("gnome", "cinnamon", "xfce")) else ("kdialog", "zenity")
        for name in names:
            executable = shutil.which(name)
            if not executable:
                continue
            if name == "kdialog":
                args = [executable, "--title", title]
                if filename is None:
                    args += ["--getexistingdirectory", start]
                else:
                    args += ["--getsavefilename", str(Path(start) / filename)]
                    if extension:
                        args += [f"{extension.upper()} files (*.{extension})"]
            else:
                args = [executable, "--file-selection", "--title", title, "--width=900", "--height=650"]
                if filename is None:
                    args += ["--directory", "--filename", start + os.sep]
                else:
                    args += ["--save", "--confirm-overwrite", "--filename", str(Path(start) / filename)]
                    if extension:
                        args += ["--file-filter", f"{extension.upper()} files | *.{extension}"]
            try:
                result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                                        text=True, encoding="utf-8", timeout=570, check=False)
                if result.returncode == 1:
                    return None
                if result.returncode == 0:
                    return result.stdout.rstrip("\r\n") or None
                write_log(f"{name} dialog failed (exit {result.returncode}); trying fallback")
            except subprocess.TimeoutExpired as exc:
                raise OSError("The save dialog timed out. Please open it again.") from exc
            except OSError as exc:
                write_log(f"{name} dialog unavailable: {exc!r}")
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
            if filename is None:
                chosen = filedialog.askdirectory(title=title, initialdir=start, mustexist=True)
            else:
                chosen = filedialog.asksaveasfilename(title=title, initialdir=start,
                    initialfile=filename, defaultextension=f".{extension}" if extension else "",
                    filetypes=[(f"{extension.upper()} files", f"*.{extension}")] if extension else [("All files", "*.*")],
                    confirmoverwrite=True)
        finally:
            root.destroy()
        return chosen or None
    except Exception as exc:
        write_log(f"file dialog unavailable: {exc!r}")
        raise OSError("File browser unavailable. Install kdialog or zenity on Linux, or Python's tkinter package.") from exc


def pick_folder(initial_dir: str | None = None) -> str | None:
    return _desktop_dialog(_dialog_start(initial_dir))


def suggest_filename(title: str, extension: str = "") -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(title or "Download"))
    name = name.encode("utf-8")[:160].decode("utf-8", errors="ignore").strip(" .") or "Download"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I):
        name = "_" + name
    if extension and not name.lower().endswith("." + extension):
        name += "." + extension
    return name


def pick_save_file(initial_dir: str | None, filename: str, extension: str = "") -> dict | None:
    if not isinstance(extension, str) or not re.fullmatch(r"[a-z0-9]{0,8}", extension):
        raise ValueError("Invalid file extension.")
    chosen = _desktop_dialog(_dialog_start(initial_dir), suggest_filename(filename, extension), extension)
    if not chosen:
        return None
    path = Path(chosen)
    if not path.is_absolute() or path.is_dir():
        raise ValueError("Choose a local file in the Save As dialog.")
    # The selected format controls the actual container, including after a
    # format change. Do not allow a misleading filename extension.
    if extension and path.suffix != "." + extension:
        path = path.with_suffix("." + extension) if path.suffix.lower() == "." + extension else path.with_name(path.name + "." + extension)
        if path.exists():
            raise ValueError("That filename already exists. Select its full name in Save As to confirm replacement.")
    if path.exists() and not extension:
        raise ValueError("Choose a new filename, or select MP4 or audio-only before replacing an existing file.")
    filename_template(path.name)
    return {"path": str(path), "outputDir": str(path.parent), "filename": path.name,
            "overwrite": path.is_file()}


def filename_template(filename: str) -> str:
    """A user filename is literal text, never a yt-dlp output expression."""
    if (not isinstance(filename, str) or not filename.strip(" .") or len(filename.encode("utf-8")) > 200
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', filename)
            or filename.endswith((" ", ".")) or filename.startswith("~")
            or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", filename, re.I)):
        raise ValueError("Choose a valid filename without folder separators or reserved characters (up to 200 bytes).")
    stem = re.sub(r"\.(mp4|m4v|mkv|webm|mov|avi|flv|mp3|m4a|aac|opus|ogg|wav|flac|ts)$", "", filename, flags=re.I)
    if not stem.strip(" ."):
        raise ValueError("Enter a filename before the extension.")
    return stem.replace("%", "%%") + ".%(ext)s"


def reveal_path(path: str) -> bool:
    """Show a file or folder in the OS file manager."""
    target = Path(path)
    if not target.exists():
        return False
    try:
        if IS_WINDOWS:
            if target.is_dir():
                os.startfile(str(target))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["explorer", "/select,", str(target)], **_DETACHED)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target if target.is_dir() else target.parent)], **_DETACHED)
        else:
            subprocess.Popen(
                ["xdg-open", str(target if target.is_dir() else target.parent)], **_DETACHED
            )
        return True
    except Exception as exc:
        write_log(f"could not reveal {target}: {exc!r}")
        return False


def log_file() -> Path:
    return state_dir() / "host.log"


def diagnostics():
    from .diagnostics import Diagnostics
    global _diagnostics
    directory = state_dir()
    with _log_lock:
        if _diagnostics is None or _diagnostics.directory != directory:
            _diagnostics = Diagnostics(directory)
        return _diagnostics


def write_log(message: str) -> None:
    """Legacy callers may pass arbitrary text; only allowlisted codes reach disk."""
    from .diagnostics import error_code
    try:
        diagnostics().record(error_code(message))
    except OSError:
        pass


def close_log() -> None:
    if _diagnostics is not None:
        _diagnostics.stop_capture()


def normalize_output_dir(path: str | None) -> str:
    """Expand and validate a user supplied output directory.

    Creates the directory if needed: yt-dlp would otherwise fail at the end of a
    long download rather than at the start.
    """
    if not path:
        path = default_download_dir()
    if not isinstance(path, str) or "\x00" in path:
        raise ValueError("Invalid download folder.")
    expanded = Path(os.path.expandvars(os.path.expanduser(path))).resolve()
    if len(str(expanded)) > 4096:
        raise OSError("that path is too long")
    expanded.mkdir(parents=True, exist_ok=True)
    if not expanded.is_dir():
        raise OSError("that path is not a directory")
    return str(expanded)


def safe_output_template(template: str | None, fallback: str = DEFAULT_TEMPLATE) -> str:
    """Keep a user supplied output template inside the chosen download folder.

    The template is joined onto the output directory by the caller, and
    ``pathlib`` discards the left-hand side when the right-hand side is
    absolute -- so an absolute or ``~``-prefixed template would silently write
    somewhere else entirely. ``..`` is rejected for the same reason.
    """
    if not template:
        return fallback
    if not isinstance(template, str) or len(template) > 500:
        return fallback
    if template.startswith(("/", "\\", "~")) or os.path.isabs(template):
        return fallback
    if template in (".", "..") or "\x00" in template or re.match(r"^[A-Za-z]:", template):
        return fallback
    # A filename template cannot introduce directories or alternate streams.
    if any(char in template for char in ("/", "\\", ":")):
        return fallback
    return template
