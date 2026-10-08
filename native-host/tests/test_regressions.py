"""Regressions for failures at the browser/queue/persistence boundaries."""
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

from ytdlp_host import jobs, paths, probe, protocol
from ytdlp_host.cache import ProbeCache
from ytdlp_host.main import Host
from tests.test_jobs import Harness


class QueueRegressions(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.cleanup)

    def group(self, count=3):
        group = self.h.submit(playlistMode="all")
        group.status = jobs.DOWNLOADING
        group.entry_count = count
        for index in range(count):
            self.h.manager._spawn_child(group, {"index": index, "url": f"https://x/{index}"}, index)
        return group, self.h.children(group)

    def test_cancelling_one_entry_does_not_cancel_siblings(self):
        group, children = self.group()
        self.h.manager.cancel(children[0].id)
        self.assertFalse(self.h.manager._cancels[group.id].is_set())
        self.assertTrue(all(not self.h.manager._cancels[c.id].is_set() for c in children[1:]))
        self.assertEqual(children[0].status, jobs.CANCELLED)

    def test_group_cancel_reaches_queued_and_running_children(self):
        group, children = self.group()
        self.h.manager._claim(children[1].id)
        self.h.manager.cancel(group.id)
        self.assertTrue(all(self.h.manager._cancels[c.id].is_set() for c in children))
        self.assertEqual(children[0].status, jobs.CANCELLED)

    def test_clear_finished_preserves_active_group_accounting(self):
        group, children = self.group()
        self.h.manager._finish(children[0], jobs.COMPLETE)
        self.h.manager.clear_finished()
        self.assertIsNone(self.h.store.get(children[0].id))
        self.assertIsNotNone(self.h.get(children[0].id))
        for child in children[1:]:
            self.h.manager._finish(child, jobs.COMPLETE)
        self.assertEqual(group.status, jobs.COMPLETE)
        self.assertEqual(self.h.manager.clear_finished(), 4)

    def test_duplicate_id_cannot_overwrite_an_existing_download(self):
        self.h.submit(jobId="same")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.h.submit(jobId="same")

    def test_history_limit_never_hides_active_jobs(self):
        for _ in range(15): self.h.submit()
        self.assertEqual(len(self.h.manager.list_jobs(limit=2)), 15)

    def test_cancelled_waiter_never_calls_ytdlp(self):
        job = self.h.submit()
        self.h.manager._claim(job.id)
        self.h.manager.cancel(job.id)
        with patch.object(jobs.yt_dlp, "YoutubeDL") as ydl:
            jobs.JobManager._run_download(self.h.manager, job)
            ydl.assert_not_called()
        self.assertEqual(job.status, jobs.CANCELLED)

    def test_all_playlist_expands_more_than_first_page(self):
        group = self.h.submit(playlistMode="all")
        with patch.object(probe, "list_entries", side_effect=[
            {"entries": [{"index": 0, "url": "https://x/0"}], "nextOffset": 500},
            {"entries": [{"index": 500, "url": "https://x/500"}], "nextOffset": None},
        ]):
            self.h.manager._plan_playlist(group)
        self.assertEqual({c.entry_index for c in self.h.children(group)}, {0, 500})

    def test_retry_preserves_custom_template_and_selection(self):
        job = self.h.submit(playlistMode="select", entryIndexes=[1, 7], outputTemplate="%(id)s.%(ext)s")
        self.h.manager.cancel(job.id)
        retried = self.h.manager.retry(job.id)
        self.assertNotEqual(retried.id, job.id)
        self.assertEqual(retried.entries, [{"index": 1}, {"index": 7}])
        self.assertEqual(retried.output_template, job.output_template)


