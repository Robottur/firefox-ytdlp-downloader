"""Probe user-supplied public URLs; never downloads their media."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native-host"))
from ytdlp_host import probe


def check(url):
    start = time.monotonic()
    try:
        info = probe.probe(url)
        return {"url": url, "seconds": round(time.monotonic() - start, 2),
                "title": info.get("title"), "playlist": info.get("isPlaylist"),
                "formats": len(info.get("formats", [])),
                "subtitles": len(info.get("subtitles", []))}
    except Exception as exc:
        return {"url": url, "seconds": round(time.monotonic() - start, 2), "error": str(exc)}


if __name__ == "__main__":
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(check, sys.argv[1:]))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    raise SystemExit(int(any("error" in result for result in results)))
