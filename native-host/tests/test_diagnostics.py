import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ytdlp_host import paths
from ytdlp_host.diagnostics import Diagnostics, safe_error


class DiagnosticsTests(unittest.TestCase):
    def test_membership_failure_is_actionable_and_logger_preserves_safe_reason(self):
        from ytdlp_host import probe, jobs
        message = 'ERROR: [youtube] PRIVATE_ID: Join this channel to get access to members-only content. https://secret.test/?token=SECRET'
        self.assertIn('membership', safe_error(RuntimeError(message)).lower())
        self.assertNotIn('PRIVATE_ID', safe_error(RuntimeError(message)))
        with tempfile.TemporaryDirectory() as tmp, patch.object(paths, 'state_dir', return_value=Path(tmp)):
            for logger in (probe._FileLogger(), jobs._FileLogger(paths.write_log)):
                logger.debug('[youtube] PRIVATE_ID: Downloading webpage')
                logger.info('A normal progress message')
                logger.error(message)
            text = paths.diagnostics().preview()['text']
            self.assertEqual(text.count('membership_required'), 2)
            self.assertNotIn('operation_failed', text)
            self.assertNotIn('SECRET', text)
            self.assertNotIn('PRIVATE_ID', text)

    def test_expired_login_and_bot_challenge_have_distinct_safe_guidance(self):
        self.assertIn('expired', safe_error(RuntimeError('The provided YouTube account cookies are no longer valid.')).lower())
        self.assertIn('verification', safe_error(RuntimeError('Sign in to confirm you’re not a bot')).lower())
        self.assertIn('yt-dlp-ejs', safe_error(RuntimeError('ERROR: [youtube] X: The page needs to be reloaded.')))

    def test_arbitrary_messages_and_versions_never_reach_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            diagnostics = Diagnostics(Path(tmp))
            diagnostics.record('extractor failed https://user:SECRET@example.test/private /tmp/TITLE Cookie: TOKEN')
            diagnostics.record('download_failed', {'python': '3.12.1', 'yt-dlp': 'SECRET', 'TOKEN': '1.0'})
            text = diagnostics.preview()['text']
            for secret in ('SECRET', 'TOKEN', 'TITLE', 'example.test', '/tmp'):
                self.assertNotIn(secret, text)
            self.assertIn('download_failed', text)
            self.assertIn('3.12.1', text)

    def test_capture_expires_and_is_bounded_and_does_not_resume(self):
        with tempfile.TemporaryDirectory() as tmp, patch('ytdlp_host.diagnostics.time.monotonic', return_value=100) as clock:
            diagnostics = Diagnostics(Path(tmp))
            diagnostics.start_capture()
            for _ in range(40000):
                diagnostics.record('download_failed')
            self.assertLessEqual((Path(tmp) / 'diagnostic-capture.jsonl').stat().st_size, 1024 * 1024)
            self.assertFalse(diagnostics.status()['captureActive'])
            diagnostics.start_capture()
            clock.return_value = 1000
            diagnostics.record('download_failed')
            self.assertFalse(diagnostics.status()['captureActive'])
            diagnostics.start_capture()
            self.assertFalse(Diagnostics(Path(tmp)).status()['captureActive'])

    def test_export_requires_preview_and_saves_exact_preview_then_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            diagnostics = Diagnostics(Path(tmp))
            destination = Path(tmp) / 'export.txt'
            with self.assertRaises(ValueError):
                diagnostics.save_preview('missing', destination)
            diagnostics.record('download_failed')
            preview = diagnostics.preview()
            diagnostics.record('operation_failed')
            diagnostics.save_preview(preview['previewId'], destination)
            self.assertEqual(destination.read_text(), preview['text'])
            diagnostics.delete()
            self.assertEqual(diagnostics.preview()['text'], '')
            self.assertTrue(destination.exists())

    def test_legacy_log_is_detected_but_only_deleted_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / 'host.log'
            legacy.write_text('SECRET')
            diagnostics = Diagnostics(Path(tmp))
            self.assertTrue(diagnostics.status()['legacyLogsPresent'])
            self.assertNotIn('SECRET', diagnostics.preview()['text'])
            diagnostics.delete()
            self.assertTrue(legacy.exists())
            diagnostics.delete_legacy()
            self.assertFalse(legacy.exists())

    def test_existing_logger_and_errors_have_no_sensitive_output(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(paths, 'state_dir', return_value=Path(tmp)):
            paths.write_log('Permission denied: /tmp/SECRET Cookie TOKEN')
            paths.close_log()
            for file in Path(tmp).iterdir():
                self.assertNotIn('SECRET', file.read_text())
                self.assertNotIn('TOKEN', file.read_text())
            self.assertIn('permission', safe_error(PermissionError('/tmp/SECRET')).lower())
            self.assertNotIn('SECRET', safe_error(RuntimeError('https://user:SECRET@example.test')))

    def test_preview_fits_native_message_after_json_escaping(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            diagnostics = Diagnostics(Path(tmp))
            row = '{"code":"download_failed"}\n'
            (Path(tmp) / 'diagnostic-capture.jsonl').write_text(row * 40000)
            preview = diagnostics.preview()
            self.assertLess(len(json.dumps(preview).encode()), 512 * 1024)