class MetadataRegressions(unittest.TestCase):
    def test_direct_media_only_costs_one_extraction(self):
        with patch.object(probe, "extract", return_value={"url": "https://x/video.mp4", "ext": "mp4"}) as extract:
            result = probe.probe("https://x/video.mp4")
            self.assertTrue(result["formats"])
            self.assertEqual(extract.call_count, 1)

    def test_page_offsets_and_deleted_entries_preserve_source_indexes(self):
        with patch.object(probe, "extract", return_value={"_type": "playlist", "playlist_count": 503,
                    "entries": [None, {"id": "x", "url": "https://x/x"}, None]}) as extract:
            result = probe.list_entries("https://x/pl", offset=500)
            self.assertEqual(result["entries"][0]["index"], 501)
            self.assertIsNone(result["nextOffset"])
            self.assertEqual(extract.call_args.args[1]["playliststart"], 501)

    def test_no_ffmpeg_uses_a_combined_format(self):
        self.assertEqual(probe.download_options({}, False)["format"], "b[ext=mp4]")
        with self.assertRaisesRegex(ValueError, "ffmpeg"):
            probe.download_options({"formatId": "137+140"}, False)

    def test_mp4_default_merges_and_remuxes_with_real_yt_dlp_postprocessor(self):
        options = probe.download_options({}, True)
        self.assertIn("ext=mp4", options["format"])
        self.assertEqual(options["merge_output_format"], "mp4")
        with probe.yt_dlp.YoutubeDL(options) as ydl:
            self.assertTrue(any(type(pp).__name__ == "FFmpegVideoRemuxerPP" for pp in ydl._pps["post_process"]))

    def test_explicit_format_and_mp3_do_not_get_mp4_remuxing(self):
        options = probe.download_options({"formatId": "248+251"}, True)
        self.assertEqual(options["format"], "248+251")
        self.assertNotIn("merge_output_format", options)
        audio = probe.download_options({"audioOnly": True}, True)
        self.assertEqual(audio["format"], "bestaudio/best")
        self.assertEqual(audio["postprocessors"][0]["key"], "FFmpegExtractAudio")

    def test_clear_during_lookup_cannot_restore_forgotten_metadata(self):
        cache = ProbeCache()
        started, release = threading.Event(), threading.Event()
        def lookup():
            started.set()
            release.wait(2)
            return {'title': 'Forgotten'}
        worker = threading.Thread(target=lambda: cache.get('url', lookup))
        worker.start()
        self.assertTrue(started.wait(2))
        cache.clear()
        release.set()
        worker.join(2)
        self.assertEqual(cache.get('url', lambda: {'title': 'Fresh'}), {'title': 'Fresh'})

    def test_cache_coalesces_and_copies_results(self):
        cache = ProbeCache()
        started, release = threading.Event(), threading.Event()
        calls, results = [], []
        def lookup():
            calls.append(1); started.set(); release.wait(2)
            return {"formats": ["a"]}
        threads = [threading.Thread(target=lambda: results.append(cache.get("url", lookup))) for _ in range(4)]
        for thread in threads: thread.start()
        started.wait(2); release.set()
        for thread in threads: thread.join(2)
        self.assertEqual(len(calls), 1)
        results[0]["formats"].clear()
        self.assertEqual(cache.get("url", lookup)["formats"], ["a"])
        cache.get("url", lookup, refresh=True)
        self.assertEqual(len(calls), 2)

    def test_all_path_separators_and_windows_drive_paths_are_rejected(self):
        for template in ("C:\\outside.%(ext)s", "C:outside.%(ext)s", "a/b.%(ext)s", "a\\b.%(ext)s"):
            self.assertEqual(paths.safe_output_template(template), paths.DEFAULT_TEMPLATE)

    def test_missing_body_is_a_protocol_error(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.read_message(io.BytesIO(struct.pack("=I", 10)))

    def test_postprocessed_path_takes_precedence(self):
        result = jobs._final_path(None, {"filepath": "/out/music.mp3", "_filename": "/out/music.webm"})
        self.assertEqual(result, "/out/music.mp3")

    def test_mixed_caption_sources_do_not_silently_prefer_manual(self):
        selected = [{"lang": "en", "auto": False}, {"lang": "tr", "auto": True}]
        with probe._ydl(probe.download_options({"subtitles": selected}, True)) as ydl:
            probe.select_subtitles(ydl, selected)
            tracks = ydl.process_subtitles("test",
                {"en": [{"ext": "srt", "url": "https://x/manual-en"}], "tr": [{"ext": "srt", "url": "https://x/manual-tr"}]},
                {"tr": [{"ext": "srt", "url": "https://x/auto-tr"}]})
            self.assertEqual(tracks["en"]["url"], "https://x/manual-en")
            self.assertEqual(tracks["tr"]["url"], "https://x/auto-tr")


class HostRegressions(unittest.TestCase):
    def test_settings_persist_and_invalid_patch_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"YTDLP_HOST_STATE_DIR": tmp}):
            messages = []
            with patch("ytdlp_host.main.ffmpeg_version", return_value=None):
                host = Host(send=messages.append)
                try:
                    host.handle({"type": "update_settings", "settings": {
                        "defaultDownloadDir": tmp, "maxConcurrentDownloads": 4, "notifyOnComplete": False}})
                    host.handle({"type": "update_settings", "settings": {
                        "maxConcurrentDownloads": 6, "defaultFormat": ["invalid"]}})
                    self.assertEqual(host.settings["maxConcurrentDownloads"], 4)
                    self.assertEqual(messages[-1]["type"], "error")
                finally: host.stop(); paths.close_log()
                second = Host(send=messages.append)
                try:
                    self.assertEqual(second.settings["maxConcurrentDownloads"], 4)
                    self.assertFalse(second.settings["notifyOnComplete"])
                finally: second.stop(); paths.close_log()

    def test_large_queue_is_framed_in_chunks(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"YTDLP_HOST_STATE_DIR": tmp}):
            messages = []
            with patch("ytdlp_host.main.ffmpeg_version", return_value=None):
                host = Host(send=messages.append)
                try:
                    with patch.object(host._manager, "list_jobs", return_value=[jobs.Job(id=str(i), url="https://x") for i in range(130)]):
                        host.handle({"type": "list_jobs", "requestId": "test"})
                    self.assertEqual([len(m["jobs"]) for m in messages], [25, 25, 25, 25, 25, 5])
                    self.assertEqual([m["final"] for m in messages], [False] * 5 + [True])
                    for message in messages: protocol.encode_message(message)
                finally: host.stop(); paths.close_log()
