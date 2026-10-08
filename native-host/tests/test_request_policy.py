import tempfile
from io import BytesIO
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

from ytdlp_host.request_policy import PolicyYoutubeDL, TrackerBlocked, check_url
from ytdlp_host import probe


class RequestPolicyTests(unittest.TestCase):
    def test_empty_hls_preflight_cannot_trigger_a_second_unchecked_fetch(self):
        from yt_dlp.networking import Response
        from yt_dlp.downloader.external import FFmpegFD
        first = Response(BytesIO(b''), 'https://example.test/a.m3u8', {})
        second = Response(BytesIO(b'#EXTM3U\n#EXT-X-KEY:METHOD=FOO\n'), 'https://example.test/a.m3u8', {})
        with PolicyYoutubeDL({'quiet': True}) as ydl, tempfile.TemporaryDirectory() as tmp:
            with patch.object(ydl, 'urlopen', side_effect=[first, second]), patch.object(
                    FFmpegFD, 'real_download', side_effect=AssertionError('Unfiltered child tool')):
                with self.assertRaises(TrackerBlocked):
                    ydl.dl(str(Path(tmp) / 'file.mp4'), {'url': 'https://example.test/a.m3u8',
                                                       'protocol': 'm3u8_native', 'ext': 'mp4'})

    def test_format_check_never_uses_external_downloader(self):
        from yt_dlp.downloader.external import FFmpegFD
        manifest = '#EXTM3U\n#EXTINF:1,\nhttps://doubleclick.net/a.ts\n#EXT-X-ENDLIST\n'
        with PolicyYoutubeDL({'quiet': True}) as ydl, tempfile.TemporaryDirectory() as tmp:
            with patch.object(FFmpegFD, 'real_download', side_effect=AssertionError('Unfiltered child tool')):
                with self.assertRaises(TrackerBlocked):
                    ydl.dl(str(Path(tmp) / 'file.mp4'), {'url': 'https://example.test/video.m3u8',
                        'protocol': 'm3u8', 'ext': 'mp4', 'hls_media_playlist_data': manifest}, test=True)

    def test_policy_failure_reaches_user_without_sensitive_details(self):
        from ytdlp_host.diagnostics import safe_error
        for error in (TrackerBlocked('A known tracker request was blocked. SECRET'),
                      RuntimeError('This network downloader cannot apply request filtering. SECRET')):
            message = safe_error(error)
            self.assertIn('filter', message)
            self.assertNotIn('SECRET', message)

    def test_tracker_hosts_block_without_matching_cdns_or_lookalikes(self):
        for url in ('https://doubleclick.net/a', 'https://sub.google-analytics.com/x', 'https://GOOGLE-ANALYTICS.COM./x'):
            with self.assertRaises(TrackerBlocked):
                check_url(url)
        for url in ('https://video.twimg.com/a', 'https://rr1.googlevideo.com/a', 'https://accounts.google.com/a',
                    'https://doubleclick.net.example.test/a', 'https://example.test/doubleclick.net'):
            check_url(url)

    def test_initial_and_redirect_requests_pass_same_filter(self):
        with PolicyYoutubeDL({'quiet': True, 'proxy': ''}) as ydl:
            handler = next(iter(ydl._request_director.handlers.values()))
            opener = handler._create_instance({}, ydl.cookiejar)
            # urllib redirects recursively call opener.open; http_request runs
            # before DNS/connect for both initial and redirected destinations.
            with self.assertRaises(TrackerBlocked):
                opener.open(Request('https://doubleclick.net/SECRET?token=SECRET'))
            redirect = next(item for item in opener.handlers if hasattr(item, 'redirect_request'))
            original = Request('https://example.test/start')
            original.timeout = 1
            with self.assertRaises(TrackerBlocked):
                redirect.http_error_302(original, BytesIO(b''), 302, 'Found',
                                        {'location': 'https://doubleclick.net/SECRET'})

    def test_external_network_downloaders_rejected_before_spawn(self):
        with PolicyYoutubeDL({'quiet': True}) as ydl, tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(TrackerBlocked, 'network downloader'):
                ydl.dl(str(Path(tmp) / 'file'), {'url': 'rtmp://example.test/media', 'protocol': 'rtmp'})
            with self.assertRaisesRegex(TrackerBlocked, 'network downloader'):
                ydl.dl(str(Path(tmp) / 'file'), {'url': 'https://example.test/live.m3u8', 'protocol': 'm3u8', 'is_live': True})

    def test_probe_factory_enforces_policy_and_disables_remote_components(self):
        with probe.youtube_dl({'quiet': True, 'remote_components': {'ejs:github'}, 'external_downloader': 'curl'}) as ydl:
            self.assertIsInstance(ydl, PolicyYoutubeDL)
            self.assertFalse(ydl.params['remote_components'])
            self.assertFalse(ydl.params['external_downloader'])
            with self.assertRaises(TrackerBlocked):
                ydl.urlopen('https://doubleclick.net/SECRET')

    def test_controlled_server_observes_allowed_authenticated_transfer_and_no_tracker_redirect(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        observed = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                observed.append((self.path, self.headers.get('Cookie')))
                if self.path in ('/media.m3u8', '/segment'):
                    if self.headers.get('Referer') != 'https://example.test/watch':
                        self.send_response(403)
                        self.end_headers()
                        return
                    body = (b'#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n/segment\n#EXT-X-ENDLIST\n'
                            if self.path.endswith('m3u8') else b'media')
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', 'http://doubleclick.net/SECRET')
                else:
                    self.send_response(200)
                    self.send_header('Content-Length', '5')
                self.end_headers()
                if self.path != '/redirect':
                    self.wfile.write(b'media')
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with probe.youtube_dl({'quiet': True, 'proxy': ''}) as ydl:
                from yt_dlp.networking import Request
                url = f'http://127.0.0.1:{server.server_port}'
                with ydl.urlopen(Request(url + '/allowed', headers={'Cookie': 'session=PRIVATE'})) as response:
                    self.assertEqual(response.read(), b'media')
                with self.assertRaises(Exception) as caught:
                    ydl.urlopen(url + '/redirect')
                self.assertIn('tracker request was blocked', str(caught.exception))
                self.assertNotIn('SECRET', str(caught.exception))
            self.assertEqual(observed, [('/allowed', 'session=PRIVATE'), ('/redirect', None)])
            with probe.youtube_dl({'quiet': True, 'proxy': ''}) as ydl, tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / 'media.mp4'
                ydl.dl(str(target), {'url': url + '/media.m3u8', 'protocol': 'm3u8_native', 'ext': 'mp4',
                                    'http_headers': {'Referer': 'https://example.test/watch'}})
                self.assertEqual(target.read_bytes(), b'media')
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
