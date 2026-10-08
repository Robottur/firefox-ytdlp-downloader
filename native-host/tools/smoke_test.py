"""End-to-end smoke test: drive the real host process over its stdio pipe.

    python3 tools/smoke_test.py [url]

With no argument it uses a locally generated one-second video served over HTTP,
so the test is hermetic. Pass a URL to exercise a real site instead.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ytdlp_host.protocol import encode_message, read_message  # noqa: E402


class Client:
    def __init__(self, command: list[str]) -> None:
        self.proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None
        )
        self.counter = 0
        self.messages: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        stream = self.proc.stdout
        while not self._stop.is_set():
            try:
                message = read_message(stream)
            except Exception as exc:
                print(f"  !! read error: {exc}")
                return
            if message is None:
                return
            with self._lock:
                self.messages.append(message)
            kind = message.get("type")
            if kind not in ("progress", "job"):
                print(f"  <- {json.dumps(message)[:220]}")

    def send(self, **payload) -> str:
        self.counter += 1
        request_id = f"r{self.counter}"
        payload["requestId"] = request_id
        self.proc.stdin.write(encode_message(payload))
        self.proc.stdin.flush()
        return request_id

    def wait_for(self, predicate, timeout=90.0, label=""):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for message in self.messages:
                    if predicate(message):
                        return message
            if self.proc.poll() is not None:
                raise SystemExit(f"host exited early ({self.proc.returncode}) waiting for {label}")
            time.sleep(0.05)
        raise SystemExit(f"timed out waiting for {label}")

    def of_type(self, kind: str) -> list[dict]:
        with self._lock:
            return [m for m in self.messages if m.get("type") == kind]

    def close(self) -> None:
        self._stop.set()
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def serve_sample(directory: Path) -> str:
    """Create a tiny mp4 and serve the folder over HTTP."""
    sample = directory / "sample.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "libx264", "-preset",
         "ultrafast", "-c:a", "aac", "-shortest", str(sample)],
        check=True, capture_output=True,
    )
    (directory / "index.html").write_text(
        "<!doctype html><title>smoke</title>"
        f'<video controls src="{sample.name}"></video>',
        encoding="utf-8",
    )
    handler = lambda *a, **kw: SimpleHTTPRequestHandler(*a, directory=str(directory), **kw)  # noqa: E731
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/index.html"


def main() -> int:
    # Never let a test use the user's installed host database/history.
    import tempfile
    test_state = tempfile.TemporaryDirectory(prefix="ytdlp-smoke-state-")
    os.environ["YTDLP_HOST_STATE_DIR"] = test_state.name
    os.environ["YTDLP_HOST_IDLE_SECONDS"] = "1"
    workdir = Path(os.environ.get("SMOKE_DIR", "/tmp/ytdlp-host-smoke"))
    workdir.mkdir(parents=True, exist_ok=True)
    url = sys.argv[1] if len(sys.argv) > 1 else serve_sample(workdir)
    print(f"target: {url}")

    client = Client([sys.executable, "-X", "utf8", "-m", "ytdlp_host.main", "--stdio"])
    try:
        ready = client.wait_for(lambda m: m.get("type") in ("host_ready", "host_error"), 30, "host_ready")
        if ready["type"] == "host_error":
            raise SystemExit(f"host failed: {ready}")
        print(f"  yt-dlp {ready['ytDlpVersion']}, ffmpeg={ready['ffmpegFound']}, "
              f"state dir {ready['stateDir']}")

        # 1. probe
        client.send(type="probe", url=url)
        probe = client.wait_for(lambda m: m.get("type") == "probe_result", 120, "probe_result")
        print(f"  title: {probe['title']}")
        print(f"  playlist: {probe['isPlaylist']}  formats: {len(probe['formats'])}  "
              f"subtitles: {len(probe['subtitles'])}")
        for fmt in probe["formats"][:4]:
            print(f"    - {fmt['formatId']}: {fmt['label']}")

        # 2. download
        target = workdir / "out"
        target.mkdir(exist_ok=True)
        client.send(
            type="download_start",
            url=probe["url"],
            title=probe["title"],
            formatId="best",
            outputDir=str(target),
        )
        queued = client.wait_for(lambda m: m.get("type") == "queued", 30, "queued")
        job_id = queued["jobId"]
        print(f"  queued as {job_id}")

        # 3. progress + completion
        deadline = time.time() + 180
        seen_progress = 0
        while time.time() < deadline:
            seen_progress = len(client.of_type("progress"))
            finals = [
                m for m in client.messages
                if m.get("jobId") == job_id and m.get("type") in ("complete", "error")
            ]
            if finals:
                break
            time.sleep(0.2)
        else:
            raise SystemExit("download did not finish in time")

        final = finals[-1]
        if final["type"] == "error":
            print(f"  !! download failed: {final['message']}")
            return 1
        print(f"  progress messages: {seen_progress}")
        print(f"  finished: {final['filepath']}")
        size = Path(final["filepath"]).stat().st_size if final.get("filepath") else 0
        print(f"  file size: {size} bytes")

        # 4. list_jobs reflects the finished job
        client.send(type="list_jobs")
        jobs = client.wait_for(lambda m: m.get("type") == "jobs", 20, "jobs")
        match = [j for j in jobs["jobs"] if j["jobId"] == job_id]
        print(f"  list_jobs: {len(jobs['jobs'])} jobs, ours is {match[0]['status'] if match else 'MISSING'}")
        print(f"  settings: {json.dumps(jobs['settings'])}")

        # 5. cancellation path on a fresh job
        client.send(type="download_start", url=probe["url"], title="cancel me", formatId="best",
                    outputDir=str(target))
        queued2 = client.wait_for(
            lambda m: m.get("type") == "queued" and m.get("jobId") != job_id, 30, "second queue"
        )
        client.send(type="cancel", jobId=queued2["jobId"])
        client.wait_for(
            lambda m: m.get("type") == "job" and m.get("job", {}).get("jobId") == queued2["jobId"]
            and m["job"]["status"] in ("cancelled", "complete", "error"),
            30, "cancelled job",
        )
        final2 = [
            m for m in client.messages
            if m.get("type") == "job" and m.get("job", {}).get("jobId") == queued2["jobId"]
        ][-1]
        print(f"  cancel result: {final2['job']['status']}")

        # 6. bad input is rejected without killing the host
        client.send(type="probe", url="file:///etc/passwd")
        bad = client.wait_for(lambda m: m.get("type") == "error" and m.get("code") == "probe_failed",
                              20, "url rejection")
        print(f"  bad url: {bad['message']}")

        client.send(type="ping")
        pong = client.wait_for(lambda m: m.get("type") == "pong", 20, "pong")
        print(f"  still alive: host {pong['hostVersion']}")
        print("\nSMOKE TEST PASSED")
        return 0
    finally:
        client.close()
        test_state.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
