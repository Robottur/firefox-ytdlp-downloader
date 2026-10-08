"""Probe result shaping: format labels, subtitle merging, playlist payloads.

The extractor itself is stubbed out; what is under test is the translation into
the exact structures the popup renders.
"""

from __future__ import annotations

import unittest

from ytdlp_host import probe


class UrlValidationTests(unittest.TestCase):
    def test_accepts_and_trims_http_urls(self):
        self.assertEqual(probe.validate_url("  https://x.test/a  "), "https://x.test/a")

    def test_rejects_everything_else(self):
        for bad in (
            "file:///etc/passwd",
            "ftp://x.test/a.mp4",
            "mms://x.test/s",
            "javascript:alert(1)",
            "data:text/html,<b>",
            "//x.test/a",
            "",
            "   ",
            None,
            42,
        ):
            with self.assertRaises(probe.ProbeError, msg=bad):
                probe.validate_url(bad)  # type: ignore[arg-type]

    def test_rejects_absurdly_long_urls(self):
        with self.assertRaises(probe.ProbeError):
            probe.validate_url("https://x.test/" + "a" * 5000)


class FormatTests(unittest.TestCase):
    def test_video_with_audio(self):
        entry = probe._format_entry(
            {
                "format_id": "137",
                "ext": "mp4",
                "height": 1080,
                "fps": 60,
                "vcodec": "avc1.640028",
                "acodec": "none",
                "filesize": 100 * 1024 * 1024,
            }
        )
        assert entry
        self.assertEqual(entry["kind"], "video only")
        self.assertTrue(entry["isVideo"])
        self.assertFalse(entry["isAudio"])
        self.assertIn("1080p60", entry["label"])
        self.assertIn("100.0 MB", entry["label"])

    def test_audio_only(self):
        entry = probe._format_entry(
            {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a", "abr": 128}
        )
        assert entry
        self.assertTrue(entry["isAudio"])
        self.assertFalse(entry["isVideo"])
        self.assertIn("128kbps", entry["label"])

    def test_direct_media_without_codecs_is_kept(self):
        entry = probe._format_entry(
            {"format_id": "http-160", "ext": "mp4", "filesize": 23875, "url": "http://x/a.mp4"}
        )
        assert entry
        self.assertTrue(entry["isVideo"])
        self.assertIn("Direct media", entry["label"])
        self.assertIn("23.3 KB", entry["label"])

    def test_storyboards_and_drm_are_dropped(self):
        self.assertIsNone(probe._format_entry({"format_id": "sb0", "vcodec": "none", "acodec": "none"}))
        self.assertIsNone(
            probe._format_entry({"format_id": "drm", "vcodec": "h264", "acodec": "aac", "has_drm": True})
        )

    def test_language_tagged_formats_are_distinguishable(self):
        entry = probe._format_entry(
            {"format_id": "140-es", "ext": "m4a", "vcodec": "none", "acodec": "mp4a", "language": "es"}
        )
        assert entry
        self.assertIn("[es]", entry["label"])

    def test_approximate_size_is_used_when_exact_is_missing(self):
        entry = probe._format_entry(
            {"format_id": "1", "ext": "mp4", "vcodec": "h264", "acodec": "aac",
             "filesize_approx": 2 * 1024 * 1024 * 1024}
        )
        assert entry
        self.assertEqual(entry["filesize"], 2 * 1024 * 1024 * 1024)
        self.assertIn("2.0 GB", entry["label"])

    def test_formats_are_sorted_by_quality_and_deduplicated(self):
        info = {
            "formats": [
                {"format_id": "18", "ext": "mp4", "height": 360, "vcodec": "h264", "acodec": "aac"},
                {"format_id": "137", "ext": "mp4", "height": 1080, "vcodec": "h264", "acodec": "none"},
                {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a"},
                {"format_id": "18", "ext": "mp4", "height": 360, "vcodec": "h264", "acodec": "aac"},
            ]
        }
        formats = probe._formats(info)
        self.assertEqual([f["formatId"] for f in formats], ["137", "18", "140"])

    def test_format_list_is_capped(self):
        info = {
            "formats": [
                {"format_id": str(i), "ext": "mp4", "height": i, "vcodec": "h264", "acodec": "aac"}
                for i in range(1, 400)
            ]
        }
        self.assertEqual(len(probe._formats(info)), probe.MAX_FORMATS)

    def test_single_resolved_format_becomes_one_entry(self):
        formats = probe._formats({"url": "http://x/a.mp4", "ext": "mp4"})
        self.assertEqual(len(formats), 1)
        self.assertIn("Direct media", formats[0]["label"])


class SubtitleTests(unittest.TestCase):
    def test_manual_and_automatic_are_merged_by_language(self):
        info = {
            "subtitles": {"en": [{"ext": "vtt"}, {"ext": "srt"}]},
            "automatic_captions": {"en": [{"ext": "vtt"}], "fr": [{"ext": "vtt"}]},
        }
        subs = probe._subtitles(info)
        by_base = {s["base"]: s for s in subs}
        self.assertTrue(by_base["en"]["manual"], "en has real subtitles")
        self.assertTrue(by_base["en"]["auto"], "en also has auto captions")
        self.assertFalse(by_base["fr"]["manual"])
        self.assertTrue(by_base["fr"]["auto"])
        self.assertEqual(by_base["en"]["exts"], ["srt", "vtt"])
        self.assertEqual(by_base["en"]["name"], "English")
        self.assertEqual(by_base["en"]["lang"], "en")

    def test_asr_variants_collapse_onto_one_readable_entry(self):
        info = {
            "automatic_captions": {
                "en": [{"ext": "vtt"}],
                "en-eEY6OEpapPo": [{"ext": "vtt"}],
                "en-nP7-2PuUl7o": [{"ext": "vtt"}],
                "fr-nP7-2PuUl7o": [{"ext": "vtt"}],
            }
        }
        subs = probe._subtitles(info)
        self.assertEqual([s["base"] for s in subs], ["en", "fr"])
        english, french = subs
        self.assertEqual(english["name"], "English")
        self.assertEqual(english["lang"], "en", "the plain code is preferred")
        self.assertEqual(len(english["variants"]), 3)
        self.assertEqual(french["name"], "French")
        self.assertEqual(french["lang"], "fr-nP7-2PuUl7o", "no plain code exists")

    def test_regional_codes_keep_their_suffix(self):
        subs = probe._subtitles({"subtitles": {"en-GB": [{"ext": "vtt"}]}})
        self.assertEqual(subs[0]["name"], "English (en-GB)")
        self.assertEqual(subs[0]["base"], "en-GB")

    def test_manual_languages_sort_first(self):
        info = {
            "subtitles": {"fr": [{"ext": "srt"}]},
            "automatic_captions": {"zz": [{"ext": "vtt"}]},
        }
        self.assertEqual([s["base"] for s in probe._subtitles(info)], ["fr", "zz"])

    def test_no_subtitles_yields_an_empty_list(self):
        self.assertEqual(probe._subtitles({}), [])


class DurationTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(probe._duration_string(75), "1:15")
        self.assertEqual(probe._duration_string(3725), "1:02:05")
        self.assertIsNone(probe._duration_string(None))
        self.assertIsNone(probe._duration_string(0))


class ProbeShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._real_run = probe.extract
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        probe.extract = self._real_run

    def test_playlist_payload_keeps_count_and_uploader(self):
        probe.extract = lambda url, extra=None: {  # type: ignore[assignment]
            "_type": "playlist",
            "id": "PL1",
            "title": "My list",
            "uploader": "Someone",
            "playlist_count": 183,
            "entries": [{"id": "a", "title": "One", "url": "https://x/1"}],
        }
        result = probe.probe("https://x/pl")
        self.assertTrue(result["isPlaylist"])
        self.assertEqual(result["playlistCount"], 183)
        self.assertTrue(result["playlistCountIsExact"])
        self.assertEqual(result["playlistUploader"], "Someone")
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["index"], 0)

    def test_playlist_count_uses_the_extractor_total_not_the_resolved_sample(self):
        """``playlistend`` caps what we resolve, not how many videos exist."""
        probe.extract = lambda url, extra=None: {  # type: ignore[assignment]
            "_type": "playlist",
            "id": "PL1",
            "title": "Long list",
            "playlist_count": 500,
            "n_entries": 3,  # what this run happened to resolve
            "entries": [{"id": str(i), "url": f"https://x/{i}"} for i in range(3)],
        }
        self.assertEqual(probe.probe("https://x/pl")["playlistCount"], 500)

    def test_playlist_without_a_reported_count_is_marked_approximate(self):
        probe.extract = lambda url, extra=None: {  # type: ignore[assignment]
            "_type": "playlist",
            "title": "Unknown size",
            "entries": [{"id": "a", "url": "https://x/1"}, {"id": "b", "url": "https://x/2"}],
        }
        result = probe.probe("https://x/pl")
        self.assertEqual(result["playlistCount"], 2)
        self.assertFalse(result["playlistCountIsExact"], "2 is only what we resolved")

    def test_single_video_reports_formats_and_subtitles(self):
        calls = []

        def fake(url, extra=None):
            calls.append(extra)
            return {
                "_type": "video",
                "id": "abc",
                "title": "A video",
                "duration": 90,
                "formats": [{"format_id": "18", "ext": "mp4", "height": 360,
                             "vcodec": "h264", "acodec": "aac"}],
                "subtitles": {"en": [{"ext": "srt"}]},
                "thumbnails": [{"url": "http://x/small.jpg", "preference": 1},
                               {"url": "http://x/big.jpg", "preference": 10}],
            }

        probe.extract = fake  # type: ignore[assignment]
        result = probe.probe("https://x/watch")
        self.assertFalse(result["isPlaylist"])
        self.assertEqual(result["durationString"], "1:30")
        self.assertEqual(result["thumbnail"], "http://x/big.jpg")
        self.assertEqual(len(result["formats"]), 1)
        self.assertEqual(result["subtitles"][0]["lang"], "en")
        self.assertEqual(len(calls), 1, "a single video must only cost one extraction")

    def test_flat_video_result_triggers_a_second_extraction(self):
        responses = [
            {"_type": "video", "id": "abc", "title": "V", "url": "https://x/stream.mp4"},
            {"_type": "video", "id": "abc", "title": "V",
             "formats": [{"format_id": "18", "ext": "mp4", "vcodec": "h264", "acodec": "aac"}]},
        ]
        probe.extract = lambda url, extra=None: responses.pop(0)  # type: ignore[assignment]
        result = probe.probe("https://x/watch")
        self.assertEqual(len(result["formats"]), 1)

    def test_extractor_surprise_playlist_is_handled(self):
        probe.extract = lambda url, extra=None: {  # type: ignore[assignment]
            "_type": "playlist", "title": "Late", "n_entries": 2, "entries": []
        }
        self.assertTrue(probe.probe("https://x/weird")["isPlaylist"])

    def test_extraction_errors_become_probe_errors(self):
        from yt_dlp.utils import DownloadError

        # Patch the raw yt-dlp call so the conversion in probe.extract runs.
        self._real_run = probe._run

        def boom(url, extra=None):
            raise DownloadError("Unsupported URL")

        probe._run = boom  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(probe, "_run", self._real_run))
        with self.assertRaises(probe.ProbeError) as ctx:
            probe.probe("https://x/nope")
        self.assertIn("Unsupported URL", str(ctx.exception))


