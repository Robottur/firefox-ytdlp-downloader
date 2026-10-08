"""Download short public test clips into a temporary directory, then remove them."""
import json
import os
from pathlib import Path
import queue
import sys
import subprocess
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native-host"))
from ytdlp_host import jobs, paths, probe


def main():
    results = []
    with tempfile.TemporaryDirectory(prefix="ytdlp-public-test-") as tmp:
        os.environ["YTDLP_HOST_STATE_DIR"] = str(Path(tmp) / "state")
        store = jobs.JobStore(Path(tmp) / "jobs.db")
        events = queue.Queue()
        manager = jobs.JobManager(store, events.put, ffmpeg_found=True, default_dir=tmp, max_concurrent=1)
        manager.start()
        try:
            for url in sys.argv[1:]:
                info = probe.probe(url)
                if info.get("isLive") or (info.get("duration") or 0) > 300:
                    raise ValueError("This test only downloads clips shorter than five minutes, excluding live streams.")
                started = time.monotonic()
                job = manager.submit({"url": url, "title": info["title"],
                    "formatId": "mp4", "outputDir": tmp})
                while True:
                    event = events.get(timeout=120)
                    if event.get("jobId") != job.id or event.get("type") not in ("complete", "error"): continue
                    if event["type"] == "error": raise RuntimeError(event["message"])
                    path = Path(event["filepath"])
                    assert path.is_file() and path.stat().st_size > 0
                    assert path.suffix == ".mp4", path
                    media = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]))
                    assert "mp4" in media["format"]["format_name"]
                    assert {s["codec_type"] for s in media["streams"]} >= {"video", "audio"}
                    results.append({"url": url, "title": job.title, "seconds": round(time.monotonic() - started, 2),
                        "bytes": path.stat().st_size, "extension": path.suffix})
                    break
        finally:
            manager.shutdown(); store.close(); paths.close_log()
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
