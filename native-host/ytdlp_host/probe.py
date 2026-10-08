"""Metadata discovery: title, thumbnail, formats, subtitles, playlist shape.

``probe`` runs a cheap *flat* extraction first. That single request tells us
whether the URL is a playlist, how many entries it has, and its title -- all
without resolving every video. Only when the URL turns out to be a single video
do we pay for a full extraction to collect the real format list.

Long playlists are never enumerated up front: the entry list is fetched
separately (:func:`list_entries`) and only when the user actually opens the
entry picker.
"""

from __future__ import annotations

import re
import math
import shutil
from urllib.parse import urlsplit
from typing import Any

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

from . import paths
from .sessions import session_context, cookie_jar

class _FileLogger:
    """Send yt-dlp's own output to the host log, never to the message stream."""

    # Progress and debug chatter is not a failure; diagnostics only keep problem codes.
    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        paths.write_log(f"yt-dlp: {msg}")

    def error(self, msg: str) -> None:
        paths.write_log(f"yt-dlp: {msg}")


def _as_float(value: Any) -> float:
    """Extractors occasionally report "variable" or None where a number belongs."""
    try:
        number = float(value)
        return number if math.isfinite(number) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


#: Format lists can run to several hundred entries on YouTube. The popup only
#: needs the useful tail of that list, and a trimmed list keeps the probe
#: payload far below the 1 MB native messaging cap.
MAX_FORMATS = 120
MAX_ENTRIES = 500

#: Extraction options shared by every call: quiet, bounded, and logged to file.
#: ``noprogress`` only silences the console renderer -- progress *hooks* keep
#: firing, which is how the extension gets its progress bar.
BASE_YDL_OPTS: dict[str, Any] = {
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "consoletitle": False,
    "nocheckcertificate": False,
    "socket_timeout": 20,
    "retries": 10,
    "extractor_retries": 3,
    "fragment_retries": 10,
    "ignoreerrors": False,
    "cachedir": False,
    # Public requests do not inherit local credentials or environment proxies.
    "usenetrc": False,
    "cookiefile": None,
    "cookiesfrombrowser": None,
    "proxy": "",
    # Without a logger yt-dlp writes to stdout, which is the message channel.
    "logger": _FileLogger(),
}

ALLOWED_SCHEMES = ("http://", "https://")


class ProbeError(Exception):
    """A probe failed for a reason worth showing the user verbatim."""


def validate_url(url: str) -> str:
    """Reject anything that is not an ordinary http(s) URL.

    This is the single choke point for untrusted input: the extension only ever
    forwards strings, and yt-dlp understands far more URL shapes than the
    extension does (file paths, ``ftp:``, ``dash:`` pseudo-protocols, ...).
    """
    if not isinstance(url, str) or not url.strip():
        raise ProbeError("No URL was provided.")
    url = url.strip()
    lowered = url.lower()
    if not lowered.startswith(ALLOWED_SCHEMES):
        raise ProbeError("Only http:// and https:// URLs are supported.")
    if len(url) > 4096:
        raise ProbeError("That URL is too long.")
    try:
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username or parsed.password or any(ord(c) < 32 for c in url):
            raise ValueError()
        parsed.port
    except ValueError as exc:
        raise ProbeError("Enter a valid http(s) URL without embedded credentials.") from exc
    return url


def _ydl(extra: dict[str, Any] | None = None) -> yt_dlp.YoutubeDL:
    opts = base_options()
    opts.update(extra or {})
    return youtube_dl(opts)


def youtube_dl(opts):
    from .request_policy import PolicyYoutubeDL
    ydl = PolicyYoutubeDL(opts)
    cookies = session_context.get()
    if cookies is not None:
        ydl.cookiejar = cookie_jar(cookies)
    return ydl


def base_options() -> dict[str, Any]:
    opts = dict(BASE_YDL_OPTS)
    # Explicitly enable Node as a fallback; yt-dlp only enables Deno by default.
    opts["js_runtimes"] = {
        name: {"path": executable}
        for name in ("deno", "node")
        if (executable := shutil.which(name))
    }
    return opts


