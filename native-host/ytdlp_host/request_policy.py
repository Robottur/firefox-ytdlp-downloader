"""Best-effort local tracker filtering for app-owned yt-dlp requests.

Rules: ytdlp-host curated domain rules v1 (2026-10-08), authored for this
project under its MIT license; no imported list or remote updates. This
intentionally small list cannot distinguish tracking in mixed-purpose APIs.
"""
from urllib.parse import urlsplit
from urllib.request import BaseHandler

import yt_dlp
from yt_dlp.downloader import get_suitable_downloader
from yt_dlp.downloader.http import HttpFD
from yt_dlp.downloader.hls import HlsFD
from yt_dlp.downloader.dash import DashSegmentsFD
from yt_dlp.networking._urllib import UrllibRH
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import RequestError
from yt_dlp.dependencies import Cryptodome

TRACKER_DOMAINS = frozenset({'doubleclick.net', 'google-analytics.com', 'googlesyndication.com',
                             'scorecardresearch.com', 'ads-twitter.com'})


class TrackerBlocked(RequestError):
    """A safe explanation without URLs, cookies, or arbitrary extractor text."""


def check_url(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').rstrip('.').lower()
    except (ValueError, TypeError):
        raise TrackerBlocked('Request policy rejected an invalid address.') from None
    if parsed.scheme not in {'http', 'https'}:
        raise TrackerBlocked('Request policy does not support this network protocol. Choose another format.')
    if any(host == domain or host.endswith('.' + domain) for domain in TRACKER_DOMAINS):
        raise TrackerBlocked('A known tracker request was blocked. Try another format or source; filtering remains enabled.')


class _PolicyHandler(BaseHandler):
    # Runs before proxy, HTTP, and HTTPS handlers and on every redirected request.
    handler_order = 100

    def http_request(self, request):
        check_url(request.full_url)
        return request

    https_request = http_request


class _PolicyUrllibRH(UrllibRH):
    def _create_instance(self, *args, **kwargs):
        opener = super()._create_instance(*args, **kwargs)
        opener.add_handler(_PolicyHandler())
        return opener


class PolicyYoutubeDL(yt_dlp.YoutubeDL):
    def __init__(self, params=None, *args, **kwargs):
        options = dict(params or {})
        options.update(external_downloader={}, hls_prefer_native=True,
                       remote_components=set(), enable_file_urls=False)
        super().__init__(options, *args, **kwargs)

    def build_request_director(self, handlers, preferences=None):
        # A single transport avoids implicit fallback to unfiltered redirect stacks.
        return super().build_request_director([_PolicyUrllibRH])

    def urlopen(self, request):
        url = request if isinstance(request, str) else getattr(request, 'url', getattr(request, 'full_url', ''))
        check_url(url)
        return super().urlopen(request)

    def dl(self, name, info, subtitle=False, test=False):
        # yt-dlp's format checks discard downloader preferences. Validate the
        # effective selection too, so checks cannot launch an external tool.
        downloader = get_suitable_downloader(info, {} if test else self.params, to_stdout=name == '-')
        if downloader not in {HttpFD, HlsFD, DashSegmentsFD}:
            raise TrackerBlocked('This network downloader cannot apply request filtering. Choose a direct HTTP, native HLS, or DASH format.')
        if downloader is HlsFD:
            # HlsFD can silently delegate unsupported manifests to ffmpeg.
            # Fetch once through the policy and reject every fallback condition.
            manifest = info.get('hls_media_playlist_data')
            if not manifest:
                with self.urlopen(Request(info['url'], headers=info.get('http_headers'))) as response:
                    manifest = response.read().decode('utf-8', errors='replace')
                    info = {**info, 'url': response.url, 'hls_media_playlist_data': manifest}
            if (not manifest.strip().startswith('#EXTM3U')
                    or not HlsFD.can_download(manifest, info, self.params.get('allow_unplayable_formats'))
                    or (not Cryptodome.AES and '#EXT-X-KEY:METHOD=AES-128' in manifest)):
                raise TrackerBlocked('This network downloader requires an unfiltered external tool. Choose another format.')
        return super().dl(name, info, subtitle=subtitle, test=test)
