"""Exercise real pipes, detached service, downloads, ffmpeg, and reconnects.

All media is generated locally. No personal browser profile or host database is
used. Run from the repository root: python3 tools/service_integration.py
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "native-host"))
from ytdlp_host.protocol import encode_message, read_message


class Client:
    def __init__(self, env, executable=None):
        cmd = [str(Path(executable).resolve())] if executable else [sys.executable, "-m", "ytdlp_host.bootstrap"]
        self.proc = subprocess.Popen(cmd, cwd=Path(executable).resolve().parent if executable else ROOT / "native-host", env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.messages = queue.Queue()
        self.counter = 0
        threading.Thread(target=self.read, daemon=True).start()
        self.ready = self.wait(lambda m: m.get("type") in ("host_ready", "host_error"))
        assert self.ready["type"] == "host_ready", self.ready

    def read(self):
        try:
            while (message := read_message(self.proc.stdout)) is not None:
                self.messages.put(message)
        except Exception as exc:
            self.messages.put({"type": "reader_error", "message": str(exc)})

    def send(self, kind, **fields):
        self.counter += 1
        request_id = str(self.counter)
        self.proc.stdin.write(encode_message({"type": kind, "requestId": request_id, **fields}))
        self.proc.stdin.flush()
        return request_id

    def wait(self, predicate, timeout=40):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f"Host exited: {self.proc.stderr.read().decode(errors='replace')}")
            try:
                message = self.messages.get(timeout=0.1)
            except queue.Empty:
                continue
            if predicate(message): return message
        raise AssertionError("Timed out waiting for host message")

    def request(self, kind, **fields):
        request_id = self.send(kind, **fields)
        reply = self.wait(lambda m: m.get("requestId") == request_id and m.get("final") is not False)
        assert reply["type"] != "error", reply
        return reply

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        self.proc.stdout.close(); self.proc.stderr.close()


class SlowMedia(SimpleHTTPRequestHandler):
    def log_message(self, *_): pass

    def copyfile(self, source, outputfile):
        try:
            while chunk := source.read(16384):
                outputfile.write(chunk); outputfile.flush()
                time.sleep(0.02)
        except (BrokenPipeError, ConnectionResetError): pass


def explain_failure(url, spec, folder):
    """The host reports only a safe category; repeat the download in-process to print the raw cause."""
    import traceback
    try:
        from ytdlp_host import probe
        opts = probe.download_options(spec, ffmpeg_found=True)
        opts.update(outtmpl=str(folder / "%(title)s.%(ext)s"), logger=None, quiet=False, no_warnings=False, verbose=True)
        with probe.youtube_dl(opts) as ydl:
            ydl.extract_info(url, download=True)
        print("EXPLAIN: the in-process repeat succeeded", flush=True)
    except Exception:
        traceback.print_exc()


def main():
    with tempfile.TemporaryDirectory(prefix="ytdlp-service-test-") as tmp:
        folder = Path(tmp)
        media = folder / "media"; media.mkdir()
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=4",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-shortest", str(media / "sample.mp4")], check=True)
        shutil.copyfile(media / "sample.mp4", media / "second.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(media / "sample.mp4"),
            "-c", "copy", str(media / "fallback.mkv")], check=True)
        (media / "captions.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello from a subtitle.\n", encoding="utf-8")
        (media / "subtitles.html").write_text('<title>Caption test</title><video src="sample.mp4">'
            '<track kind="subtitles" src="captions.vtt" srclang="en"></video>', encoding="utf-8")
        (media / "playlist.html").write_text('<title>Two clips</title><video src="sample.mp4"></video>'
            '<video src="second.mp4"></video>', encoding="utf-8")
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(media / "sample.mp4"),
            "-map", "0", "-c", "copy", "-f", "dash", str(media / "streams.mpd")], check=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SlowMedia, directory=media))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/sample.mp4"
        env = {**os.environ, "YTDLP_HOST_STATE_DIR": str(folder / "state"), "YTDLP_HOST_IDLE_SECONDS": "2"}
        executable = sys.argv[1] if len(sys.argv) > 1 else None
        client = None
        try:
            client = Client(env, executable)
            initial_pid = json.loads((folder / "state/service.json").read_text())["pid"]
            client.request("update_settings", settings={"defaultDownloadDir": str(folder / "downloads"),
                                                       "maxConcurrentDownloads": 1, "notifyOnComplete": False})
            # Extraction and cache use real yt-dlp over HTTP.
            start = time.monotonic()
            info = client.request("probe", url=url)
            cold_seconds = time.monotonic() - start
            assert info["formats"]
            start = time.monotonic(); client.request("probe", url=url)
            cached_seconds = time.monotonic() - start
            assert cached_seconds < 0.5, "Cached probe should be immediate"
            print(f"PASS: real metadata extraction ({cold_seconds:.3f}s) and cached lookup ({cached_seconds:.3f}s)", flush=True)
            queued = client.request("download_start", url=url, formatId="best")
            job_id = queued["jobId"]
            client.wait(lambda m: m.get("type") == "progress" and m.get("jobId") == job_id and m.get("downloadedBytes", 0) > 0)
            client.close(); client = None
            time.sleep(0.3)
            reconnect_start = time.monotonic()
            client = Client(env, executable)
            print(f"MEASURE: bridge reconnect {time.monotonic() - reconnect_start:.3f}s", flush=True)
            assert json.loads((folder / "state/service.json").read_text())["pid"] == initial_pid
            assert client.ready["settings"]["maxConcurrentDownloads"] == 1
            deadline = time.monotonic() + 30
            while True:
                rows = client.request("list_jobs")["jobs"]
                job = next(j for j in rows if j["jobId"] == job_id)
                if job["status"] in ("complete", "error", "cancelled"): break
                assert time.monotonic() < deadline
                time.sleep(0.1)
            assert job["status"] == "complete", job
            assert Path(job["filename"]).stat().st_size == (media / "sample.mp4").stat().st_size
            print("PASS: download survives bridge exit and reconnect; file is intact", flush=True)
            queued = client.request("download_start", url=url, formatId="best", audioOnly=True,
                                    outputTemplate="audio.%(ext)s")
            result = client.wait(lambda m: m.get("jobId") == queued["jobId"] and m.get("type") in ("complete", "error"))
            assert result["type"] == "complete", result
            assert result["filepath"].endswith(".mp3"), result
            assert Path(result["filepath"]).stat().st_size > 1000
            print("PASS: real MP3 conversion and final output path", flush=True)
            queued = client.request("download_start", url=url, formatId="best", outputTemplate="cancelled.%(ext)s")
            cancel_start = time.monotonic()
            client.request("cancel", jobId=queued["jobId"])
            deadline = time.monotonic() + 20
            while True:
                job = next(j for j in client.request("list_jobs")["jobs"] if j["jobId"] == queued["jobId"])
                if job["status"] == "cancelled": break
                assert job["status"] not in ("error", "complete"), job
                assert time.monotonic() < deadline
                time.sleep(0.1)
            assert not (folder / "downloads/cancelled.mp4").exists()
            print(f"PASS: cancellation prevents a finished output file ({time.monotonic() - cancel_start:.3f}s)", flush=True)
            caption_url = url.replace("sample.mp4", "subtitles.html")
            assert client.request("probe", url=caption_url)["subtitles"][0]["lang"] == "en"
            queued = client.request("download_start", url=caption_url, formatId="best",
                                    subtitles=[{"lang": "en", "auto": False}], outputTemplate="captioned.%(ext)s")
            result = client.wait(lambda m: m.get("jobId") == queued["jobId"] and m.get("type") in ("complete", "error"))
            assert result["type"] == "complete", result
            assert "Hello from a subtitle" in (folder / "downloads/captioned.en.srt").read_text()
            print("PASS: subtitle discovery, download and SRT conversion", flush=True)
            dash_url = url.replace("sample.mp4", "streams.mpd")
            queued = client.request("download_start", url=dash_url, formatId="mp4", outputTemplate="merged.%(ext)s")
            result = client.wait(lambda m: m.get("jobId") == queued["jobId"] and m.get("type") in ("complete", "error"))
            if result["type"] != "complete":
                explain_failure(dash_url, {"formatId": "mp4"}, folder / "explain")
            assert result["type"] == "complete", result
            streams = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", result["filepath"]]))
            assert {s["codec_type"] for s in streams["streams"]} == {"audio", "video"}
            assert Path(result["filepath"]).suffix == ".mp4"
            print("PASS: default MP4 merges separate DASH video and audio", flush=True)
            queued = client.request("download_start", url=url.replace("sample.mp4", "fallback.mkv"),
                                    outputTemplate="remuxed.%(ext)s")
            result = client.wait(lambda m: m.get("jobId") == queued["jobId"] and m.get("type") in ("complete", "error"))
            assert result["type"] == "complete", result
            assert Path(result["filepath"]).suffix == ".mp4"
            output = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", result["filepath"]]))
            assert "mp4" in output["format"]["format_name"]
            assert {s["codec_type"] for s in output["streams"]} == {"audio", "video"}
            print("PASS: non-MP4 fallback remuxes to a real MP4 with audio and video", flush=True)
            playlist_url = url.replace("sample.mp4", "playlist.html")
            playlist = client.request("probe", url=playlist_url)
            assert playlist["isPlaylist"]
            for mode, count, selected in (("all", 2, []), ("select", 1, [1])):
                queued = client.request("download_start", url=playlist_url, formatId="best",
                    playlistMode=mode, entryIndexes=selected)
                result = client.wait(lambda m: m.get("jobId") == queued["jobId"] and m.get("type") in ("complete", "error"))
                assert result["type"] == "complete", result
                rows = client.request("list_jobs")["jobs"]
                children = [j for j in rows if j["groupId"] == queued["jobId"]]
                assert len(children) == count and all(j["status"] == "complete" for j in children), children
                if selected: assert children[0]["entryIndex"] == 1
            print("PASS: whole playlist and selected entries produce the expected child jobs", flush=True)
            renamed = folder / "downloads/My 100% renamed clip.mp4"
            renamed.write_bytes(b"previous file explicitly selected for replacement")
            queued = client.request("download_start", url=url, formatId="mp4",
                                    outputFilename=renamed.name, overwrite=True)
            result = client.wait(lambda m: m.get("jobId") == queued["jobId"] and m.get("type") in ("complete", "error"))
            assert result["type"] == "complete", result
            assert Path(result["filepath"]) == renamed
            assert renamed.read_bytes() == (media / "sample.mp4").read_bytes()
            print("PASS: Save As filename (including literal %) and confirmed replacement are respected", flush=True)
            # Unknown and maliciously framed commands do not kill the service.
            bad_id = client.send("probe", url="file:///etc/passwd")
            bad = client.wait(lambda m: m.get("requestId") == bad_id)
            assert bad["type"] == "error"
            client.request("ping")
            print("PASS: rejected URL leaves host responsive", flush=True)
            client.request("stop_service")
            client.close(); client = None
            deadline = time.monotonic() + 10
            while (folder / "state/service.json").exists() and time.monotonic() < deadline: time.sleep(0.1)
            client = Client(env, executable)
            assert client.ready["settings"]["notifyOnComplete"] is False
            assert client.request("list_jobs")["jobs"] == [], "Finished history must not survive restart by default"
            assert client.request("history_policy")["retainFinishedHistory"] is False
            print("PASS: default finished history is absent after restart", flush=True)
            client.request("configure_history", enabled=True, existing="keep")
            queued = client.request("download_start", url=url, outputTemplate="retained.%(ext)s")
            result = client.wait(lambda m: m.get("jobId") == queued["jobId"] and m.get("type") in ("complete", "error"))
            assert result["type"] == "complete", result
            client.request("diagnostics_start")
            client.request("diagnostics_stop")
            preview = client.request("diagnostics_preview")
            assert "previewId" in preview and url not in preview["text"]
            assert "downloads" not in preview["text"]
            client.request("diagnostics_delete")
            client.request("stop_service")
            client.close(); client = None
            deadline = time.monotonic() + 10
            while (folder / "state/service.json").exists() and time.monotonic() < deadline: time.sleep(0.1)
            client = Client(env, executable)
            assert client.request("history_policy")["retainFinishedHistory"] is True
            assert len(client.request("list_jobs")["jobs"]) == 1
            client.request("clear_finished")
            assert client.request("list_jobs")["jobs"] == []
            assert list((folder / "downloads").glob("*.mp3")), "Clearing history must keep downloads"
            print("PASS: opt-in history/settings survive restart; clearing keeps files; diagnostics omit sources", flush=True)
        finally:
            if client:
                try: client.request("stop_service")
                except Exception: pass
                client.close()
            server.shutdown(); server.server_close()
            time.sleep(2.2)


if __name__ == "__main__": main()
