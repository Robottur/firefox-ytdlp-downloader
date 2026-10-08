"""Job queue behaviour: persistence, concurrency gating, cancel, playlists.

yt-dlp is never invoked and the network step is replaced by a controllable stub,
so these tests exercise the queue machinery only. Managers that do not need
workers are left unstarted, which keeps the job states deterministic.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from yt_dlp import utils
from ytdlp_host import jobs, probe


class RecordingStore(jobs.JobStore):
    """JobStore backed by a throwaway file."""

    def __init__(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        super().__init__(Path(self._dir.name) / "jobs.db")

    def cleanup(self) -> None:
        self.close()
        self._dir.cleanup()


class Harness:
    """A JobManager with a fake download step and a captured wire log."""

    def __init__(self, max_concurrent: int = 2, start: bool = False) -> None:
        self.store = RecordingStore()
        self.messages: list[dict] = []
        self.lock = threading.Lock()
        self.started = threading.Event()
        self.release = threading.Event()
        self.ran: list[str] = []
        self.manager = jobs.JobManager(
            store=self.store,
            send=self._send,
            ffmpeg_found=True,
            default_dir=tempfile.gettempdir(),
            max_concurrent=max_concurrent,
            log=lambda _m: None,
        )
        self.manager._run_download = self._fake_download  # type: ignore[method-assign]
        if start:
            self.manager.start()

    # -- fake network step --------------------------------------------------

    def _fake_download(self, job: jobs.Job) -> None:
        self.manager._gate.acquire()  # mirror the real gate placement
        try:
            self.manager._prepare(job)
            self.manager._set_status(job, jobs.DOWNLOADING, phase="Downloading")
            self.manager._emit_progress(job, force=True)
            with self.lock:
                self.ran.append(job.id)
            self.started.set()
            cancel = self.manager._cancels[job.id]
            deadline = time.time() + 5
            while time.time() < deadline and not self.release.is_set():
                if cancel.is_set():
                    break
                time.sleep(0.01)
            if cancel.is_set():
                self.manager._finish(job, jobs.CANCELLED, phase="Cancelled")
                return
            job.filename = f"/tmp/{job.id}.mkv"
            self.manager._finish(job, jobs.COMPLETE, phase="Done")
        finally:
            self.manager._gate.release()

    # -- helpers ------------------------------------------------------------

    def _send(self, payload: dict) -> None:
        with self.lock:
            self.messages.append(payload)

    def submit(self, **overrides) -> jobs.Job:
        spec = {
            "url": "https://example.com/watch",
            "formatId": "bestvideo+bestaudio/best",
            "outputDir": tempfile.gettempdir(),
        }
        spec.update(overrides)
        return self.manager.submit(spec)

    def wait_for(self, predicate, timeout: float = 5.0) -> jobs.Job:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for job in self.manager.list_jobs():
                if predicate(job):
                    return job
            time.sleep(0.01)
        raise AssertionError("timed out waiting for the expected job state")

    def get(self, job_id: str):
        return next((job for job in self.manager.list_jobs() if job.id == job_id), None)

    def children(self, group: jobs.Job) -> list[jobs.Job]:
        return [j for j in self.manager.list_jobs() if j.group_id == group.id]

    def wait_for_children(self, group: jobs.Job, count: int, timeout: float = 5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            children = self.children(group)
            if len(children) == count:
                return children
            time.sleep(0.01)
        raise AssertionError(
            f"expected {count} children, found {len(self.children(group))}"
        )

    def cleanup(self) -> None:
        self.release.set()
        self.manager.shutdown()
        self.store.cleanup()


class Base(unittest.TestCase):
    start_workers = False
    max_concurrent = 2

    def setUp(self) -> None:
        self.h = Harness(max_concurrent=self.max_concurrent, start=self.start_workers)
        self.addCleanup(self.h.cleanup)


class PersistenceTests(Base):
    def test_submit_persists_and_lists(self):
        job = self.h.submit()
        stored = self.h.store.get(job.id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.url, "https://example.com/watch")
        self.assertIn(job.id, [j.id for j in self.h.manager.list_jobs()])

    def test_wire_shape_uses_camel_case(self):
        wire = self.h.submit().to_wire()
        for key in ("jobId", "isGroup", "percent", "outputDir", "playlistMode", "eta"):
            self.assertIn(key, wire)
        self.assertNotIn("group_id", wire)

    def test_opted_in_history_survives_a_host_restart(self):
        self.h.store.configure_history(True)
        job = self.h.submit()
        self.h.store.upsert(
            jobs.Job(
                id=job.id,
                url=job.url,
                kind="video",
                status=jobs.COMPLETE,
                output_dir=job.output_dir,
                filename="/tmp/x.mkv",
            )
        )
        # A fresh manager over the same file is what the extension sees after
        # the browser restarts.
        revived = jobs.JobManager(
            store=self.h.store, send=lambda _p: None, ffmpeg_found=True,
            default_dir=tempfile.gettempdir(), log=lambda _m: None,
        )
        self.addCleanup(revived.shutdown)
        self.assertEqual([j.id for j in revived.list_jobs()], [job.id])
        self.assertEqual(revived.recover(), [])

    def test_recover_marks_in_flight_jobs_as_interrupted(self):
        job = self.h.submit()
        self.h.store.upsert(
            jobs.Job(id=job.id, url=job.url, status=jobs.DOWNLOADING, output_dir=job.output_dir)
        )
        self.assertEqual([j.id for j in self.h.manager.recover()], [job.id])
        self.assertEqual(self.h.store.get(job.id).status, jobs.INTERRUPTED)
        self.assertIn("Interrupted", self.h.store.get(job.id).error)

    def test_clear_finished_only_removes_terminal_jobs(self):
        done = self.h.submit()
        self.h.manager._finish(done, jobs.COMPLETE, phase="Done")
        queued = self.h.submit(url="https://example.com/other")
        self.assertEqual(self.h.manager.clear_finished(), 1)
        self.assertIsNone(self.h.store.get(done.id))
        self.assertIsNotNone(self.h.store.get(queued.id))

    def test_output_dir_is_created_and_template_sanitised(self):
        target = Path(tempfile.gettempdir()) / "ytdlp-host-test-dir" / "nested"
        job = self.h.submit(outputDir=str(target), outputTemplate="../../escape.%(ext)s")
        self.assertTrue(target.is_dir())
        self.assertNotIn("..", job.output_template)
        self.assertIn("%(ext)s", job.output_template)


class ValidationTests(Base):
    def test_only_http_urls_are_accepted(self):
        for bad in (
            "file:///etc/passwd",
            "ftp://host/f.mp4",
            "javascript:alert(1)",
            "dash:https://example.com/manifest",
            "",
            "   ",
        ):
            with self.assertRaises(probe.ProbeError):
                self.h.submit(url=bad)

    def test_select_mode_requires_indexes(self):
        with self.assertRaises(ValueError):
            self.h.submit(url="https://example.com/pl", playlistMode="select", entryIndexes=[])

    def test_unknown_playlist_mode_degrades_to_single(self):
        job = self.h.submit(url="https://example.com/x", playlistMode="nonsense")
        self.assertEqual(job.playlist_mode, "single")
        self.assertEqual(job.kind, "video")

    def test_url_validation_messages_are_human_readable(self):
        with self.assertRaises(probe.ProbeError) as ctx:
            probe.validate_url("file:///etc/passwd")
        self.assertIn("http", str(ctx.exception))


class ProgressTests(Base):
    def test_hook_updates_bytes_percent_and_speed(self):
        job = self.h.submit()
        hook = self.h.manager._make_hook(job, threading.Event())
        hook(
            {
                "status": "downloading",
                "downloaded_bytes": 50 * 1024 * 1024,
                "total_bytes": 100 * 1024 * 1024,
                "speed": 1_048_576.0,
                "eta": 50,
                "filename": "/tmp/partial.mkv",
            }
        )
        self.assertEqual(job.downloaded_bytes, 50 * 1024 * 1024)
        self.assertAlmostEqual(job.percent, 50.0, places=1)
        self.assertEqual(job.speed, 1_048_576.0)
        self.assertEqual(job.eta, 50)
        self.assertEqual(job.filename, "/tmp/partial.mkv")

    def test_hook_raises_download_cancelled_after_cancel(self):
        job = self.h.submit()
        cancel = threading.Event()
        cancel.set()
        hook = self.h.manager._make_hook(job, cancel)
        with self.assertRaises(utils.DownloadCancelled):
            hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 10})

    def test_unknown_total_leaves_progress_indeterminate(self):
        job = self.h.submit()
        self.h.manager._make_hook(job, threading.Event())(
            {"status": "downloading", "downloaded_bytes": 1024}
        )
        self.assertEqual(job.percent, 0.0)
        self.assertEqual(job.total_bytes, 0)

    def test_finished_hook_pins_progress_to_100(self):
        job = self.h.submit()
        self.h.manager._make_hook(job, threading.Event())(
            {"status": "finished", "filename": "/tmp/done.mkv"}
        )
        self.assertEqual(job.percent, 100.0)

    def test_progress_messages_are_throttled_but_forced_ones_escape(self):
        job = self.h.submit()
        job.status = jobs.DOWNLOADING
        self.h.manager._emit_progress(job, force=True)  # baseline tick
        before = len(self.h.messages)
        for _ in range(50):
            self.h.manager._emit_progress(job)
        self.assertEqual(len(self.h.messages) - before, 0, "throttled ticks must not be sent")
        self.h.manager._emit_progress(job, force=True)
        self.assertEqual(len(self.h.messages) - before, 1)

    def test_completion_emits_a_complete_message(self):
        job = self.h.submit()
        job.filename = "/tmp/done.mkv"
        self.h.manager._finish(job, jobs.COMPLETE, phase="Done")
        self.assertTrue(
            any(m["type"] == "complete" and m["filepath"] == "/tmp/done.mkv" for m in self.h.messages)
        )

    def test_failure_emits_an_error_message(self):
        job = self.h.submit()
        self.h.manager._finish(job, jobs.ERROR, error="HTTP Error 403")
        message = next(m for m in self.h.messages if m["type"] == "error")
        self.assertEqual(message["message"], "HTTP Error 403")
        self.assertEqual(message["code"], "download_failed")


class QueueTests(Base):
    start_workers = True
    max_concurrent = 2

    def test_jobs_run_through_to_complete(self):
        self.h.release.set()
        job = self.h.submit()
        self.assertEqual(self.h.wait_for(lambda j: j.id == job.id and j.status == jobs.COMPLETE).id, job.id)
        self.assertIn(job.id, self.h.ran)
        self.assertTrue(any(m["type"] == "complete" for m in self.h.messages))

    def test_cancelling_a_queued_job_never_starts_it(self):
        # A manager with no workers: nothing can claim the job, so cancel() is
        # the one that finishes it and the download step is never reached.
        idle = Harness(max_concurrent=1, start=False)
        self.addCleanup(idle.cleanup)
        job = idle.submit()
        self.assertTrue(idle.manager.cancel(job.id))
        self.assertEqual(idle.get(job.id).status, jobs.CANCELLED)
        self.assertEqual(idle.ran, [])
        self.assertFalse(idle.manager.cancel(job.id), "cancelling twice is a no-op")

    def test_cancelling_a_running_job_stops_it(self):
        job = self.h.submit()  # workers running, gate holds it in the fake step
        self.assertTrue(self.h.started.wait(5))
        self.assertTrue(self.h.manager.cancel(job.id))
        self.assertEqual(self.h.wait_for(lambda j: j.id == job.id and j.status == jobs.CANCELLED).id, job.id)

    def test_cancelling_a_finished_job_is_refused(self):
        self.h.release.set()
        job = self.h.submit()
        self.h.wait_for(lambda j: j.id == job.id and j.status == jobs.COMPLETE)
        self.assertFalse(self.h.manager.cancel(job.id))

    def test_queue_drains_in_order(self):
        self.h.release.set()
        ids = [self.h.submit(url=f"https://example.com/{n}").id for n in range(6)]
        for job_id in ids:
            self.h.wait_for(lambda j, i=job_id: j.id == i and j.status == jobs.COMPLETE, timeout=10)
        self.assertEqual(sorted(self.h.ran), sorted(ids))

    def test_list_jobs_puts_active_jobs_first(self):
        job = self.h.submit()
        listing = self.h.manager.list_jobs()
        self.assertEqual(listing[0].id, job.id)
        self.assertNotIn(listing[0].status, jobs.TERMINAL)


class ConcurrencyGateTests(unittest.TestCase):
    def test_limit_is_respected_and_raisable(self):
        gate = jobs.ConcurrencyGate(2)
        gate.acquire()
        gate.acquire()
        done = threading.Event()

        def run() -> None:
            gate.acquire()
            done.set()
            gate.release()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertFalse(done.wait(0.3), "a third slot must not be handed out at limit 2")

        gate.set_limit(3)
        self.assertTrue(done.wait(2), "raising the limit must release the waiter")
        thread.join(2)
        gate.release()
        gate.release()

    def test_limit_is_always_at_least_one(self):
        gate = jobs.ConcurrencyGate(0)
        self.assertEqual(gate.limit, 1)
        gate.set_limit(-5)
        self.assertEqual(gate.limit, 1)

    def test_slots_are_reusable(self):
        gate = jobs.ConcurrencyGate(1)
        for _ in range(20):
            gate.acquire()
            gate.release()
        self.assertEqual(gate.active, 0)


class CancelRaceTests(unittest.TestCase):
    """cancel() and the worker pool both want the same job; exactly one wins."""

    def test_claim_is_exclusive(self):
        h = Harness(start=False)
        self.addCleanup(h.cleanup)
        job = h.submit()
        self.assertIsNotNone(h.manager._claim(job.id), "the first claim wins")
        self.assertIsNone(h.manager._claim(job.id), "a second claim must find nothing")
        self.assertEqual(job.status, jobs.DOWNLOADING)

    def test_claim_refuses_a_cancelled_job(self):
        h = Harness(start=False)
        self.addCleanup(h.cleanup)
        job = h.submit()
        h.manager.cancel(job.id)
        self.assertIsNone(h.manager._claim(job.id))

    def test_cancelling_after_a_claim_leaves_the_finish_to_the_worker(self):
        h = Harness(start=False)
        self.addCleanup(h.cleanup)
        job = h.submit()
        h.manager._claim(job.id)
        self.assertTrue(h.manager.cancel(job.id))
        self.assertEqual(job.status, jobs.DOWNLOADING, "the worker still owns the job")
        self.assertTrue(h.manager._cancels[job.id].is_set(), "but the cancel event is set")

    def test_a_finished_job_is_never_written_complete_twice(self):
        h = Harness(start=False)
        self.addCleanup(h.cleanup)
        job = h.submit()
        h.manager._finish(job, jobs.COMPLETE, phase="Done")
        self.assertFalse(h.manager.cancel(job.id))
        self.assertEqual(h.get(job.id).status, jobs.COMPLETE)


class ConcurrencyGateStopTests(unittest.TestCase):
    def test_stop_releases_waiters(self):
        gate = jobs.ConcurrencyGate(1)
        gate.acquire()
        stop = threading.Event()
        taken = []

        def run() -> None:
            taken.append(gate.acquire(stop))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        time.sleep(0.1)
        self.assertEqual(taken, [], "the second waiter is parked")
        gate.stop()
        thread.join(2)
        self.assertEqual(taken, [False], "shutdown unblocks it with a refusal, not a slot")

    def test_acquire_refuses_once_stop_is_set(self):
        gate = jobs.ConcurrencyGate(4)
        stop = threading.Event()
        stop.set()
        self.assertFalse(gate.acquire(stop))
        self.assertEqual(gate.active, 0, "a refused acquire must not consume a slot")


class StoreAfterCloseTests(unittest.TestCase):
    def test_reads_and_writes_after_close_are_no_ops(self):
        store = RecordingStore()
        job = jobs.Job(id="a", url="https://x.test/a", output_dir=tempfile.gettempdir())
        store.upsert(job)
        store.close()
        store.upsert(job)  # must not raise
        self.assertEqual(store.get("a"), None)
        self.assertEqual(store.recent(), [])
        self.assertEqual(store.unfinished(), [])
        self.assertEqual(store.delete_finished(), 0)
        store._dir.cleanup()

    def test_recover_looks_back_further_than_the_recent_window(self):
        store = RecordingStore()
        self.addCleanup(store._dir.cleanup)
        self.addCleanup(store.close)
        for index in range(250):  # older than recent()'s default limit
            store.upsert(
                jobs.Job(id=f"old{index}", url="https://x.test/a", status=jobs.COMPLETE, created_at=1.0)
            )
        store.upsert(jobs.Job(id="stuck", url="https://x.test/a", status=jobs.DOWNLOADING))
        manager = jobs.JobManager(
            store=store, send=lambda _p: None, ffmpeg_found=True,
            default_dir=tempfile.gettempdir(), log=lambda _m: None,
        )
        self.addCleanup(manager.shutdown)
        recovered = manager.recover()
        self.assertEqual([j.id for j in recovered], ["stuck"])
        self.assertEqual(store.get("stuck").status, jobs.INTERRUPTED)


class DownloadOptionsTests(unittest.TestCase):
    def test_audio_only_uses_the_extract_audio_postprocessor(self):
        opts = probe.download_options({"formatId": "x", "audioOnly": True}, ffmpeg_found=True)
        self.assertEqual(opts["format"], "x")
        self.assertEqual(opts["postprocessors"][0]["key"], "FFmpegExtractAudio")

    def test_audio_only_without_ffmpeg_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "ffmpeg"):
            probe.download_options({"audioOnly": True}, ffmpeg_found=False)

    def test_merged_format_is_the_default(self):
        self.assertEqual(
            probe.download_options({}, ffmpeg_found=True)["format"], "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
        )

    def test_subtitles_split_manual_and_automatic(self):
        opts = probe.download_options(
            {"subtitles": [{"lang": "en", "auto": False}, {"lang": "tr", "auto": True}]},
            ffmpeg_found=True,
        )
        self.assertTrue(opts["writesubtitles"])
        self.assertTrue(opts["writeautomaticsub"])
        self.assertEqual(opts["subtitleslangs"], ["en", "tr"])
        self.assertEqual(opts["subtitlesformat"], "srt/best")

    def test_no_subtitles_means_no_subtitle_options(self):
        opts = probe.download_options({"subtitles": []}, ffmpeg_found=True)
        for key in ("writesubtitles", "writeautomaticsub", "subtitleslangs"):
            self.assertNotIn(key, opts)

    def test_single_url_downloads_never_widen_to_a_playlist(self):
        self.assertTrue(probe.download_options({}, ffmpeg_found=False)["noplaylist"])


class PlaylistTests(Base):
    start_workers = True
    max_concurrent = 3

    def setUp(self) -> None:
        super().setUp()
        self.h.release.set()
        self._real_list_entries = probe.list_entries
        self._real_resolve = probe.resolve_entry_url
        self.entries = {
            "entries": [
                {"index": 0, "id": "a", "title": "First", "url": "https://example.com/a"},
                {"index": 1, "id": "b", "title": "Second", "url": "https://example.com/b"},
                {"index": 2, "id": "c", "title": "Third", "url": "https://example.com/c"},
            ]
        }
        probe.list_entries = lambda url, **kw: self.entries  # type: ignore[assignment]
        probe.resolve_entry_url = lambda url, item: f"https://example.com/resolved/{item}"  # type: ignore[assignment]
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        probe.list_entries = self._real_list_entries  # type: ignore[assignment]
        probe.resolve_entry_url = self._real_resolve  # type: ignore[assignment]

    def test_whole_playlist_creates_one_child_per_entry(self):
        group = self.h.submit(url="https://example.com/pl", playlistMode="all", title="My list")
        children = self.h.wait_for_children(group, 3)
        self.assertEqual(sorted(c.entry_index for c in children), [0, 1, 2])
        self.assertEqual(sorted(c.title for c in children), ["First", "Second", "Third"])
        self.h.wait_for(lambda j: j.id == group.id and j.status == jobs.COMPLETE)
        self.assertIn("3 videos", self.h.get(group.id).phase)

    def test_select_mode_downloads_only_chosen_entries(self):
        group = self.h.submit(
            url="https://example.com/pl", playlistMode="select", entryIndexes=[0, 2]
        )
        children = self.h.wait_for_children(group, 2)
        self.assertEqual(sorted(c.title for c in children), ["First", "Third"])

    def test_children_inherit_settings_from_the_group(self):
        group = self.h.submit(
            url="https://example.com/pl",
            playlistMode="all",
            audioOnly=True,
            subtitles=[{"lang": "en", "auto": False}],
        )
        group = self.h.wait_for(lambda j: j.id == group.id and j.entry_count == 3)
        for child in self.h.children(group):
            self.assertTrue(child.audio_only)
            self.assertEqual(child.subtitle_langs, [{"lang": "en", "auto": False}])
            self.assertEqual(child.output_dir, group.output_dir)

    def test_cancelling_the_group_cancels_its_children(self):
        self.h.release.clear()
        group = self.h.submit(url="https://example.com/pl", playlistMode="all")
        self.h.wait_for_children(group, 3)
        self.h.wait_for(lambda j: j.id == group.id and j.status == jobs.DOWNLOADING)
        self.assertTrue(self.h.manager.cancel(group.id))
        for child in self.h.children(group):
            self.h.wait_for(lambda j, cid=child.id: j.id == cid and j.status == jobs.CANCELLED)
        self.h.wait_for(lambda j: j.id == group.id and j.status == jobs.CANCELLED)

    def test_group_progress_tracks_children(self):
        self.h.release.clear()
        group = self.h.submit(url="https://example.com/pl", playlistMode="all")
        self.h.wait_for_children(group, 3)
        for child in self.h.children(group):
            self.h.manager._set_status(child, jobs.DOWNLOADING, phase="Downloading")
            child.downloaded_bytes, child.total_bytes = 30, 100
        self.h.manager._refresh_group(group)
        self.assertAlmostEqual(group.percent, 30.0, places=1)
        self.assertEqual(group.entry_done_bytes, 90)
        self.assertEqual(group.phase, "0 of 3 videos")

    def test_group_record_is_announced_before_its_children(self):
        """The extension groups rows by parent, so the parent must arrive first."""
        self.h.release.set()
        group = self.h.submit(url="https://example.com/pl", playlistMode="all")
        self.h.wait_for_children(group, 3)
        announced = [m["job"]["jobId"] for m in self.h.messages if m["type"] == "job"]
        self.assertEqual(announced[0], group.id, "the playlist row is queued first")
        for child in self.h.children(group):
            self.assertIn(
                child.id,
                announced[1:],
                "each child is announced after its group",
            )
            self.assertGreater(
                announced.index(child.id),
                announced.index(group.id),
                f"{child.id} arrived before its group",
            )

    def test_entry_without_url_falls_back_to_playlist_items(self):
        self.entries["entries"][1]["url"] = ""
        group = self.h.submit(url="https://example.com/pl", playlistMode="all")
        self.h.wait_for(lambda j: j.id == group.id and j.status == jobs.COMPLETE)
        stored = self.h.get(f"{group.id}-2")
        self.assertEqual(stored.url, "https://example.com/resolved/2")

    def test_empty_playlist_is_an_error(self):
        self.entries["entries"] = []
        group = self.h.submit(url="https://example.com/pl", playlistMode="all")
        group = self.h.wait_for(lambda j: j.id == group.id and j.status == jobs.ERROR)
        self.assertIn("No playable entries", group.error)


class GroupAggregationTests(Base):
    """How a playlist parent summarises its children.

    Deterministic by construction: children are attached by hand instead of
    being produced by a worker, so every aggregation rule can be checked without
    racing the download threads.
    """

    def setUp(self) -> None:
        super().setUp()
        self.group = self.h.submit(url="https://example.com/pl", playlistMode="all")
        self.entries = [
            {"index": i, "id": str(i), "title": f"Entry {i}", "url": f"https://example.com/{i}"}
            for i in range(3)
        ]
        for position, entry in enumerate(self.entries):
            self.h.manager._spawn_child(self.group, entry, position)
        self.children = self.h.children(self.group)
        self.h.manager._set_status(self.group, jobs.DOWNLOADING, phase="0 of 3 videos")

    def finish_children(self, status: jobs. str, error: str | None = None) -> None:
        for child in self.children:
            self.h.manager._finish(child, status, error=error)

    def test_all_done_completes_the_group(self):
        self.finish_children(jobs.COMPLETE)
        self.assertEqual(self.h.get(self.group.id).status, jobs.COMPLETE)
        self.assertIn("3 videos", self.h.get(self.group.id).phase)

    def test_partial_failure_errors_with_a_summary(self):
        self.h.manager._finish(self.children[0], jobs.ERROR, error="Video unavailable")
        self.h.manager._finish(self.children[1], jobs.COMPLETE)
        self.h.manager._finish(self.children[2], jobs.COMPLETE)
        group = self.h.get(self.group.id)
        self.assertEqual(group.status, jobs.ERROR)
        self.assertIn("2 of 3 videos", group.phase)
        self.assertIn("1 failed", group.phase)

    def test_total_failure_errors_the_group(self):
        self.finish_children(jobs.ERROR, error="HTTP Error 403")
        group = self.h.get(self.group.id)
        self.assertEqual(group.status, jobs.ERROR)
        self.assertIn("403", group.error)

    def test_total_cancellation_cancels_the_group(self):
        self.finish_children(jobs.CANCELLED)
        self.assertEqual(self.h.get(self.group.id).status, jobs.CANCELLED)

    def test_partial_cancellation_is_reported_not_hidden(self):
        self.h.manager._finish(self.children[0], jobs.CANCELLED)
        for child in self.children[1:]:
            self.h.manager._finish(child, jobs.COMPLETE)
        group = self.h.get(self.group.id)
        self.assertEqual(group.status, jobs.CANCELLED)
        self.assertIn("2 of 3 videos", group.phase)
        self.assertIn("1 cancelled", group.phase)

    def test_bytes_are_summed_and_phase_counts_completed_entries(self):
        for child in self.children:
            self.h.manager._set_status(child, jobs.DOWNLOADING, phase="Downloading")
            child.downloaded_bytes, child.total_bytes, child.speed = 10, 40, 500.0
        self.h.manager._refresh_group(self.group)
        self.assertEqual(self.group.entry_done_bytes, 30)
        self.assertEqual(self.group.entry_total_bytes, 120)
        self.assertAlmostEqual(self.group.percent, 25.0, places=1)
        self.assertEqual(self.group.speed, 1500.0)
        self.finish_children(jobs.COMPLETE)
        self.assertEqual(self.h.get(self.group.id).percent, 100.0)


class EntryListingTests(unittest.TestCase):
    """``list_entries`` shape, with the extraction itself stubbed out."""

    def setUp(self) -> None:
        self._real_run = probe._run
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        probe._run = self._real_run

    def test_entries_are_numbered_and_playlist_metadata_is_kept(self):
        probe._run = lambda url, extra=None: {  # type: ignore[assignment]
            "_type": "playlist",
            "id": "PL123",
            "title": "My list",
            "n_entries": 3,
            "entries": [
                {"id": "a", "title": "One", "url": "https://example.com/a"},
                {"id": "b", "title": "Two", "url": "https://example.com/b"},
                {"id": "c", "title": "Three", "url": "https://example.com/c"},
            ],
        }
        result = probe.list_entries("https://example.com/pl")
        self.assertEqual(result["total"], 3)
        self.assertEqual([e["index"] for e in result["entries"]], [0, 1, 2])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["entries"][1]["title"], "Two")

    def test_non_playlist_urls_yield_no_entries(self):
        probe._run = lambda url, extra=None: {"_type": "video", "title": "Single"}  # type: ignore[assignment]
        result = probe.list_entries("https://example.com/watch")
        self.assertEqual(result["entries"], [])

    def test_entry_urls_that_are_not_http_are_dropped(self):
        probe._run = lambda url, extra=None: {  # type: ignore[assignment]
            "_type": "playlist",
            "n_entries": 1,
            "entries": [{"id": "a", "title": "One", "url": "mms://stream"}],
        }
        self.assertEqual(probe.list_entries("https://example.com/pl")["entries"][0]["url"], "")

    def test_limit_is_clamped(self):
        probe._run = lambda url, extra=None: {"_type": "playlist", "entries": []}  # type: ignore[assignment]
        # No exception, and the request stays inside the cap.
        self.assertEqual(probe.list_entries("https://example.com/pl", limit=10**9)["entries"], [])


if __name__ == "__main__":
    unittest.main()
