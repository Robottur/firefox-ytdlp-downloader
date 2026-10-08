"""Opt-in loopback HTTPS fixture: real host handlers, yt-dlp, cookies and media.

Uses synthetic cookies and a temporary self-signed certificate only. Certificate
verification is relaxed inside this fixture process, never production settings.
"""
import json
import os
from pathlib import Path
import queue
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'native-host'))
from ytdlp_host.main import Host
from ytdlp_host import probe, paths


def main():
    assert probe.BASE_YDL_OPTS['nocheckcertificate'] is False
    with tempfile.TemporaryDirectory(prefix='ytdlp-auth-') as temporary:
        root = Path(temporary)
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(root/'key.pem'),
                        '-out', str(root/'cert.pem'), '-days', '1', '-subj', '/CN=127.0.0.1',
                        '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, capture_output=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x120:rate=10:duration=1',
                        '-c:v', 'libx264', '-preset', 'ultrafast', str(root/'sample.mp4')], check=True)
        media = (root/'sample.mp4').read_bytes()
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self): self.reply(head=True)
            def do_GET(self): self.reply()
            def log_message(self, *_): pass
            def reply(self, head=False):
                authenticated = self.headers.get('Cookie') == 'member=fixture-secret'
                seen.append((self.path, authenticated))
                if self.path.startswith('/member') and not authenticated:
                    self.send_response(403); self.end_headers(); return
                if self.path.endswith('.mp4'):
                    body, kind = media, 'video/mp4'
                elif self.path.endswith('.vtt'):
                    body, kind = b'WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nMember caption.\n', 'text/vtt'
                elif self.path == '/member-list':
                    body, kind = (b'<title>Member playlist</title><video src="/member.mp4">'
                                  b'<track kind="subtitles" src="/member.vtt" srclang="en"></video>'
                                  b'<video src="/member2.mp4">'
                                  b'<track kind="subtitles" src="/member.vtt" srclang="en"></video>', 'text/html')
                else:
                    body, kind = b'<title>Member fixture</title><video src="/member.mp4"></video>', 'text/html'
                self.send_response(200); self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(body))); self.end_headers()
                if not head: self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(root/'cert.pem', root/'key.pem')
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        messages = queue.Queue()
        host = None
        try:
            with patch.dict(os.environ, {'YTDLP_HOST_STATE_DIR': str(root/'state')}), patch.dict(probe.BASE_YDL_OPTS, {'nocheckcertificate': True, 'retries':0}):
                host = Host(send=messages.put)
                counter = 0
                def request(kind, **payload):
                    nonlocal counter
                    counter += 1
                    key = str(counter)
                    host.handle({'type':kind, 'requestId':key, **payload})
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        result = messages.get(timeout=20)
                        if result.get('requestId') == key: return result
                    raise AssertionError('No response')
                url = f'https://127.0.0.1:{server.server_port}/member'
                public = request('probe', url=url)
                assert public['type'] == 'error', public
                consent = request('authorize_session', url=url, cookies=[{'name':'member','value':'fixture-secret',
                    'domain':'127.0.0.1','path':'/','secure':True,'session':True,'hostOnly':True}])
                token = consent['sessionToken']
                metadata = request('probe', url=url, sessionToken=token)
                assert metadata['type'] == 'probe_result', metadata
                started = request('download_start', url=url, sessionToken=token, outputDir=str(root/'downloads'), formatId='mp4')
                assert started['type'] == 'queued', started
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    result = messages.get(timeout=20)
                    if result.get('jobId') == started['jobId'] and result.get('type') in ('complete','error'):
                        assert result['type'] == 'complete', result
                        assert Path(result['filepath']).read_bytes() == media
                        break
                else: raise AssertionError('No terminal result')
                assert request('probe', url=url, sessionToken=token)['type'] == 'error', 'Consumed grant must be invalid'
                for mode, indexes, count in [('all', [], 2), ('select', [1], 1)]:
                    playlist_url = url + '-list'
                    grant = request('authorize_session', url=playlist_url, cookies=[{'name':'member',
                        'value':'fixture-secret', 'domain':'127.0.0.1', 'path':'/', 'secure':True,
                        'session':True, 'hostOnly':True}])['sessionToken']
                    assert request('probe', url=playlist_url, sessionToken=grant)['isPlaylist']
                    operation = request('download_start', url=playlist_url, sessionToken=grant,
                        outputDir=str(root/mode), formatId='mp4', playlistMode=mode, entryIndexes=indexes,
                        subtitles=[{'lang':'en', 'auto':False}])
                    assert operation['type'] == 'queued', operation
                    while True:
                        event = messages.get(timeout=20)
                        if event.get('jobId') == operation['jobId'] and event.get('type') in ('complete', 'error'):
                            assert event['type'] == 'complete', event
                            break
                    outputs = list((root/mode).glob('*.mp4'))
                    captions = list((root/mode).glob('*.srt'))
                    assert len(outputs) == len(captions) == count, (outputs, captions)
                    assert all(file.read_bytes() == media for file in outputs)
                    assert all('Member caption.' in file.read_text() for file in captions)
                    assert host._store.get(operation['jobId']) is None
                assert request('probe', url=url, refresh=True)['type'] == 'error', 'Public context must not reuse cookies/cache'
                assert seen[-1] == ('/member', False), seen
                assert host._store.get(started['jobId']) is None
                host.stop(); host = None
                for file in (root/'state').glob('*'):
                    if file.is_file():
                        assert b'fixture-secret' not in file.read_bytes(), file.name
                        assert token.encode() not in file.read_bytes(), file.name
                print('PASS: HTTPS member-only video, all/selected playlists and subtitles; no public cookie reuse; consumed consent; no credentials or terminal jobs persisted')
        finally:
            if host: host.stop()
            paths.close_log()
            server.shutdown(); server.server_close()

if __name__ == '__main__': main()
