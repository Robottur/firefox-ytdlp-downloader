"""Save As routing, literal filenames, cancellation and queue persistence."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ytdlp_host import jobs, paths, probe
from tests.test_jobs import Harness


class DialogTests(unittest.TestCase):
    def test_kde_save_dialog_has_filename_and_filter_and_private_pipes(self):
        with patch.object(paths, "IS_WINDOWS", False), patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}), \
                patch.object(paths.shutil, "which", side_effect=lambda name: "/usr/bin/" + name), \
                patch.object(paths.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "/tmp/My clip.mp4\n", "")) as run:
            self.assertEqual(paths._desktop_dialog("/tmp", "My clip.mp4", "mp4"), "/tmp/My clip.mp4")
            self.assertEqual(run.call_args.args[0][-3:], ["--getsavefilename", str(Path("/tmp") / "My clip.mp4"), "MP4 files (*.mp4)"])
            self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_gnome_uses_save_dialog_with_overwrite_confirmation(self):
        with patch.object(paths, "IS_WINDOWS", False), patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"}), \
                patch.object(paths.shutil, "which", side_effect=lambda name: "/usr/bin/" + name), \
                patch.object(paths.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")) as run:
            self.assertIsNone(paths._desktop_dialog("/tmp", "clip.mp4", "mp4"))
            self.assertIn("--confirm-overwrite", run.call_args.args[0])
            self.assertIn("--save", run.call_args.args[0])
            self.assertEqual(run.call_count, 1, "Cancel must not open a fallback dialog")

    def test_folder_picker_uses_desktop_directory_browser(self):
        with patch.object(paths, "IS_WINDOWS", False), patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}), \
                patch.object(paths.shutil, "which", return_value="/usr/bin/kdialog"), \
                patch.object(paths.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "/tmp\n", "")) as run:
            self.assertEqual(paths._desktop_dialog("/tmp"), "/tmp")
            self.assertIn("--getexistingdirectory", run.call_args.args[0])

    def test_save_as_returns_directory_filename_and_confirmed_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "renamed.mp4"
            target.write_bytes(b"existing")
            with patch.object(paths, "_desktop_dialog", return_value=str(target)):
                result = paths.pick_save_file(tmp, "suggestion", "mp4")
            self.assertEqual(result["filename"], "renamed.mp4")
            self.assertEqual(result["outputDir"], tmp)
            self.assertTrue(result["overwrite"])
            self.assertEqual(target.read_bytes(), b"existing", "Choosing a name must not modify the file")

    def test_cancelled_save_as_returns_no_changes(self):
        with patch.object(paths, "_desktop_dialog", return_value=None):
            self.assertIsNone(paths.pick_save_file(None, "clip", "mp4"))

    def test_extension_normalization_never_authorizes_a_different_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "clip.mp4").write_bytes(b"must keep")
            with patch.object(paths, "_desktop_dialog", return_value=str(Path(tmp) / "clip.MP4")), self.assertRaisesRegex(ValueError, "confirm replacement"):
                paths.pick_save_file(tmp, "clip", "mp4")

    def test_unknown_final_container_cannot_authorize_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "clip.mp4"
            target.write_bytes(b"must keep")
            with patch.object(paths, "_desktop_dialog", return_value=str(target)), self.assertRaisesRegex(ValueError, "new filename"):
                paths.pick_save_file(tmp, "clip", "")

    def test_literal_filename_does_not_expand_yt_dlp_fields(self):
        template = paths.filename_template("100% - %(id)s..mp4")
        with probe.yt_dlp.YoutubeDL({"outtmpl": template}) as ydl:
            self.assertEqual(ydl.prepare_filename({"id": "INJECTED", "title": "other", "ext": "mp4"}), "100% - %(id)s..mp4")
        self.assertEqual(paths.safe_output_template(template), template)

    def test_filename_cannot_escape_folder_or_use_reserved_names(self):
        for name in ("../clip.mp4", "C:clip.mp4", "a\\clip.mp4", "CON.mp4", ".mp4", "~clip.mp4", "x\x00.mp4"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                paths.filename_template(name)

    def test_suggested_title_is_portable_and_has_extension(self):
        self.assertEqual(paths.suggest_filename("A/B: clip?", "mp4"), "A_B_ clip_.mp4")
        self.assertEqual(paths.suggest_filename("clip.mp4", "mp4"), "clip.mp4")


class FilenameQueueTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.addCleanup(self.h.cleanup)

    def test_named_output_survives_retry_but_overwrite_needs_new_confirmation(self):
        job = self.h.submit(outputFilename="My 100% clip.mp4", overwrite=True)
        loaded = self.h.store.get(job.id)
        self.assertTrue(loaded.overwrite)
        self.assertEqual(loaded.output_template, "My 100%% clip.%(ext)s")
        job.status = jobs.ERROR
        retried = self.h.manager.retry(job.id)
        self.assertFalse(retried.overwrite)
        self.assertEqual(retried.output_template, job.output_template)

    def test_playlist_refuses_shared_filename(self):
        with self.assertRaisesRegex(ValueError, "playlist"):
            self.h.submit(playlistMode="all", outputFilename="all.mp4")

    def test_existing_file_needs_explicit_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "existing.mp4").write_bytes(b"old")
            with self.assertRaisesRegex(ValueError, "Save As"):
                self.h.submit(outputDir=tmp, outputFilename="existing.mp4")

    def test_two_queued_jobs_cannot_share_explicit_filename(self):
        self.h.submit(outputFilename="same.mp4")
        with self.assertRaisesRegex(ValueError, "already using"):
            self.h.submit(outputFilename="same.mp4")

    def test_old_history_schema_is_migrated_without_losing_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.db"
            store = jobs.JobStore(db)
            store.upsert(jobs.Job(id="old", url="https://example.test/video"))
            store._conn.execute("ALTER TABLE jobs DROP COLUMN overwrite")
            store._conn.commit()
            store.close()
            restored = jobs.JobStore(db)
            try:
                self.assertFalse(restored.get("old").overwrite)
            finally:
                restored.close()