class DeadEntryTests(unittest.TestCase):
    """A playlist entry that yt-dlp could not resolve is normal, not fatal."""

    def setUp(self) -> None:
        self._real = probe.extract
        self.addCleanup(lambda: setattr(probe, "extract", self._real))

    def test_none_entries_are_skipped_in_the_probe_payload(self):
        probe.extract = lambda url, extra=None: {  # type: ignore[assignment]
            "_type": "playlist",
            "title": "Mostly dead",
            "playlist_count": 3,
            "entries": [None, {"id": "b", "title": "Alive", "url": "https://x/b"}, None],
        }
        result = probe.probe("https://x/pl")
        self.assertEqual([e["id"] for e in result["entries"]], ["b"])

    def test_none_entries_are_skipped_in_the_checklist(self):
        probe.extract = lambda url, extra=None: {  # type: ignore[assignment]
            "_type": "playlist",
            "playlist_count": 3,
            "entries": [{"id": "a", "url": "https://x/a"}, None, {"id": "c", "url": "https://x/c"}],
        }
        result = probe.list_entries("https://x/pl")
        self.assertEqual([e["id"] for e in result["entries"]], ["a", "c"])
        self.assertEqual(result["total"], 3)
        self.assertFalse(result["truncated"])
        self.assertEqual([e["index"] for e in result["entries"]], [0, 2])


