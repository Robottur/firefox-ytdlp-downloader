"""Download queue: concurrency, cancellation, playlist fan-out, persistence.

Design notes
------------
*One yt-dlp process per job, one job per file.* A playlist is not downloaded by
a single yt-dlp call: the host expands it into a parent job plus one child job
per selected entry, so every file gets its own progress bar, its own cancel
button, and its own row in the database. This also makes cancellation trivial --
yt-dlp aborts a download cleanly when a progress hook raises
``yt_dlp.utils.DownloadCancelled``.

Concurrency is a gate, not a thread pool size. A fixed set of worker threads
waits on a resizable gate before transferring a file, so ``max_concurrent`` can
change between downloads without restarting anything.

Threading rules in this module, because every field below is touched from the
request thread *and* the worker threads:

* ``_jobs`` and ``_cancels`` are only mutated while holding ``_lock``, and are
  always iterated through ``list(...)`` -- a dict view iterator raises
  ``RuntimeError`` if another thread inserts mid-loop.
* A job moves from ``QUEUED`` to a running state exactly once, under ``_lock``.
  ``cancel()`` relies on that: it can only finish a job itself if no worker has
  claimed it yet, otherwise it raises the cancel event and lets the worker exit.
* Every wire message goes through the injected ``send``, which is expected to
  swallow write errors: a dead extension must not turn into a download failure.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from .sessions import using_session
from .diagnostics import safe_error
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl
from typing import Any, Callable

import yt_dlp
from yt_dlp.utils import DownloadCancelled, DownloadError

from . import paths, probe

#: Job lifecycle. ``preparing`` is a playlist parent that is still expanding.
QUEUED = "queued"
PREPARING = "preparing"
DOWNLOADING = "downloading"
COMPLETE = "complete"
ERROR = "error"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"

TERMINAL = (COMPLETE, ERROR, CANCELLED)
ACTIVE = (QUEUED, PREPARING, DOWNLOADING)

#: Progress messages are throttled to this many per second per job.
PROGRESS_INTERVAL = 0.5

#: Worker threads. Must be >= the highest concurrency the user can select.
WORKER_COUNT = 8


class ConcurrencyGate:
    """A semaphore whose limit can be changed, and which shutdown can unblock."""

    def __init__(self, limit: int) -> None:
        self._cond = threading.Condition()
        self._limit = max(1, int(limit))
        self._active = 0
        self._stopped = False

    def acquire(self, stop: threading.Event | None = None) -> bool:
        """Take a slot. Returns False if the manager is shutting down."""
        with self._cond:
            while self._active >= self._limit and not self._stopped:
                if stop is not None and stop.is_set():
                    return False
                self._cond.wait(0.2)
            if self._stopped or (stop is not None and stop.is_set()):
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._cond:
            self._active -= 1
            self._cond.notify_all()

    def set_limit(self, limit: int) -> None:
        with self._cond:
            self._limit = max(1, int(limit))
            self._cond.notify_all()

    def stop(self) -> None:
        """Release every waiter; in-flight downloads keep their slot."""
        with self._cond:
            self._stopped = True
            self._cond.notify_all()

    @property
    def active(self) -> int:
        with self._cond:
            return self._active

    @property
    def limit(self) -> int:
        with self._cond:
            return self._limit


def recoverable_source(url: str | None) -> bool:
    if not url:
        return True
    parsed = urlsplit(url)
    if parsed.fragment:
        return False
    if not parsed.query:
        return True
    # Keep functional URLs intact in memory. Only known non-secret YouTube
    # source identifiers are persisted; unknown query parameters may be signed.
    host = (parsed.hostname or "").lower()
    return (host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
            and all(key in {"v", "list", "index", "start", "end", "t"}
                    for key, _ in parse_qsl(parsed.query, keep_blank_values=True)))


@dataclass
class Job:
    """One download (or one playlist). Persisted as a single SQLite row."""

    id: str
    url: str
    kind: str = "video"  # "video" | "playlist"
    title: str = ""
    status: str = QUEUED
    phase: str = ""  # free-form detail shown next to the status
    format_id: str = ""
    audio_only: bool = False
    subtitle_langs: list[dict[str, Any]] = field(default_factory=list)
    output_dir: str = ""
    output_template: str = ""
    overwrite: bool = False
    playlist_mode: str = "single"  # "single" | "all" | "select"
    entry_index: int | None = None
    entry_count: int | None = None
    entry_total_bytes: int = 0
    entry_done_bytes: int = 0
    group_id: str | None = None
    parent_url: str | None = None
    playlist_item: int | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)
    percent: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed: float = 0.0
    eta: int | None = None
    filename: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    auth_required: bool = False
    _session_cookies: list | None = field(default=None, repr=False)
    _last_emit: float = 0.0

    def to_wire(self) -> dict[str, Any]:
        return {
            "jobId": self.id,
            "authRequired": self.auth_required,
            "recoveryAvailable": recoverable_source(self.url) and recoverable_source(self.parent_url),
            "url": self.url,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "phase": self.phase,
            "formatId": self.format_id,
            "audioOnly": self.audio_only,
            "subtitles": self.subtitle_langs,
            "outputDir": self.output_dir,
            "playlistMode": self.playlist_mode,
            "entryIndex": self.entry_index,
            "entryCount": self.entry_count,
            "groupId": self.group_id,
            "isGroup": self.kind == "playlist",
            "percent": round(self.percent, 1),
            "downloadedBytes": self.downloaded_bytes,
            "totalBytes": self.total_bytes,
            "speed": round(self.speed, 0),
            "eta": self.eta,
            "filename": self.filename,
            "error": self.error[:4000] if self.error else None,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    def to_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "auth_required": int(self.auth_required),
            "url": self.url,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "phase": self.phase,
            "format_id": self.format_id,
            "audio_only": int(self.audio_only),
            "subtitle_langs": json.dumps(self.subtitle_langs),
            "output_dir": self.output_dir,
            "output_template": self.output_template,
            "overwrite": int(self.overwrite),
            "playlist_mode": self.playlist_mode,
            "entry_index": self.entry_index,
            "entry_count": self.entry_count,
            "group_id": self.group_id,
            "parent_url": self.parent_url,
            "playlist_item": self.playlist_item,
            "entries": json.dumps([{"index": e["index"]} for e in self.entries[:2000] if isinstance(e.get("index"), int)]),
            "percent": self.percent,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "speed": self.speed,
            "eta": self.eta,
            "filename": self.filename,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        auth_required=bool(int(row["auth_required"] or 0)),
        url=row["url"],
        kind=row["kind"],
        title=row["title"],
        status=row["status"],
        phase=row["phase"],
        format_id=row["format_id"],
        audio_only=bool(row["audio_only"]),
        subtitle_langs=json.loads(row["subtitle_langs"] or "[]"),
        output_dir=row["output_dir"],
        output_template=row["output_template"],
        overwrite=bool(row["overwrite"]),
        playlist_mode=row["playlist_mode"],
        entry_index=row["entry_index"],
        entry_count=row["entry_count"],
        group_id=row["group_id"],
        parent_url=row["parent_url"],
        playlist_item=row["playlist_item"],
        entries=json.loads(row["entries"] or "[]"),
        percent=row["percent"] or 0.0,
        downloaded_bytes=row["downloaded_bytes"] or 0,
        total_bytes=row["total_bytes"] or 0,
        speed=row["speed"] or 0.0,
        eta=row["eta"],
        filename=row["filename"],
        error=row["error"],
        created_at=row["created_at"] or 0.0,
        updated_at=row["updated_at"] or 0.0,
    )


class JobStore:
    """Thin SQLite wrapper. One connection, one lock, one table."""

    #: SQLite storage class per column, kept next to :meth:`Job.to_row` so the
    #: table definition can never drift from the row dictionary.
    COLUMN_TYPES = {
        "percent": "REAL",
        "speed": "REAL",
        "created_at": "REAL",
        "updated_at": "REAL",
        "audio_only": "INTEGER",
        "overwrite": "INTEGER",
        "auth_required": "INTEGER",
        "entry_index": "INTEGER",
        "entry_count": "INTEGER",
        "playlist_item": "INTEGER",
        "downloaded_bytes": "INTEGER",
        "total_bytes": "INTEGER",
        "eta": "INTEGER",
    }

    def __init__(self, db_path: Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._closed = False
        self._create()

    def _create(self) -> None:
        names = list(Job(id="", url="").to_row().keys())
        columns = ", ".join(f"{n} {self.COLUMN_TYPES.get(n, 'TEXT')}" for n in names)
        # ``id`` is the primary key: the upsert in :meth:`upsert` needs it.
        columns = columns.replace("id TEXT", "id TEXT PRIMARY KEY", 1)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(f"CREATE TABLE IF NOT EXISTS jobs ({columns})")
            if "overwrite" not in {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)")}:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN overwrite INTEGER NOT NULL DEFAULT 0")
            if "auth_required" not in {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)")}:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN auth_required INTEGER NOT NULL DEFAULT 0")
            self._conn.execute("CREATE INDEX IF NOT EXISTS jobs_created ON jobs (created_at DESC)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status)")
            self._conn.execute("CREATE TABLE IF NOT EXISTS history_policy "
                               "(id INTEGER PRIMARY KEY CHECK(id=1), retain INTEGER NOT NULL, migration INTEGER NOT NULL)")
            self._conn.execute("INSERT OR IGNORE INTO history_policy SELECT 1, 0, EXISTS("
                               "SELECT 1 FROM jobs WHERE status IN ('complete','error','cancelled'))")
            self._conn.commit()

    def upsert(self, job: Job) -> None:
        row = job.to_row()
        placeholders = ", ".join("?" for _ in row)
        assignments = ", ".join(f"{name}=excluded.{name}" for name in row if name != "id")
        with self._lock:
            if self._closed:
                # A worker that finishes after shutdown must not raise: the job
                # is simply not recorded anywhere.
                return
            if not recoverable_source(job.url) or not recoverable_source(job.parent_url):
                with self._conn:
                    self._conn.execute("DELETE FROM jobs WHERE id=?", (job.id,))
                return
            if job.status in TERMINAL and not self._conn.execute(
                "SELECT retain FROM history_policy WHERE id=1"
            ).fetchone()[0]:
                with self._conn:
                    self._conn.execute("DELETE FROM jobs WHERE id=?", (job.id,))
                return
            self._conn.execute(
                f"INSERT INTO jobs ({', '.join(row)}) VALUES ({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {assignments}",
                tuple(row.values()),
            )
            self._conn.commit()

    def recent(self, limit: int = 200) -> list[Job]:
        with self._lock:
            if self._closed:
                return []
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_job(r) for r in rows]

    def unfinished(self) -> list[Job]:
        """Every job still marked as running, however old it is."""
        with self._lock:
            if self._closed:
                return []
            rows = self._conn.execute(
                f"SELECT * FROM jobs WHERE status IN ({', '.join('?' * len(ACTIVE))})",
                ACTIVE,
            ).fetchall()
        return [_row_to_job(r) for r in rows]

    def recoverable(self) -> list[Job]:
        with self._lock:
            if self._closed:
                return []
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','preparing','downloading','interrupted')"
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            if self._closed:
                return None
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def history_policy(self) -> dict[str, Any]:
        with self._lock:
            retain, migration = self._conn.execute(
                "SELECT retain, migration FROM history_policy WHERE id=1"
            ).fetchone()
            count = self._conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('complete','error','cancelled')"
            ).fetchone()[0]
            return {"retainFinishedHistory": bool(retain), "historyMigrationPending": bool(migration),
                    "retainedHistoryCount": count}

    def configure_history(self, enabled: bool, existing: str | None = None) -> dict[str, Any]:
        if type(enabled) is not bool or existing not in (None, "keep", "remove"):
            raise ValueError("Choose a history preference and keep or remove existing records.")
        with self._lock, self._conn:
            retain, migration = self._conn.execute(
                "SELECT retain, migration FROM history_policy WHERE id=1"
            ).fetchone()
            count = self._conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('complete','error','cancelled')"
            ).fetchone()[0]
            if count and existing is None and (migration or (retain and not enabled)):
                raise ValueError("Choose whether to keep or remove existing finished records.")
            if existing == "remove":
                self._conn.execute("DELETE FROM jobs WHERE status IN ('complete','error','cancelled')")
            self._conn.execute("UPDATE history_policy SET retain=?, migration=0 WHERE id=1", (int(enabled),))
        return self.history_policy()

    def finished_ids(self, include_interrupted: bool = False) -> set[str]:
        statuses = (*TERMINAL, INTERRUPTED) if include_interrupted else TERMINAL
        with self._lock:
            if self._closed:
                return set()
            rows = self._conn.execute(
                f"SELECT id FROM jobs WHERE status IN ({','.join('?' for _ in statuses)}) AND "
                "(group_id IS NULL OR group_id NOT IN "
                "(SELECT id FROM jobs WHERE status IN ('queued','preparing','downloading')))", statuses
            ).fetchall()
        return {row[0] for row in rows}

    def discard(self, job_ids: set[str]) -> None:
        with self._lock:
            if self._closed:
                if job_ids:
                    raise RuntimeError("Cannot delete records after the companion store closes.")
                return
            with self._conn:
                self._conn.executemany("DELETE FROM jobs WHERE id=?", ((jid,) for jid in job_ids))

    def delete_finished(self) -> int:
        ids = self.finished_ids()
        self.discard(ids)
        return len(ids)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()


class JobManager:
    """Owns the queue, the workers and the job table.

    ``send`` receives wire messages and is called from worker threads, so it must
    be thread-safe and must not raise: :class:`protocol.MessageWriter` is safe,
    and :meth:`main.Host.send` adds the error guard.
    """

    def __init__(
        self,
        store: JobStore,
        send: Callable[[dict[str, Any]], None],
        ffmpeg_found: bool,
        default_dir: str | Callable[[], str] = "",
        max_concurrent: int = 2,
        log: Callable[[str], None] = paths.write_log,
    ) -> None:
        self._store = store
        self._send = send
        self._ffmpeg = ffmpeg_found
        # A callable so that changing the setting in the options page applies to
        # jobs queued afterwards, without rebuilding the manager.
        self._default_dir = default_dir if callable(default_dir) else (lambda: default_dir)
        self._log = log
        self._gate = ConcurrencyGate(max_concurrent)
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._queue: deque[str] = deque()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._started = False

    # ------------------------------------------------------------------ setup

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for index in range(WORKER_COUNT):
            thread = threading.Thread(
                target=self._worker, name=f"ytdl-worker-{index}", daemon=True
            )
            thread.start()
            self._workers.append(thread)

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the workers.

        Every cancel event is set first, so an in-flight yt-dlp download aborts
        on its next progress tick instead of being waited out. Waiters parked on
        the concurrency gate are released too, otherwise a saturated queue would
        keep threads alive long past the join deadline.
        """
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        with self._lock:
            for event in list(self._cancels.values()):
                event.set()
        self._gate.stop()
        deadline = time.time() + timeout
        for thread in self._workers:
            thread.join(max(0.0, deadline - time.time()))

    def set_max_concurrent(self, limit: int) -> int:
        self._gate.set_limit(limit)
        return self._gate.limit

    def recover(self) -> list[Job]:
        """Mark jobs that were mid-flight when the host last exited.

        Queried by status rather than by recency: a job stuck in ``downloading``
        from last week must not be left looking live forever.
        """
        stale: list[Job] = []
        for job in self._store.unfinished():
            job.status = INTERRUPTED
            job.phase = "Interrupted"
            job.error = "Interrupted: the host was closed while this job was running."
            job.updated_at = time.time()
            self._store.upsert(job)
            with self._lock:
                self._jobs[job.id] = job
            stale.append(job)
        return stale

    def abandon(self, reason: str) -> list[Job]:
        """Mark every unfinished in-memory job as failed.

        Called when the connection to Firefox goes away: those downloads are
        gone with the process, and leaving them as "downloading" in the queue
        would be a lie.
        """
        with self._lock:
            candidates = [j for j in list(self._jobs.values()) if j.status not in TERMINAL]
        dropped: list[Job] = []
        for job in candidates:
            job.status = INTERRUPTED
            job.phase = "Interrupted"
            job.error = reason
            job.speed = 0.0
            job.eta = None
            job.updated_at = time.time()
            self._store.upsert(job)
            dropped.append(job)
        return dropped

    # -------------------------------------------------------------- public API

    def submit(self, spec: dict[str, Any]) -> Job:
        """Queue a download described by the extension's ``download_start``."""
        url = probe.validate_url(spec.get("url", ""))
        if spec.get("authRequired") and not spec.get("_sessionCookies"):
            raise ValueError("This download needs fresh signed-in consent. Read its source with a new session first.")
        mode = spec.get("playlistMode") or "single"
        if mode not in ("single", "all", "select"):
            mode = "single"
        if mode == "select" and not spec.get("entryIndexes"):
            raise ValueError("Playlist mode 'select' requires at least one entry index.")
        indexes = spec.get("entryIndexes") or []
        if not isinstance(indexes, list) or any(type(i) is not int or i < 0 for i in indexes):
            raise ValueError("Playlist entry indexes must be nonnegative integers.")
        indexes = sorted(set(indexes))
        if spec.get("playlistItem") is not None and (type(spec["playlistItem"]) is not int or spec["playlistItem"] < 1):
            raise ValueError("Playlist position must be a positive integer.")
        subtitles = spec.get("subtitles") or []
        if not isinstance(subtitles, list) or len(subtitles) > 100 or any(
            not isinstance(s, dict) or not isinstance(s.get("lang"), str) or not 1 <= len(s["lang"]) <= 100
            for s in subtitles
        ):
            raise ValueError("Choose at most 100 valid subtitle languages.")
        format_id = spec.get("formatId") or ("bestaudio/best" if spec.get("audioOnly") else "mp4")
        if not isinstance(format_id, str) or len(format_id) > 500:
            raise ValueError("Invalid format selector.")
        probe.download_options({**spec, "formatId": format_id}, self._ffmpeg)

        filename = spec.get("outputFilename")
        if filename and mode != "single":
            raise ValueError("Choose a folder for a playlist; each video needs its own filename.")
        output_dir = paths.normalize_output_dir(spec.get("outputDir") or self._default_dir())
        output_template = paths.filename_template(filename) if filename else paths.safe_output_template(spec.get("outputTemplate"))
        overwrite = spec.get("overwrite") is True
        if filename and (Path(output_dir) / filename).exists() and not overwrite:
            raise ValueError("That file exists. Use Save As to confirm replacement, or choose a different filename.")
        job = Job(
            id=spec.get("jobId") or uuid.uuid4().hex[:12],
            auth_required=bool(spec.get("authRequired")),
            _session_cookies=spec.get("_sessionCookies"),
            url=url,
            kind="playlist" if mode in ("all", "select") else "video",
            title=(spec.get("title") or "")[:300],
            format_id=format_id,
            audio_only=bool(spec.get("audioOnly")),
            subtitle_langs=[{"lang": s["lang"], "auto": bool(s.get("auto"))} for s in subtitles],
            output_dir=output_dir,
            output_template=output_template,
            overwrite=overwrite,
            playlist_mode=mode,
            parent_url=probe.validate_url(spec["parentUrl"]) if spec.get("parentUrl") else None,
            playlist_item=spec.get("playlistItem"),
        )
        if mode in ("all", "select"):
            job.entry_count = len(indexes) if mode == "select" else None
            job.entries = [{"index": i} for i in indexes] if mode == "select" else []
            job.phase = "Waiting to expand playlist…"
        else:
            job.phase = "Queued"

        with self._lock:
            if not isinstance(job.id, str) or len(job.id) > 100:
                raise ValueError("Invalid job id.")
            if job.id in self._jobs or self._store.get(job.id):
                raise ValueError("That job id already exists.")
            if filename and any(j.status in ACTIVE and j.output_dir == output_dir and j.output_template == output_template
                                for j in self._jobs.values()):
                raise ValueError("A download is already using that filename. Choose another name.")
            self._jobs[job.id] = job
            # setdefault: reusing a live job id must not drop a pending cancel.
            self._cancels.setdefault(job.id, threading.Event())
            self._store.upsert(job)
            self._emit_job(job)
            self._enqueue(job.id)
        return job

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id) or self._store.get(job_id)
            if job is None or job.status in TERMINAL:
                return False
            event = self._cancels.setdefault(job_id, threading.Event())
            if job.kind == "playlist":
                event.set()
                for child in list(self._jobs.values()):
                    if child.group_id == job.id and child.status not in TERMINAL:
                        self.cancel(child.id)
            if job.status == QUEUED:
                # No worker has claimed it yet, so nothing is running: finish it
                # here. If a worker is mid-claim it will see the event and bail
                # out on its first progress tick instead.
                event.set()
                self._finish_locked(job, CANCELLED, phase="Cancelled")
                return True
            event.set()
            job.phase = "Cancelling…"
            self._emit_job(job)
        self._log(f"cancel requested for {job_id}")
        return True

    def list_jobs(self, limit: int = 200) -> list[Job]:
        """Merge the in-memory queue with persisted history, active first."""
        with self._lock:
            live = dict(self._jobs)
        merged: dict[str, Job] = {j.id: j for j in self._store.recent(limit)}
        # Recoverable records must not disappear behind a display limit.
        merged.update({j.id: j for j in self._store.recoverable()})
        merged.update(live)
        jobs = sorted(merged.values(), key=lambda j: j.created_at, reverse=True)
        active = [j for j in jobs if j.status not in TERMINAL]
        done = [j for j in jobs if j.status in TERMINAL]
        selected = {j.id: j for j in active + done[:limit]}
        # Include ancestors even if a large playlist pushed them out of history.
        for job in list(selected.values()):
            if job.group_id and job.group_id not in selected:
                parent = merged.get(job.group_id) or self._store.get(job.group_id)
                if parent:
                    selected[parent.id] = parent
        return list(selected.values())

    def clear_finished(self, include_interrupted: bool = False) -> int:
        with self._lock:
            ids = self._store.finished_ids(include_interrupted)
            statuses = (*TERMINAL, INTERRUPTED) if include_interrupted else TERMINAL
            for job_id, job in list(self._jobs.items()):
                parent = self._jobs.get(job.group_id) if job.group_id else None
                if job.status in statuses and (not parent or parent.status not in ACTIVE):
                    ids.add(job_id)
            # Only forget memory after SQLite confirms deletion; failures reach the UI.
            self._store.discard(ids)
            for job_id in ids:
                self._jobs.pop(job_id, None)
                self._cancels.pop(job_id, None)
            return len(ids)

    def dismiss(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id) or self._store.get(job_id)
            if job is None:
                return False
            parent = (self._jobs.get(job.group_id) or self._store.get(job.group_id)) if job.group_id else None
            if job.status in ACTIVE or (parent and parent.status in ACTIVE):
                raise ValueError("Wait for or cancel the active download before dismissing its record.")
            children = [j for j in self.list_jobs(limit=2**31-1) if j.group_id == job_id]
            if any(j.status in ACTIVE for j in children):
                raise ValueError("Wait for or cancel the playlist before dismissing its records.")
            ids = {job_id, *(j.id for j in children)}
            self._store.discard(ids)
            for jid in ids:
                self._jobs.pop(jid, None)
                self._cancels.pop(jid, None)
            return True

    def has_active_jobs(self) -> bool:
        with self._lock:
            return any(j.status in ACTIVE for j in self._jobs.values())

    def retry(self, job_id: str, cookies: list | None = None) -> Job:
        job = self._jobs.get(job_id) or self._store.get(job_id)
        if job is None or job.status not in (*TERMINAL, INTERRUPTED):
            raise ValueError("Only finished or interrupted jobs can be retried.")
        spec = job.to_wire()
        spec.pop("jobId")
        spec["outputTemplate"] = job.output_template
        spec["overwrite"] = False
        if cookies:
            spec["_sessionCookies"] = cookies
        spec["entryIndexes"] = [e["index"] for e in job.entries]
        spec["parentUrl"] = job.parent_url
        spec["playlistItem"] = job.playlist_item
        return self.submit(spec)

    # ------------------------------------------------------------- internals

    def _enqueue(self, job_id: str) -> None:
        with self._cond:
            self._queue.append(job_id)
            self._cond.notify()

    def _claim(self, job_id: str) -> Job | None:
        """Take ownership of a queued job, or return None.

        This is the point where ``cancel()`` and a worker race. Both go through
        the lock, so exactly one of them wins: either the cancel finishes the job
        and the worker never sees it, or the worker claims it and the cancel has
        to go through the cancel event.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != QUEUED:
                return None
            job.status = DOWNLOADING
            return job

    def _worker(self) -> None:
        while not self._stop.is_set():
            with self._cond:
                while not self._queue and not self._stop.is_set():
                    self._cond.wait(1.0)
                if self._stop.is_set():
                    return
                job_id = self._queue.popleft()
            job = self._claim(job_id)
            if job is None:
                continue
            try:
                if job.auth_required and not job._session_cookies:
                    raise ValueError("This download needs fresh signed-in consent.")
                with using_session(job._session_cookies):
                    if job.kind == "playlist":
                        self._plan_playlist(job)
                    else:
                        self._run_download(job)
            except probe.ProbeError as exc:
                self._finish(job, ERROR, error=safe_error(exc))
            except Exception as exc:  # never let a worker thread die
                self._log(f"worker error on {job_id}: {exc!r}")
                self._finish(job, ERROR, error=safe_error(exc))

    # -- playlist expansion -------------------------------------------------

    def _plan_playlist(self, job: Job) -> None:
        """Resolve the entry list, then enqueue one child job per entry."""
        cancel = self._cancels.get(job.id) or threading.Event()
        self._set_status(job, PREPARING, phase="Reading playlist entries…")
        self._emit_job(job)

        try:
            listing = probe.list_entries(job.url)
            all_entries = list(listing.get("entries") or [])
            offset = listing.get("nextOffset")
            wanted_max = max((e["index"] for e in job.entries), default=-1)
            while offset is not None and (job.playlist_mode == "all" or offset <= wanted_max):
                if cancel.is_set() or self._stop.is_set():
                    self._finish(job, CANCELLED, phase="Cancelled")
                    return
                page = probe.list_entries(job.url, offset=offset)
                all_entries.extend(page.get("entries") or [])
                next_offset = page.get("nextOffset")
                if next_offset is not None and next_offset <= offset:
                    raise probe.ProbeError("The site returned a repeating playlist page.")
                offset = next_offset
            listing = {**listing, "entries": all_entries, "truncated": False}
        except probe.ProbeError as exc:
            self._finish(job, ERROR, error=safe_error(exc))
            return
        if cancel.is_set() or self._stop.is_set():
            self._finish(job, CANCELLED, phase="Cancelled")
            return

        entries = [e for e in (listing.get("entries") or []) if e]
        if job.playlist_mode == "select":
            wanted = {e["index"] for e in job.entries}  # indexes from the popup
            entries = [e for e in entries if e.get("index") in wanted]
        if not entries:
            self._finish(job, ERROR, error="No playable entries were found in that playlist.")
            return

        job.entry_count = len(entries)
        job.title = job.title or f"Playlist ({len(entries)} videos)"
        notes = []
        if listing.get("truncated"):
            notes.append(f"first {len(entries)} of {listing.get('total')}")
        # Past the expansion the group no longer prepares anything: it is now
        # waiting on its children.
        self._set_status(
            job,
            DOWNLOADING,
            phase=f"Expanded {len(entries)} videos" + (f" ({', '.join(notes)})" if notes else ""),
        )
        # The group record goes out before its children, so the extension never
        # has to hold a child whose parent it has not seen.
        self._emit_job(job)

        # Publish all children under the same lock. A fast first child must not
        # finish the group while the remaining children are still being added.
        with self._lock:
            for position, entry in enumerate(entries):
                self._spawn_child(job, entry, position)
            self._refresh_group(job)

    def _spawn_child(self, group: Job, entry: dict[str, Any], position: int) -> None:
        index = entry.get("index", position)
        child = Job(
            # Position, not index: a playlist can list the same video twice and
            # two children must not share an id.
            id=f"{group.id}-{position + 1}",
            auth_required=group.auth_required,
            _session_cookies=group._session_cookies,
            url=entry.get("url") or group.url,
            kind="video",
            title=entry.get("title") or "",
            format_id=group.format_id,
            audio_only=group.audio_only,
            subtitle_langs=group.subtitle_langs,
            output_dir=group.output_dir,
            output_template=group.output_template,
            playlist_mode="single",
            group_id=group.id,
            parent_url=group.url,
            playlist_item=(index + 1) if isinstance(index, int) else None,
            entry_index=index if isinstance(index, int) else position,
            entry_count=group.entry_count,
            phase="Queued",
        )
        with self._lock:
            self._jobs[child.id] = child
            self._cancels[child.id] = threading.Event()
            if self._cancels[group.id].is_set():
                self._cancels[child.id].set()
            self._store.upsert(child)
            self._emit_job(child)
            self._enqueue(child.id)

    # -- single download ----------------------------------------------------

    def _prepare(self, job: Job) -> None:
        """Last chance to turn a job's URL into something directly downloadable.

        Flat extractors sometimes hand back a playlist URL (or nothing) for an
        individual entry, in which case the entry has to be re-resolved on its
        own. Doing this before the download step keeps the URL fix-up
        independent of how the file is fetched.
        """
        if job.playlist_item and job.url == job.parent_url:
            job.url = probe.resolve_entry_url(job.parent_url or job.url, job.playlist_item)
            self._store.upsert(job)

    def _run_download(self, job: Job) -> None:
        cancel = self._cancels.get(job.id) or threading.Event()

        opts = probe.download_options(
            {
                "formatId": job.format_id,
                "audioOnly": job.audio_only,
                "subtitles": job.subtitle_langs,
            },
            self._ffmpeg,
        )
        opts["outtmpl"] = str(Path(job.output_dir) / job.output_template)
        if job.overwrite:
            opts["overwrites"] = True
            opts["continuedl"] = False
        opts["progress_hooks"] = [self._make_hook(job, cancel)]
        opts["postprocessor_hooks"] = [self._make_post_hook(job)]
        opts["logger"] = _FileLogger(self._log)
        opts["noprogress"] = True
        opts["quiet"] = True

        if not self._gate.acquire(cancel):
            self._finish(job, CANCELLED, phase="Cancelled")
            return
        try:
            if cancel.is_set() or self._stop.is_set():
                raise DownloadCancelled("cancelled before starting")
            self._prepare(job)
            if job.playlist_item and job.url == job.parent_url:
                opts["playlist_items"] = str(job.playlist_item)
            self._set_status(job, DOWNLOADING, phase="Starting…")
            self._emit_job(job)
            self._emit_progress(job, force=True)
            with probe.youtube_dl(opts) as ydl:
                if job.subtitle_langs:
                    probe.select_subtitles(ydl, job.subtitle_langs)
                info = ydl.extract_info(job.url, download=True) or {}
                # Resolved inside the ``with``: the final path is only available
                # while the instance is open, and any failure here must be
                # reported against the job rather than escaping to the worker.
                job.filename = _final_path(ydl, info)
                job.title = info.get("title") or job.title
                if not job.filename:
                    raise DownloadError("The site did not produce a downloadable file.")
        except DownloadCancelled:
            self._finish(job, CANCELLED, phase="Cancelled")
            return
        except DownloadError as exc:
            self._finish(job, ERROR, error=safe_error(exc))
            return
        except Exception as exc:  # network stack, disk errors, ffmpeg failures
            self._log(f"download failed {job.id}: {exc!r}")
            self._finish(job, ERROR, error=safe_error(exc))
            return
        finally:
            self._gate.release()

        if cancel.is_set() or self._stop.is_set():
            self._finish(job, CANCELLED, phase="Cancelled")
            return

        self._finish(job, COMPLETE, phase="Done")

    def _make_hook(self, job: Job, cancel: threading.Event) -> Callable[[dict], None]:
        def hook(status: dict) -> None:
            if cancel.is_set():
                raise DownloadCancelled("cancelled from the extension")
            state = status.get("status")
            if state == "downloading":
                job.downloaded_bytes = _as_int(status.get("downloaded_bytes"))
                total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
                job.total_bytes = _as_int(total)
                job.speed = _as_float(status.get("speed"))
                job.eta = _as_int(status["eta"]) if status.get("eta") is not None else None
                if job.total_bytes > 0:
                    job.percent = min(100.0, job.downloaded_bytes * 100.0 / job.total_bytes)
                else:
                    job.percent = 0.0
                job.phase = "Downloading"
                if status.get("filename"):
                    job.filename = status["filename"]
                self._emit_progress(job)
            elif state == "finished":
                if status.get("filename"):
                    job.filename = status["filename"]
                job.percent = 100.0
                self._emit_progress(job, force=True)
            if job.group_id:
                group = self._jobs.get(job.group_id)
                if group and time.monotonic() - group._last_emit >= PROGRESS_INTERVAL:
                    self._refresh_group(group)

        return hook

    def _make_post_hook(self, job: Job) -> Callable[[dict], None]:
        def hook(status: dict) -> None:
            event = self._cancels.get(job.id)
            if event is not None and event.is_set():
                raise DownloadCancelled("cancelled from the extension")
            if status.get("status") == "started" and status.get("postprocessor"):
                # "Merging formats", "Extracting audio", ... ffmpeg work can
                # take a while, so the phase tells the user why it is waiting.
                job.phase = str(status["postprocessor"])
                self._emit_progress(job, force=True)

        return hook

    # -- state transitions --------------------------------------------------

    def _set_status(self, job: Job, status: str, phase: str | None = None) -> None:
        job.status = status
        if phase is not None:
            job.phase = phase
        job.updated_at = time.time()
        self._store.upsert(job)

    def _finish(self, job: Job, status: str, phase: str = "", error: str | None = None) -> None:
        with self._lock:
            self._finish_locked(job, status, phase=phase, error=error)

    def _finish_locked(self, job: Job, status: str, phase: str = "", error: str | None = None) -> None:
        if job.status in TERMINAL:
            return
        if self._stop.is_set():
            status, phase, error = INTERRUPTED, "Interrupted", "Interrupted: the companion exited. Retry to resume."
        job.status = status
        job.phase = phase
        job.error = error
        job.speed = 0.0
        job.eta = None
        job.updated_at = time.time()
        job._session_cookies = None
        if status == COMPLETE:
            job.percent = 100.0
        self._store.upsert(job)
        self._emit_job(job)
        self._emit_progress(job, force=True)
        if status == COMPLETE:
            self._send(
                {"type": "complete", "jobId": job.id, "filepath": job.filename, "title": job.title}
            )
        elif status == ERROR:
            self._send(
                {
                    "type": "error",
                    "jobId": job.id,
                    "message": error or "Unknown error",
                    "code": "download_failed",
                }
            )
        group_id = job.group_id
        if group_id:
            with self._lock:
                group = self._jobs.get(group_id)
            if group is not None:
                self._refresh_group(group)

    def _refresh_group(self, group: Job) -> None:
        """Recompute a playlist parent's progress from its children."""
        with self._lock:
            children = [j for j in list(self._jobs.values()) if j.group_id == group.id]
            if not children or group.status not in (PREPARING, DOWNLOADING, QUEUED):
                self._emit_job(group)
                return

            group.entry_count = group.entry_count or len(children)
            group.entry_done_bytes = sum(j.downloaded_bytes for j in children)
            group.entry_total_bytes = sum(j.total_bytes for j in children)
            group.total_bytes = group.entry_total_bytes
            group.downloaded_bytes = group.entry_done_bytes
            if group.entry_total_bytes > 0:
                group.percent = min(100.0, group.entry_done_bytes * 100.0 / group.entry_total_bytes)
            else:
                done = sum(1 for j in children if j.status == COMPLETE)
                group.percent = done * 100.0 / len(children)
            group.speed = sum(j.speed for j in children if j.status == DOWNLOADING)

            done = [j for j in children if j.status == COMPLETE]
            failed = [j for j in children if j.status == ERROR]
            cancelled = [j for j in children if j.status == CANCELLED]
            settled = len(done) + len(failed) + len(cancelled)

            if settled < len(children):
                # Still in flight: report the tally and wait for the next child.
                group.phase = f"{len(done)} of {len(children)} videos"
                group.updated_at = time.time()
                self._store.upsert(group)
                self._emit_progress(group)
                self._emit_job(group)
                return

            if len(done) == len(children):
                # A playlist's files are separate; the group row offers the
                # folder of the first one rather than pretending there is a
                # single output.
                group.filename = next((j.filename for j in children if j.filename), None)
                self._finish_locked(group, COMPLETE, phase=f"{len(children)} videos")
            elif len(failed) == len(children):
                self._finish_locked(group, ERROR, error=failed[0].error or "All entries failed")
            elif len(cancelled) == len(children):
                self._finish_locked(group, CANCELLED, phase="Cancelled")
            else:
                notes = []
                if failed:
                    notes.append(f"{len(failed)} failed")
                if cancelled:
                    notes.append(f"{len(cancelled)} cancelled")
                summary = f"{len(done)} of {len(children)} videos ({', '.join(notes)})"
                self._finish_locked(group, ERROR if failed else CANCELLED,
                                    phase=summary, error=summary if failed else None)

    # -- wire output --------------------------------------------------------

    def _emit_job(self, job: Job) -> None:
        with self._lock:
            self._send({"type": "job", "job": job.to_wire()})

    def _emit_progress(self, job: Job, force: bool = False) -> None:
        with self._lock:
            self._emit_progress_locked(job, force)

    def _emit_progress_locked(self, job: Job, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - job._last_emit < PROGRESS_INTERVAL:
            return
        job._last_emit = now
        self._send(
            {
                "type": "progress",
                "jobId": job.id,
                "status": job.status,
                "phase": job.phase,
                "percent": round(job.percent, 1),
                "speed": round(job.speed, 0),
                "eta": job.eta,
                "downloadedBytes": job.downloaded_bytes,
                "totalBytes": job.total_bytes,
            }
        )


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _as_float(value: Any) -> float:
    try:
        number = float(value or 0.0)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _final_path(ydl: yt_dlp.YoutubeDL, info: dict[str, Any]) -> str | None:
    """Best guess at the finished file, post-processing included."""
    if info.get("entries"):
        entry = next((e for e in info["entries"] if e), None)
        return _final_path(ydl, entry) if entry else None
    if info.get("filepath"):
        return str(info["filepath"])
    downloads = info.get("requested_downloads") or []
    for entry in reversed(downloads):
        if entry.get("filepath"):
            return str(entry["filepath"])
    for entry in downloads:
        if entry.get("_filename"):
            return str(entry["_filename"])
    if info.get("_filename"):
        return str(info["_filename"])
    try:
        return ydl.prepare_filename(info)
    except Exception:  # pragma: no cover - defensive
        return None


class _FileLogger:
    """Route yt-dlp's own logging into the host log file, never stdout."""

    def __init__(self, log: Callable[[str], None]) -> None:
        self._log = log

    # Progress and debug chatter is not a failure; diagnostics only keep problem codes.
    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        self._log(f"yt-dlp: {msg}")

    def error(self, msg: str) -> None:
        self._log(f"yt-dlp: {msg}")
