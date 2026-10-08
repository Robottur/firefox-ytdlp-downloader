"""Entry point: the stdio loop and the message dispatch table.

The main thread does nothing but read frames from stdin. Every request is
handed to a small thread pool so a slow extraction never blocks the pipe, and
results go back out through a single lock-protected writer.

Nothing in this process may ever write to stdout except
:class:`protocol.MessageWriter`. The very first thing :func:`main` does is point
``sys.stdout`` at stderr, so a stray ``print`` anywhere in the dependency tree
cannot corrupt the protocol stream.
"""

from __future__ import annotations

import queue
import json
import os
import shutil
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

import yt_dlp
from yt_dlp.dependencies import yt_dlp_ejs

from . import jobs, paths, probe
from .cache import ProbeCache
from .sessions import Sessions
from .diagnostics import safe_error
from .protocol import MessageWriter, ProtocolError, read_message

HOST_NAME = "ytdlp_downloader.host"
HOST_VERSION = "0.3.0"

DEFAULT_SETTINGS: dict[str, Any] = {
    "defaultDownloadDir": "",
    "defaultFormat": "mp4",
    "maxConcurrentDownloads": 2,
    "notifyOnComplete": True,
}


def _log(message: str) -> None:
    paths.write_log(f"[{HOST_VERSION}] {message}")


def ffmpeg_version() -> str | None:
    if not shutil.which("ffmpeg"):
        return None
    try:
        out = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            stdin=subprocess.DEVNULL,  # never inherit the protocol pipe
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        first = (out.stdout or "").splitlines()[:1]
        return first[0] if first else "installed"
    except Exception:
        return "installed"


