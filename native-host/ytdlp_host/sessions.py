"""Short-lived, operation-scoped credentials. Never persisted or serialized."""
from contextlib import contextmanager
from contextvars import ContextVar
from http.cookiejar import Cookie, DefaultCookiePolicy
import secrets
import math
import threading
import time
from urllib.parse import urlsplit

from yt_dlp.cookies import YoutubeDLCookieJar

session_context = ContextVar('download_session', default=None)


def cookie_jar(cookies):
    jar = YoutubeDLCookieJar()
    jar.set_policy(DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
    for c in cookies:
        jar.set_cookie(Cookie(0, c['name'], c['value'], None, False, c['domain'],
            not c.get('hostOnly', False), c['domain'].startswith('.'), c.get('path', '/'),
            True, c.get('secure', False), None if c.get('session', False) else int(c.get('expirationDate', 0)),
            c.get('session', False), None, None, {}))
    return jar


@contextmanager
def using_session(cookies):
    token = session_context.set(cookies)
    try:
        yield
    finally:
        session_context.reset(token)


class Sessions:
    def __init__(self):
        self._values = {}
        self._lock = threading.Lock()

    def _expire(self):
        now = time.monotonic()
        self._values = {k: v for k, v in self._values.items() if now - v[2] < 600}

    def create(self, url, cookies):
        source = urlsplit(url)
        host = source.hostname or ''
        if source.scheme != 'https' or source.username or source.password or not host:
            raise ValueError('Signed-in sessions require an HTTPS source.')
        if not isinstance(cookies, list) or not 1 <= len(cookies) <= 500:
            raise ValueError('No usable signed-in cookies were found, or the session is too large.')
        clean = []
        size = 0
        for c in cookies:
            if not isinstance(c, dict):
                raise ValueError('Invalid session cookie.')
            for key in ('name', 'value', 'domain', 'path'):
                if not isinstance(c.get(key), str) or any(ord(ch) < 32 or ord(ch) == 127 for ch in c[key]):
                    raise ValueError('Invalid session cookie.')
            domain = c['domain'].lstrip('.').lower()
            if '.' not in domain or not (host == domain or (not c.get('hostOnly') and host.endswith('.' + domain))):
                raise ValueError('Session cookies must belong to the selected site.')
            if not c['path'].startswith('/') or ';' in c['name'] or '=' in c['name'] or ';' in c['value']:
                raise ValueError('Invalid session cookie.')
            if not c.get('session') and (not isinstance(c.get('expirationDate'), (int, float)) or not math.isfinite(c['expirationDate'])):
                raise ValueError('Invalid session expiry.')
            safe = {k: c[k] for k in ('name', 'value', 'domain', 'path', 'secure', 'hostOnly', 'session', 'expirationDate') if k in c}
            size += sum(len(str(v)) for v in safe.values())
            clean.append(safe)
        if size > 256000:
            raise ValueError('The session is too large.')
        with self._lock:
            self._expire()
            if len(self._values) >= 32:
                raise ValueError('Too many unused sessions. Finish or cancel a previous operation.')
            token = secrets.token_hex(24)
            self._values[token] = (host, clean, time.monotonic())
        expiry = threading.Timer(600, self._expire_token, args=(token,))
        expiry.daemon = True
        expiry.start()
        return token

    def _expire_token(self, token):
        with self._lock:
            value = self._values.get(token)
            if value is None:
                return
            remaining = 600 - (time.monotonic() - value[2])
            if remaining <= 0:
                self._values.pop(token, None)
                return
        expiry = threading.Timer(remaining, self._expire_token, args=(token,))
        expiry.daemon = True
        expiry.start()

    def acquire(self, token, url, consume=False):
        with self._lock:
            self._expire()
            value = self._values.get(token)
            if value is None:
                raise ValueError('Session expired. Enable your signed-in session again.')
            host, cookies, _ = value
            target = urlsplit(url)
            if target.scheme != 'https' or target.hostname != host:
                raise ValueError('Choose a new signed-in session for this source site.')
            if consume:
                self._values.pop(token)
            else:
                self._values[token] = (host, cookies, time.monotonic())
            return cookies

    @contextmanager
    def use(self, token, url):
        with using_session(self.acquire(token, url)):
            yield

    def drop(self, token):
        with self._lock:
            self._values.pop(token, None)
