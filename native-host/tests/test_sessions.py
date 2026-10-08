import unittest
from unittest.mock import patch
from ytdlp_host.sessions import Sessions, session_context
from ytdlp_host import probe

COOKIE = {'name':'session', 'value':'secret', 'domain':'.example.test', 'path':'/', 'secure':True, 'hostOnly':False, 'session':True}

class SessionTests(unittest.TestCase):
    def test_scoped_cookie_transfer_and_isolation(self):
        sessions = Sessions()
        token = sessions.create('https://example.test/watch', [COOKIE])
        with sessions.use(token, 'https://example.test/watch'):
            with probe._ydl() as authenticated:
                self.assertEqual(authenticated.cookiejar.get_cookie_header('https://example.test/video'), 'session=secret')
                self.assertIsNone(authenticated.cookiejar.get_cookie_header('https://unrelated.test/video'))
                self.assertIsNone(authenticated.cookiejar.get_cookie_header('http://example.test/video'))
        with probe._ydl() as public:
            self.assertEqual(len(public.cookiejar), 0)

    def test_host_only_cookie_never_reaches_subdomain(self):
        from ytdlp_host.sessions import cookie_jar
        jar = cookie_jar([{**COOKIE, 'domain': 'example.test', 'hostOnly': True}])
        self.assertEqual(jar.get_cookie_header('https://example.test/watch'), 'session=secret')
        self.assertIsNone(jar.get_cookie_header('https://sub.example.test/watch'))

    def test_expiry_and_release(self):
        sessions = Sessions()
        with patch('ytdlp_host.sessions.time.monotonic', return_value=0):
            token = sessions.create('https://example.test/watch', [COOKIE])
        with patch('ytdlp_host.sessions.time.monotonic', return_value=601):
            with self.assertRaises(ValueError):
                sessions.acquire(token, 'https://example.test/watch')
        token = sessions.create('https://example.test/watch', [COOKIE])
        sessions.drop(token)
        with self.assertRaises(ValueError):
            sessions.acquire(token, 'https://example.test/watch')

    def test_refuse_cross_site_or_malformed_cookies(self):
        sessions = Sessions()
        with self.assertRaises(ValueError):
            sessions.create('https://example.test/watch', [{**COOKIE, 'domain':'.other.test'}])
        with self.assertRaises(ValueError):
            sessions.create('https://example.test/watch', [{**COOKIE, 'value':'bad\r\nheader'}])
        token = sessions.create('https://example.test/watch', [COOKIE])
        with self.assertRaises(ValueError):
            sessions.acquire(token, 'https://unrelated.test/video')

class AuthenticatedQueueTests(unittest.TestCase):
    def setUp(self):
        from tests.test_jobs import Harness
        self.h = Harness()
        self.addCleanup(self.h.cleanup)

    def test_credentials_are_not_persisted_and_retry_requires_new_consent(self):
        from ytdlp_host import jobs
        job = self.h.submit(url='https://example.test/watch', authRequired=True, _sessionCookies=[COOKIE])
        self.assertTrue(job.auth_required)
        restored = self.h.store.get(job.id)
        self.assertTrue(restored.auth_required)
        self.assertIsNone(restored._session_cookies)
        self.assertNotIn('secret', str(job.to_row()))
        self.assertNotIn('secret', str(job.to_wire()))
        self.h.manager._finish(job, jobs.ERROR, error='Failed')
        self.assertIsNone(job._session_cookies)
        with self.assertRaisesRegex(ValueError, 'fresh signed-in consent'):
            self.h.manager.retry(job.id)

    def test_public_job_remains_public_after_roundtrip(self):
        job = self.h.submit()
        self.assertFalse(self.h.store.get(job.id).auth_required)

    def test_playlist_children_keep_isolated_credentials_until_finished(self):
        from ytdlp_host import jobs
        parent = self.h.submit(url='https://example.test/list', playlistMode='all', authRequired=True, _sessionCookies=[COOKIE])
        parent.status = jobs.DOWNLOADING
        self.h.manager._spawn_child(parent, {'url':'https://example.test/one','index':0}, 0)
        self.h.manager._spawn_child(parent, {'url':'https://example.test/two','index':1}, 1)
        children = self.h.children(parent)
        self.assertTrue(all(c.auth_required for c in children))
        self.h.manager.cancel(children[0].id)
        self.assertIsNone(children[0]._session_cookies)
        self.assertEqual(children[1]._session_cookies, [COOKIE])
        self.h.manager._finish(children[1], jobs.COMPLETE)
        self.assertIsNone(parent._session_cookies)

    def test_recovery_never_automatically_resumes_authenticated_work(self):
        from ytdlp_host import jobs
        job = self.h.submit(url='https://example.test/watch', authRequired=True, _sessionCookies=[COOKIE])
        self.h.manager.recover()
        restored = self.h.store.get(job.id)
        self.assertEqual(restored.status, jobs.INTERRUPTED)
        self.assertTrue(restored.auth_required)
        self.assertIsNone(restored._session_cookies)

    def test_retry_preserves_selected_playlist_choices_with_fresh_cookies(self):
        from ytdlp_host import jobs
        original = self.h.submit(url='https://example.test/list', playlistMode='select', entryIndexes=[1,4],
            formatId='mp4', audioOnly=False, subtitles=[{'lang':'en','auto':True}],
            authRequired=True, _sessionCookies=[COOKIE], overwrite=True)
        self.h.manager._finish(original, jobs.ERROR, error='Interrupted')
        retry = self.h.manager.retry(original.id, cookies=[COOKIE])
        self.assertEqual(retry.playlist_mode, 'select')
        self.assertEqual([e['index'] for e in retry.entries], [1,4])
        self.assertEqual(retry.subtitle_langs, original.subtitle_langs)
        self.assertEqual(retry.output_dir, original.output_dir)
        self.assertEqual(retry.format_id, original.format_id)
        self.assertFalse(retry.overwrite)
        self.assertEqual(retry._session_cookies, [COOKIE])

    def test_signed_media_sources_and_entry_urls_are_not_saved(self):
        from ytdlp_host import jobs
        signed = self.h.submit(url='https://cdn.example.test/video.mp4?token=secret')
        self.assertIsNone(self.h.store.get(signed.id))
        self.assertFalse(signed.to_wire()['recoveryAvailable'])
        playlist = self.h.submit(url='https://www.youtube.com/playlist?list=PL123', playlistMode='all')
        playlist.entries = [{'index':0,'url':signed.url,'title':'Entry'}]
        self.h.store.upsert(playlist)
        restored = self.h.store.get(playlist.id)
        self.assertEqual(restored.entries, [{'index':0}])