class DaemonPool:
    """A minimal thread pool whose workers are daemons.

    ``concurrent.futures.ThreadPoolExecutor`` uses non-daemon threads, and
    Python's atexit hook joins those: a probe still in flight (socket timeout
    times retries) would keep the host process alive long after Firefox has
    gone. This pool is fire-and-forget, so closing the pipe ends the process.
    """

    def __init__(self, size: int) -> None:
        self._queue: "queue.Queue[Callable[[], None] | None]" = queue.Queue()
        self._threads: list[threading.Thread] = []
        for index in range(size):
            thread = threading.Thread(
                target=self._loop, name=f"ytdl-req-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def _loop(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                return
            try:
                task()
            except Exception as exc:  # one bad request must not kill a worker
                _log(f"request worker crashed: {exc!r}")

    def submit(self, task: Callable[[], None]) -> None:
        self._queue.put(task)

    def shutdown(self) -> None:
        for _ in self._threads:
            self._queue.put(None)


class Host:
    """Owns the connection and every piece of state behind it."""

    def __init__(self, stream: Any = None, send: Callable | None = None) -> None:
        # ``sys.stdout`` is already stderr by the time we get here (see main),
        # so the protocol stream has to be passed in explicitly.
        self.writer = MessageWriter(stream or sys.__stdout__.buffer) if send is None else None
        self._sender = send or self.writer.send
        self.settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.settings["defaultDownloadDir"] = paths.default_download_dir()
        self._settings_lock = threading.Lock()
        try:
            saved = json.loads((paths.state_dir() / "settings.json").read_text(encoding="utf-8"))
            self.settings.update({k: saved[k] for k in DEFAULT_SETTINGS if k in saved})
        except (OSError, ValueError, TypeError):
            pass
        self._ffmpeg_version = ffmpeg_version()
        self.ffmpeg_found = self._ffmpeg_version is not None
        self._cache = ProbeCache()
        self._sessions = Sessions()
        self._pool = DaemonPool(6)
        self._store = jobs.JobStore(paths.state_dir() / "jobs.db")
        self._manager = jobs.JobManager(
            store=self._store,
            # self.send, not writer.send: a dropped extension must not surface
            # inside a progress hook as a download failure.
            send=self.send,
            ffmpeg_found=self.ffmpeg_found,
            default_dir=lambda: self.settings["defaultDownloadDir"],
            max_concurrent=int(self.settings["maxConcurrentDownloads"]),
            log=_log,
        )
        # Recover before starting workers, so nothing can claim an interrupted
        # job while it is being rewritten.
        for job in self._manager.recover():
            _log(f"marked interrupted job {job.id} as error")
        self._manager.start()
        self._stopping = False
        paths.diagnostics().record("host_started", versions={"host": HOST_VERSION})

    # ------------------------------------------------------------------ util

    def send(self, payload: dict[str, Any]) -> None:
        try:
            self._sender(payload)
        except Exception as exc:  # a broken pipe is not worth a traceback
            _log(f"write failed: {exc!r}")

    def error(self, message: str, code: str, request_id: str | None = None) -> None:
        payload = {
            "type": "error",
            "message": message or code.replace("_", " "),
            "code": code,
        }
        if request_id:
            payload["requestId"] = request_id
        self.send(payload)

    def _dispatch(self, request_id: str, work: Callable[[], dict[str, Any] | None]) -> None:
        """Run ``work`` off the read loop and reply with whatever it returns."""

        def runner() -> None:
            try:
                result = work()
                if result:
                    result.setdefault("type", "ack")
                    if request_id:
                        result.setdefault("requestId", request_id)
                    self.send(result)
            except Exception as exc:
                _log(f"request {request_id} failed: {traceback.format_exc()}")
                self.error(self._describe(exc), self._code_for(exc), request_id)

        self._pool.submit(runner)

    @staticmethod
    def _describe(exc: BaseException) -> str:
        """Never surface an empty error string: the popup would show nothing."""
        return str(exc) if isinstance(exc, ValueError) else safe_error(exc)

    @staticmethod
    def _code_for(exc: BaseException) -> str:
        if isinstance(exc, probe.ProbeError):
            return "probe_failed"
        if isinstance(exc, ValueError):
            return "bad_request"
        return "internal_error"

    def _reply_error(self, exc: BaseException, request_id: str) -> None:
        self.error(self._describe(exc), self._code_for(exc), request_id)

    # -------------------------------------------------------------- handlers

    def handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        request_id = message.get("requestId") or ""
        handler = getattr(self, f"_on_{kind}", None) if isinstance(kind, str) else None
        if handler is None:
            self.error(f"Unknown message type: {kind!r}", "unknown_type", request_id or None)
            return
        try:
            handler(message, request_id)
        except Exception as exc:
            _log(f"handler {kind} raised: {traceback.format_exc()}")
            self._reply_error(exc, request_id)

    def _on_ping(self, message: dict[str, Any], request_id: str) -> None:
        self._dispatch(request_id, lambda: {"type": "pong", "hostVersion": HOST_VERSION})

    def _on_authorize_session(self, message, request_id):
        token = self._sessions.create(message.get("url", ""), message.get("cookies"))
        self.send({"type": "session_authorized", "requestId": request_id, "sessionToken": token})

    def _on_release_session(self, message, request_id):
        self._sessions.drop(message.get("sessionToken"))
        self.send({"type": "ack", "requestId": request_id})

    def _on_probe(self, message: dict[str, Any], request_id: str) -> None:
        url = message.get("url") or ""
        single = bool(message.get("single"))

        def work() -> dict[str, Any]:
            if message.get("sessionToken"):
                with self._sessions.use(message["sessionToken"], url):
                    result = probe.probe(url, single=single)
            else:
                result = self._cache.get((url, single), lambda: probe.probe(url, single=single),
                                         refresh=bool(message.get("refresh")))
            result["type"] = "probe_result"
            return result

        self._dispatch(request_id, work)

    def _on_probe_entries(self, message: dict[str, Any], request_id: str) -> None:
        url = message.get("url") or ""

        def work() -> dict[str, Any]:
            limit = message.get("limit") or probe.MAX_ENTRIES
            offset = message.get("offset") or 0
            if message.get("sessionToken"):
                with self._sessions.use(message["sessionToken"], url):
                    result = probe.list_entries(url, limit=limit, offset=offset)
            else:
                result = self._cache.get((url, limit, offset),
                    lambda: probe.list_entries(url, limit=limit, offset=offset))
            result["type"] = "probe_entries_result"
            return result

        self._dispatch(request_id, work)

    def _on_download_start(self, message: dict[str, Any], request_id: str) -> None:
        message = dict(message)
        # Credentials are never accepted through generic job fields.
        message.pop("_sessionCookies", None)
        message.pop("authRequired", None)
        token = message.pop("sessionToken", None)
        if token:
            message["_sessionCookies"] = self._sessions.acquire(token, message.get("url", ""))
            message["authRequired"] = True
        job = self._manager.submit(message)
        if token:
            self._sessions.drop(token)
        self.send({"type": "queued", "requestId": request_id, "jobId": job.id, "kind": job.kind})

    def _on_cancel(self, message: dict[str, Any], request_id: str) -> None:
        job_id = message.get("jobId") or ""
        cancelled = self._manager.cancel(job_id)
        if not cancelled:
            self.error("That job is no longer running.", "not_cancellable", request_id or None)
            return
        self.send({"type": "ack", "requestId": request_id, "jobId": job_id, "cancelled": True})

    def _on_retry(self, message: dict[str, Any], request_id: str) -> None:
        job_id = message.get("jobId") or ""
        previous = self._manager._jobs.get(job_id) or self._store.get(job_id)
        token = message.get("sessionToken")
        cookies = self._sessions.acquire(token, previous.url) if token and previous else None
        job = self._manager.retry(job_id, cookies=cookies)
        if token:
            self._sessions.drop(token)
        self.send({"type": "queued", "requestId": request_id, "jobId": job.id, "kind": job.kind})

    def _on_list_jobs(self, message: dict[str, Any], request_id: str) -> None:
        # Keep incremental events outside the snapshot's chunk sequence.
        with self._manager._lock:
            job_list = [job.to_wire() for job in self._manager.list_jobs()]
            for index in range(0, max(1, len(job_list)), 25):
                final = index + 25 >= len(job_list)
                self.send({"type": "jobs", "requestId": request_id,
                           "jobs": job_list[index:index + 25], "append": index > 0,
                           "final": final, "settings": self.settings})

    def _on_clear_finished(self, message: dict[str, Any], request_id: str) -> None:
        self._manager.clear_finished(include_interrupted=message.get("includeInterrupted") is True)
        self._cache.clear()
        # Resend the queue so the extension's mirror drops the cleared rows too;
        # an ack alone would leave them on screen until the next reconnect.
        self._on_list_jobs(message, request_id)

    def _on_history_policy(self, message, request_id):
        self.send({"type": "history_policy", "requestId": request_id, **self._store.history_policy()})

    def _on_configure_history(self, message, request_id):
        policy = self._store.configure_history(message.get("enabled"), message.get("existing"))
        if message.get("existing") == "remove":
            self._manager.clear_finished()
            self._cache.clear()
        self.send({"type": "history_policy", "requestId": request_id, **policy})
        self._on_list_jobs({}, "")

    def _on_dismiss(self, message, request_id):
        self._manager.dismiss(message.get("jobId", ""))
        self._cache.clear()
        self._on_list_jobs(message, request_id)

    def _diagnostics_action(self, action, request_id):
        self._dispatch(request_id, lambda: {"type": "diagnostics", **getattr(paths.diagnostics(), action)()})

    def _on_diagnostics_status(self, message, request_id):
        self._diagnostics_action("status", request_id)

    def _on_diagnostics_start(self, message, request_id):
        self._diagnostics_action("start_capture", request_id)

    def _on_diagnostics_stop(self, message, request_id):
        self._diagnostics_action("stop_capture", request_id)

    def _on_diagnostics_preview(self, message, request_id):
        self._diagnostics_action("preview", request_id)

    def _on_diagnostics_delete(self, message, request_id):
        self._diagnostics_action("delete", request_id)

    def _on_diagnostics_delete_legacy(self, message, request_id):
        self._diagnostics_action("delete_legacy", request_id)

    def _on_diagnostics_save(self, message, request_id):
        def work():
            chosen = paths._desktop_dialog(paths._dialog_start(None), "diagnostics.jsonl", "jsonl")
            if not chosen:
                return {"type": "diagnostics", "saved": False}
            paths.diagnostics().save_preview(message.get("previewId"), Path(chosen))
            return {"type": "diagnostics", "saved": True}
        self._dispatch(request_id, work)

    def _on_pick_folder(self, message: dict[str, Any], request_id: str) -> None:
        def work() -> dict[str, Any] | None:
            chosen = paths.pick_folder(message.get("initialDir"))
            if chosen is None:
                # The user closed the dialog, or tkinter is unavailable. The
                # extension treats "no answer" as "nothing changed".
                return {"type": "folder_picked", "cancelled": True}
            return {"type": "folder_picked", "path": chosen, "cancelled": False}

        self._dispatch(request_id, work)

    def _on_pick_save_file(self, message: dict[str, Any], request_id: str) -> None:
        def work() -> dict[str, Any]:
            chosen = paths.pick_save_file(message.get("initialDir"), message.get("filename") or "Download",
                                          message.get("extension") or "")
            return {"type": "save_file_picked", "cancelled": chosen is None, **(chosen or {})}
        self._dispatch(request_id, work)

    def _on_open_path(self, message: dict[str, Any], request_id: str) -> None:
        target = message.get("path") or ""
        if not isinstance(target, str) or not target.strip():
            self.error("No path was given.", "bad_request", request_id or None)
            return
        known = self._manager.list_jobs()
        if target not in {j.filename for j in known} | {j.output_dir for j in known} | {self.settings["defaultDownloadDir"]}:
            raise ValueError("Only download files and folders can be opened.")
        ok = paths.reveal_path(target)
        if not ok:
            raise OSError("The downloaded file is missing, or the file manager could not be opened.")
        self.send(
            {
                "type": "ack",
                "requestId": request_id,
                "opened": ok,
                "message": None if ok else "Could not open the file manager.",
            }
        )

    def _on_update_settings(self, message: dict[str, Any], request_id: str) -> None:
        incoming = message.get("settings") or {}
        if not isinstance(incoming, dict):
            raise ValueError("Settings must be an object.")
        updated = dict(self.settings)
        for key, value in incoming.items():
            if key not in DEFAULT_SETTINGS:
                continue
            if key == "maxConcurrentDownloads":
                try:
                    value = max(1, min(8, int(value)))
                except (TypeError, ValueError):
                    raise ValueError("Concurrency must be a number between 1 and 8.")
            if key == "defaultFormat" and (not isinstance(value, str) or not value or len(value) > 500):
                raise ValueError("Enter a valid format selector (up to 500 characters).")
            if key == "notifyOnComplete" and not isinstance(value, bool):
                raise ValueError("Notification preference must be true or false.")
            if key == "defaultDownloadDir":
                # An empty value means "keep using what we have": falling back
                # to the OS default here would silently move the user's folder.
                if not value:
                    continue
                try:
                    value = paths.normalize_output_dir(str(value))
                except OSError as exc:
                    self.error(
                        f"That folder cannot be used: {exc}", "bad_request", request_id or None
                    )
                    return
            updated[key] = value
        with self._settings_lock:
            target = paths.state_dir() / "settings.json"
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(updated), encoding="utf-8")
            os.replace(temporary, target)
            self.settings = updated
        limit = self._manager.set_max_concurrent(int(self.settings["maxConcurrentDownloads"]))
        self.send(
            {
                "type": "settings",
                "requestId": request_id,
                "settings": self.settings,
                "maxConcurrentDownloads": limit,
            }
        )

    def _on_shutdown(self, message: dict[str, Any], request_id: str) -> None:
        self.send({"type": "bye", "requestId": request_id})
        self._stopping = True

    # -------------------------------------------------------------- lifecycle

    def ready_payload(self) -> dict[str, Any]:
        return {
            "type": "host_ready",
            "hostName": HOST_NAME,
            "hostVersion": HOST_VERSION,
            "ytDlpVersion": yt_dlp.version.__version__,
            "pythonVersion": sys.version.split()[0],
            "ffmpegFound": self.ffmpeg_found,
            "ffmpegVersion": self._ffmpeg_version,
            "jsRuntimes": list(probe.base_options()["js_runtimes"]),
            "challengeSolver": getattr(yt_dlp_ejs, "version", None),
            "survivesBrowserExit": True,
            "platform": sys.platform,
            "stateDir": str(paths.state_dir()),
            "settings": self.settings,
        }

    def serve(self) -> int:
        stream = sys.stdin.buffer
        self.send(self.ready_payload())
        while not self._stopping:
            try:
                message = read_message(stream)
            except ProtocolError as exc:
                _log(f"protocol error: {exc}")
                self.error(f"Protocol error: {exc}", "protocol_error")
                break
            if message is None:
                _log("extension closed the connection")
                break
            self.handle(message)
        # Order matters: stop the workers *first*, so cancel events are set and
        # no in-flight download can still reach _finish(COMPLETE) afterwards.
        # Anything still unfinished is then recorded as interrupted rather than
        # left dangling as "downloading" forever.
        self.stop()
        return 0

    def stop(self) -> None:
        self._manager.shutdown()
        try:
            for job in self._manager.abandon(
                "Interrupted: the connection to Firefox closed before this job finished."
            ):
                _log(f"abandoned job {job.id}")
        except Exception as exc:  # a dead pipe must not skip the cleanup below
            _log(f"abandon failed: {exc!r}")
        try:
            self._store.close()
        except Exception:
            pass
        self._pool.shutdown()
        try:
            sys.stdout.flush()
        except Exception:
            pass


def main() -> int:
    # stdout is the protocol channel. Anything else written there corrupts it,
    # so move the real stdout out of the way before any other import can print,
    # and keep a handle on the genuine pipe.
    try:
        protocol_stream = sys.__stdout__.buffer
    except AttributeError:  # launched with no stdout at all
        _log("no stdout: the native messaging host must be started with a pipe")
        return 1
    sys.stdout = sys.stderr
    # Windows pipe handles must use binary mode; text translation corrupts
    # arbitrary bytes in native message length headers.
    if sys.platform == "win32":
        import msvcrt
        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(protocol_stream.fileno(), os.O_BINARY)
    try:
        from . import service
        if "--service" in sys.argv:
            return service.serve()
        if "--stop-service" in sys.argv:
            return service.stop_service()
        if "--stdio" in sys.argv:
            return Host(protocol_stream).serve()
        return service.bridge(sys.stdin.buffer, protocol_stream)
    except Exception as exc:
        _log(f"host failed to start: {traceback.format_exc()}")
        MessageWriter(protocol_stream).send(
            {
                "type": "host_error",
                "message": f"The yt-dlp host could not start: {exc}",
                "code": "startup_failed",
                "hint": "Re-run the installer, or check ~/.local/share/ytdlp-host/host.log",
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