class NonNumericFieldTests(unittest.TestCase):
    """Extractors do report "variable" where a number is expected."""

    def test_format_survives_junk_numeric_fields(self):
        entry = probe._format_entry(
            {
                "format_id": "x",
                "ext": "mp4",
                "height": "variable",
                "fps": "variable",
                "vcodec": "h264",
                "acodec": "aac",
                "tbr": "high",
                "filesize": "unknown",
            }
        )
        assert entry
        self.assertEqual(entry["height"], 0)
        self.assertEqual(entry["fps"], 0.0)
        self.assertEqual(entry["tbr"], 0.0)
        self.assertIsNone(entry["filesize"])
        self.assertNotIn("None", entry["label"])

    def test_float_height_is_rendered_without_a_decimal_point(self):
        entry = probe._format_entry(
            {"format_id": "y", "ext": "mp4", "height": 1080.0, "vcodec": "h264", "acodec": "none"}
        )
        assert entry
        self.assertIn("1080p", entry["label"])


class DownloadTemplateTests(unittest.TestCase):
    def test_absolute_templates_are_refused(self):
        """An absolute template would discard the chosen download folder."""
        for bad in ("/etc/cron.d/x.%(ext)s", "~/.config/evil.%(ext)s", "~/x.%(ext)s"):
            template = probe.paths.safe_output_template(bad)
            self.assertNotIn(bad, template, bad)
            self.assertIn("%(ext)s", template)

    def test_traversal_is_refused(self):
        self.assertNotIn("..", probe.paths.safe_output_template("../escape.%(ext)s"))

    def test_colons_are_allowed(self):
        """"uploader: title" is a standard yt-dlp idiom."""
        template = probe.paths.safe_output_template("%(uploader)s - %(title)s.%(ext)s")
        self.assertEqual(template, "%(uploader)s - %(title)s.%(ext)s")

    def test_ordinary_templates_pass_through(self):
        self.assertEqual(
            probe.paths.safe_output_template("%(title)s.%(ext)s"), "%(title)s.%(ext)s"
        )


if __name__ == "__main__":
    unittest.main()


class PublicSessionTests(unittest.TestCase):
    def test_public_options_disable_ambient_credentials_and_proxy(self):
        opts = probe.base_options()
        self.assertIs(opts["usenetrc"], False)
        self.assertIsNone(opts["cookiefile"])
        self.assertIsNone(opts["cookiesfrombrowser"])
        self.assertEqual(opts["proxy"], "")

    def test_public_extractors_do_not_share_cookie_jars(self):
        from http.cookiejar import Cookie
        with probe._ydl() as first, probe._ydl() as second:
            first.cookiejar.set_cookie(Cookie(0, "session", "secret", None, False,
                "example.test", False, False, "/", True, True, None, True, None, None, {}))
            self.assertEqual(len(first.cookiejar), 1)
            self.assertEqual(len(second.cookiejar), 0)