def _run(url: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    with _ydl(extra) as ydl:
        return ydl.extract_info(url, download=False) or {}


def extract(url: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one extraction, turning yt-dlp failures into :class:`ProbeError`.

    The message is passed through untouched: yt-dlp's own extractor errors are
    the most informative thing we can show the user ("private video", "sign in
    to confirm your age", "unsupported URL"). Network-level failures are
    included, so the caller always gets one exception type to handle.
    """
    try:
        return _run(url, extra)
    except (DownloadError, ExtractorError, OSError, ValueError) as exc:
        raise ProbeError(str(exc) or exc.__class__.__name__) from exc


def _best_thumbnail(info: dict[str, Any]) -> str | None:
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbnails = info.get("thumbnails") or []
    if not thumbnails:
        return None
    usable = [t for t in thumbnails if t.get("url") and (t.get("preference") or 0) > 0]
    if not usable:
        usable = [t for t in thumbnails if t.get("url")]
    if not usable:
        return None
    best = max(usable, key=lambda t: (t.get("preference") or 0, t.get("width") or 0))
    return best.get("url")


def _pretty_size(num: Any) -> str | None:
    value = _as_float(num) if num else 0.0
    if value <= 0:
        return None
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return None




def _format_entry(fmt: dict[str, Any]) -> dict[str, Any] | None:
    if fmt.get("has_drm"):
        return None
    # A missing codec means "not reported" (direct media, generic pages), which
    # is different from an explicit "none" (audio-only stream, storyboard).
    vcodec = fmt.get("vcodec")
    acodec = fmt.get("acodec")
    if vcodec == "none" and acodec == "none":
        return None  # storyboards / data streams

    ext = fmt.get("ext") or "?"
    height = fmt.get("height")
    fps = fmt.get("fps")
    size = _pretty_size(fmt.get("filesize") or fmt.get("filesize_approx"))
    lang = fmt.get("language")
    lang_tag = f" [{lang}]" if lang and lang != "Original" else ""
    is_audio = vcodec == "none"

    if is_audio:
        abr = _as_float(fmt.get("abr") or fmt.get("tbr"))
        kind = "audio"
        label = f"Audio · {abr:.0f}kbps" if abr else "Audio"
        label = f"{label} · {ext}{lang_tag}"
    elif vcodec is None and acodec is None:
        kind = "video+audio"
        label = f"Direct media · {ext}{lang_tag}"
        if size:
            label += f" · {size}"
    else:
        resolution = f"{int(_as_float(height))}p" if height else (fmt.get("resolution") or "?")
        fps_value = _as_float(fps)
        if fps_value > 30:
            resolution += f"{fps_value:.0f}"
        codec = vcodec or "unknown"
        if acodec == "none":
            kind = "video only"
            label = f"{resolution} · video only · {codec}"
        else:
            kind = "video+audio"
            label = f"{resolution} · video+audio · {codec}"
        if size:
            label += f" · {size}"
        label += f" · {ext}{lang_tag}"
    if fmt.get("format_note"):
        label += f" · {fmt['format_note']}"

    return {
        "formatId": str(fmt.get("format_id") or ""),
        "ext": ext,
        "label": label,
        "kind": kind,
        "isAudio": is_audio,
        "isVideo": not is_audio,
        "height": int(_as_float(height)),
        "fps": _as_float(fps),
        "vcodec": vcodec or "none",
        "acodec": acodec or "none",
        "tbr": _as_float(fmt.get("tbr")),
        "filesize": _as_int(fmt.get("filesize") or fmt.get("filesize_approx")) or None,
        "protocol": fmt.get("protocol") or "",
    }


def _formats(info: dict[str, Any]) -> list[dict[str, Any]]:
    raw = list(info.get("formats") or [])
    if not raw and (info.get("url") or info.get("requested_formats")):
        # A single resolved stream (direct media, live HLS). It is exposed as one
        # selectable format whose id is "best", which is a real yt-dlp selector,
        # so the popup can hand the id straight back to the downloader.
        raw = [dict(info, format_id=info.get("format_id") or "best")]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fmt in sorted(
        raw,
        key=lambda f: (
            _as_float(f.get("height")),
            _as_float(f.get("vbr") or f.get("tbr")),
            _as_float(f.get("abr")),
        ),
        reverse=True,
    ):
        entry = _format_entry(fmt)
        if not entry or not entry["formatId"] or entry["formatId"] in seen:
            continue
        seen.add(entry["formatId"])
        out.append(entry)
    # Keep audio choices even on sites with hundreds of separate video streams.
    audio = [f for f in out if f["isAudio"]][:30]
    video = [f for f in out if not f["isAudio"]][:MAX_FORMATS - len(audio)]
    return video + audio


#: YouTube exposes one caption track per translation, with a random-looking
#: suffix: ``en-eEY6OEpapPo``, ``tr-nP7-2PuUl7o``. They all mean one language, so
#: they are grouped by base code for display while the exact code is kept for
#: the download. Real region codes (``en-GB``, ``zh-Hant``) are left alone:
#: only long, digit-bearing suffixes are treated as per-video variants.
ASR_SUFFIX = re.compile(r"^([a-z]{2,3}(?:-[A-Za-z]{2,4})?)-(?=[A-Za-z0-9-]*[0-9])[A-Za-z0-9-]{8,}$")

_LANGUAGE_NAMES = {
    "en": "English", "tr": "Turkish", "de": "German", "fr": "French",
    "es": "Spanish", "it": "Italian", "nl": "Dutch", "pt": "Portuguese",
    "ru": "Russian", "ja": "Japanese", "ko": "Korean", "zh": "Chinese",
    "ar": "Arabic", "hi": "Hindi", "pl": "Polish", "sv": "Swedish",
    "el": "Greek", "uk": "Ukrainian", "cs": "Czech", "ro": "Romanian",
    "hu": "Hungarian", "vi": "Vietnamese", "id": "Indonesian", "th": "Thai",
}


def _language_name(code: str) -> str:
    match = ASR_SUFFIX.match(code)
    if match:
        code = match.group(1)
    base = code.split("-")[0].lower()
    label = _LANGUAGE_NAMES.get(base, base)
    return label if code == base else f"{label} ({code})"


def _language_base(code: str) -> str:
    """Group key for the picker: every ``en-xxxxx`` track lands under ``en``.

    Regional codes are kept intact -- ``en-GB`` is a different track from the
    auto-generated ``en``, not another flavour of it.
    """
    match = ASR_SUFFIX.match(code)
    return match.group(1) if match else code


def _caption_rank(lang: str, is_manual: bool) -> tuple[int, int, str]:
    """Sort key for caption variants: plain code first, manual before auto."""
    suffix_penalty = 0 if not ASR_SUFFIX.match(lang) else 1
    return (suffix_penalty, 0 if is_manual else 1, lang)


def _subtitles(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge manual and automatic caption tracks, deduplicated by language.

    Variants of the same base language (``en-GB``, ``en-eEY6OEpapPo``) collapse
    onto the best code for that language, so the picker stays readable while the
    download still uses an exact code that yt-dlp understands.
    """
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    by_base: dict[str, dict[str, Any]] = {}

    for lang, is_manual in [(l, True) for l in manual] + [(l, False) for l in auto]:
        base = _language_base(lang)
        source = manual if is_manual else auto
        exts = sorted({(t or {}).get("ext", "") for t in source.get(lang, [])} - {""})
        entry = by_base.get(base)
        if entry is None:
            entry = {
                "lang": lang,
                "base": base,
                "name": _language_name(base),
                "variants": [],
                "manual": False,
                "auto": False,
                "exts": [],
            }
            by_base[base] = entry
        entry["manual"] = entry["manual"] or is_manual
        entry["auto"] = entry["auto"] or not is_manual
        entry["exts"] = sorted(set(entry["exts"]) | set(exts))
        entry["variants"].append({"lang": lang, "manual": is_manual, "exts": exts})

    entries = list(by_base.values())
    for entry in entries:
        entry["variants"].sort(key=lambda v: _caption_rank(v["lang"], v["manual"]))
        entry["lang"] = entry["variants"][0]["lang"]
    entries.sort(key=lambda e: (not e["manual"], e["name"].lower()))
    return entries[:100]


def _is_playlist(info: dict[str, Any]) -> bool:
    return info.get("_type") == "playlist" or (
        "entries" in info and info.get("_type") != "video"
    )


def _playlist_payload(info: dict[str, Any], url: str) -> dict[str, Any]:
    # A flat playlist yields None for entries it could not resolve (private,
    # deleted, geo-blocked). Those are normal, not a reason to fail the request.
    raw_entries = list(info.get("entries") or [])
    entries = [_entry_payload(e, i) for i, e in enumerate(raw_entries) if e]
    # ``n_entries`` counts only the entries this run resolved, which is capped by
    # ``playlistend``. ``playlist_count`` is the extractor's own total, so trust
    # only that one as an exact figure.
    reported = info.get("playlist_count")
    count = int(reported) if reported else (len(entries) or None)
    return {
        "url": url,
        "title": info.get("title") or "Playlist",
        "thumbnail": _best_thumbnail(info),
        "isPlaylist": True,
        "playlistCount": count,
        "playlistCountIsExact": bool(reported),
        "playlistId": info.get("id"),
        "playlistUploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "entries": entries[:MAX_ENTRIES],
        "entriesTruncated": bool(count and count > len(entries)),
        "durationString": _duration_string(info.get("duration")),
    }


def _entry_payload(entry: dict[str, Any], index: int) -> dict[str, Any]:
    # Re-extract the page when available: a resolved stream URL alone loses
    # captions and the site's authentication/extraction context.
    url = entry.get("webpage_url") or entry.get("url") or ""
    # Flat entries often carry a protocol-relative or bare watch id; normalise
    # to something we can validate later, and drop anything not http(s).
    if url and not url.lower().startswith(ALLOWED_SCHEMES):
        url = ""
    # Preserve holes left by private/deleted entries and extractor numbering.
    index = _as_int(entry.get("playlist_index")) - 1 if entry.get("playlist_index") else index
    return {
        "index": index,
        "id": str(entry.get("id") or ""),
        "title": str(entry.get("title") or entry.get("fulltitle") or f"Entry {index + 1}")[:300],
        "url": url,
        "duration": entry.get("duration"),
        "durationString": _duration_string(entry.get("duration")),
    }


def _duration_string(seconds: Any) -> str | None:
    seconds = _as_float(seconds)
    if not seconds or seconds < 0:
        return None
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def probe(url: str, single: bool = False) -> dict[str, Any]:
    """Resolve ``url`` into everything the popup needs to describe it."""
    url = validate_url(url)

    # Phase 1. ``extract_flat='in_playlist'`` means: fully resolve a plain
    # video (one request, formats included) but only enumerate the entries of a
    # playlist. A big playlist therefore costs the same as a single video, and
    # the popup learns the entry count without resolving a thousand pages.
    info = extract(url, {"extract_flat": "in_playlist", "playlistend": 3, "noplaylist": single})
    if _is_playlist(info):
        return _playlist_payload(info, url)

    # Phase 2 only runs when phase 1 was itself flat (an extractor that already
    # told us this is a video but returned no format list).
    if info.get("_type") in ("url", "url_transparent"):
        info = extract(url, {"noplaylist": True})
        if _is_playlist(info):  # flat detection can miss exotic extractors
            return _playlist_payload(info, url)

    return {
        "url": url,
        "title": info.get("title") or info.get("id") or url,
        "thumbnail": _best_thumbnail(info),
        "isPlaylist": False,
        "duration": info.get("duration"),
        "durationString": _duration_string(info.get("duration")),
        "uploader": info.get("uploader") or info.get("channel"),
        "formats": _formats(info),
        "subtitles": _subtitles(info),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "isLive": bool(info.get("is_live")),
    }


def list_entries(url: str, limit: int = MAX_ENTRIES, offset: int = 0) -> dict[str, Any]:
    """Enumerate playlist entries for the entry picker.

    ``limit`` is capped: some playlists have thousands of entries and the point
    of this request is a human-sized checklist.
    """
    url = validate_url(url)
    limit = max(1, min(int(limit or MAX_ENTRIES), MAX_ENTRIES))
    offset = max(0, int(offset))
    info = extract(url, {"extract_flat": True, "playliststart": offset + 1,
                         "playlistend": offset + limit, "ignoreerrors": True})
    if not _is_playlist(info):
        return {"url": url, "entries": [], "total": 1, "title": info.get("title")}
    # None entries are the ones yt-dlp could not resolve; they would otherwise
    # break the checklist for a playlist that only lost one video.
    raw = list(info.get("entries") or [])[:limit]
    entries = [_entry_payload(e, offset + i) for i, e in enumerate(raw) if e]
    total = info.get("playlist_count") or offset + len(raw)
    more = (offset + len(raw) < _as_int(total) or len(raw) == limit) and bool(raw)
    return {
        "url": url,
        "title": info.get("title"),
        "entries": entries,
        "total": int(total) if total else len(entries),
        "truncated": more,
        "nextOffset": offset + len(raw) if more else None,
    }


def resolve_entry_url(playlist_url: str, playlist_item: int) -> str:
    """Resolve a single playlist entry that had no usable URL in flat mode."""
    playlist_url = validate_url(playlist_url)
    info = extract(playlist_url, {"playlist_items": str(playlist_item), "noplaylist": False})
    if _is_playlist(info):
        entries = [e for e in (info.get("entries") or []) if e]
        if entries:
            info = entries[0]
    url = info.get("webpage_url") or ""
    if not url:
        return playlist_url
    if not url.lower().startswith("http"):
        candidate = info.get("id") or ""
        if candidate and re.fullmatch(r"[\w-]{6,}", candidate):
            url = f"https://www.youtube.com/watch?v={candidate}"
        else:
            return playlist_url
    return url


def download_options(spec: dict[str, Any], ffmpeg_found: bool) -> dict[str, Any]:
    """Translate an extension ``download_start`` payload into yt-dlp options."""
    opts: dict[str, Any] = {**base_options(),
        "format": spec.get("formatId") or "mp4",
        "noplaylist": True,
        "playlist_items": "1",
        "overwrites": False,
        "continuedl": True,
        "windowsfilenames": True,
        "trim_file_name": 200,
    }

    if spec.get("audioOnly"):
        if not ffmpeg_found:
            raise ValueError("MP3 extraction requires ffmpeg. Install ffmpeg and restart the host.")
        opts["format"] = spec.get("formatId") or "bestaudio/best"
        if ffmpeg_found:
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ]
    elif opts["format"] == "mp4":
        if ffmpeg_found:
            # Prefer native MP4/M4A streams. If only another container exists,
            # remux it without re-encoding; the resulting file really is MP4.
            opts["format"] = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
            opts["merge_output_format"] = "mp4"
            opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
        else:
            opts["format"] = "b[ext=mp4]"
    if not ffmpeg_found and "+" in opts["format"]:
        raise ValueError("Merging video and audio requires ffmpeg. Choose a combined format.")

    manual = [s["lang"] for s in (spec.get("subtitles") or []) if s.get("lang") and not s.get("auto")]
    auto = [s["lang"] for s in (spec.get("subtitles") or []) if s.get("lang") and s.get("auto")]
    langs = manual + auto
    if langs:
        if manual:
            opts["writesubtitles"] = True
        if auto:
            opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = langs
        opts["subtitlesformat"] = "srt/best"
        if ffmpeg_found:
            opts.setdefault("postprocessors", []).append(
                {"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"}
            )

    return opts


def select_subtitles(ydl: yt_dlp.YoutubeDL, selected: list[dict[str, Any]]) -> None:
    """Apply the requested source per language, including mixed manual/auto.

    yt-dlp's global flags otherwise let a manual track override an explicitly
    chosen automatic track when another language requests manual subtitles.
    """
    original = ydl.process_subtitles
    def process(video_id, normal, automatic):
        def keep(tracks, auto):
            wanted = {s["lang"] for s in selected if bool(s.get("auto")) == auto}
            kept = {}
            for lang, entries in (tracks or {}).items():
                key = lang if lang in wanted else _language_base(lang)
                if key in wanted and (key not in kept or key == lang):
                    kept[key] = entries
            return kept
        return original(video_id, keep(normal, False), keep(automatic, True))
    ydl.process_subtitles = process
