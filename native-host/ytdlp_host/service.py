"""Detached download service and a disposable Firefox stdio bridge.

Firefox owns (and kills) native messaging processes. Only the tiny bridge is
its child; the queue runs in a detached process. Local TCP carries the same
bounded JSON framing, authenticated with a random secret in the user's private
state directory. There is no HTTP server or unauthenticated command endpoint.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid

from . import paths
from .protocol import MessageWriter, encode_message, read_message


def command(*args):
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-X", "utf8", "-m", "ytdlp_host.bootstrap", *args]


class ServiceLock:
    """OS releases the lock on crash; a stale PID cannot block startup."""
    def __enter__(self):
        self.file = open(paths.state_dir() / "service.lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                self.file.write(b"0")
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise
        return self

    def __exit__(self, *_):
        self.file.close()


def connect():
    endpoint = json.loads((paths.state_dir() / "service.json").read_text(encoding="utf-8"))
    sock = socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=5)
    stream = sock.makefile("rb")
    try:
        sock.sendall(encode_message({"type": "authenticate", "token": endpoint["token"]}))
        ready = read_message(stream)
        if not ready or ready.get("type") != "host_ready":
            raise OSError("Download service did not authenticate.")
        sock.settimeout(None)
        return sock, stream, ready
    except Exception:
        stream.close()
        sock.close()
        raise


def launch():
    options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        # Escape Firefox's Windows Job object, which is killed on browser exit.
        options["creationflags"] = (subprocess.CREATE_BREAKAWAY_FROM_JOB |
                                    subprocess.DETACHED_PROCESS |
                                    subprocess.CREATE_NEW_PROCESS_GROUP)
        # PyInstaller onefile must unpack its own service copy, independent of
        # the bridge's temporary directory and lifetime.
        options["env"] = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
        try:
            subprocess.Popen(command("--service"), **options)
            return
        except PermissionError:
            # A job object without BREAKAWAY_OK (e.g. CI runners) refuses breakaway.
            # shortcut: the service then ends with that job; downloads stop if its owner exits.
            options["creationflags"] &= ~subprocess.CREATE_BREAKAWAY_FROM_JOB
    else:
        options["start_new_session"] = True
    subprocess.Popen(command("--service"), **options)


def bridge(stdin, stdout):
    try:
        connection = connect()
    except (OSError, ValueError, KeyError):
        launch()
        last_launch = time.monotonic()
        deadline = time.monotonic() + 30
        while True:
            try:
                connection = connect()
                break
            except (OSError, ValueError, KeyError):
                if time.monotonic() >= deadline:
                    raise OSError("The download service did not start. Re-run the platform installer and check host.log.")
                if time.monotonic() - last_launch > 2:
                    launch()
                    last_launch = time.monotonic()
                time.sleep(0.1)
    sock, stream, ready = connection
    writer = MessageWriter(stdout)
    writer.send(ready)

    def forward_requests():
        try:
            while (message := read_message(stdin)) is not None:
                sock.sendall(encode_message(message))
        except Exception:
            pass
        finally:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    threading.Thread(target=forward_requests, daemon=True).start()
    try:
        while (message := read_message(stream)) is not None:
            writer.send(message)
    except (OSError, ValueError):
        pass
    finally:
        stream.close()
        sock.close()
    return 0


class Client:
    def __init__(self, sock):
        self.sock = sock
        self.outgoing = queue.Queue(maxsize=512)
        self.write_lock = threading.Lock()
        self.closed = threading.Event()
        threading.Thread(target=self.write, daemon=True).start()

    def send(self, message):
        try:
            self.outgoing.put_nowait(encode_message(message))
        except queue.Full:
            # Reconnecting clients receive a complete snapshot. A slow or dead
            # Firefox must never block a download's progress hook.
            self.close()

    def write(self):
        try:
            while not self.closed.is_set():
                try:
                    frame = self.outgoing.get(timeout=1)
                except queue.Empty:
                    continue
                with self.write_lock:
                    self.sock.sendall(frame)
        except OSError:
            self.close()

    def close(self):
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def serve():
    try:
        lock = ServiceLock()
        lock.__enter__()
    except OSError:
        return 0  # another Firefox profile/bridge already started the service
    try:
        return _serve_locked()
    finally:
        lock.__exit__()


def _serve_locked():
    from .main import Host

    clients = {}
    clients_lock = threading.RLock()
    dispatch_lock = threading.Lock()
    stop = threading.Event()
    secret = secrets.token_hex(32)

    def send(message):
        request_id = message.get("requestId") or ""
        with clients_lock:
            if request_id:
                client_id, _, original_id = request_id.partition(":")
                client = clients.get(client_id)
                if client:
                    client.send({**message, "requestId": original_id})
            else:
                for client in list(clients.values()):
                    client.send(message)

    host = Host(send=send)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(1)
    endpoint = paths.state_dir() / "service.json"
    temporary = endpoint.with_suffix(".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"port": listener.getsockname()[1], "token": secret, "pid": os.getpid()}, f)
    os.replace(temporary, endpoint)

    def handle(sock):
        client_id = uuid.uuid4().hex
        client = None
        sock.settimeout(5)
        stream = sock.makefile("rb")
        try:
            auth = read_message(stream)
            token = auth.get("token") if auth else None
            if not isinstance(token, str) or not secrets.compare_digest(token, secret):
                return
            sock.settimeout(None)
            client = Client(sock)
            with clients_lock:
                client.send(host.ready_payload())
                clients[client_id] = client
            while not stop.is_set() and (message := read_message(stream)) is not None:
                request_id = message.get("requestId") or ""
                if not isinstance(request_id, str) or len(request_id) > 100:
                    break
                if message.get("type") in ("shutdown", "stop_service"):
                    if host._manager.has_active_jobs():
                        client.send({"type": "error", "requestId": request_id,
                                     "message": "Downloads are active. Cancel or finish them before stopping the host.",
                                     "code": "busy"})
                    else:
                        # Synchronous acknowledgement before closing clients.
                        with client.write_lock:
                            sock.sendall(encode_message({"type": "bye", "requestId": request_id}))
                        stop.set()
                    continue
                message["requestId"] = f"{client_id}:{request_id}"
                with dispatch_lock:
                    host.handle(message)
        except Exception as exc:
            paths.write_log(f"service client disconnected: {type(exc).__name__}")
        finally:
            with clients_lock:
                clients.pop(client_id, None)
            if client:
                client.close()
            stream.close()
            sock.close()

    idle_since = time.monotonic()
    idle_timeout = float(os.environ.get("YTDLP_HOST_IDLE_SECONDS", "300"))
    try:
        while not stop.is_set():
            try:
                sock, _ = listener.accept()
                threading.Thread(target=handle, args=(sock,), daemon=True).start()
                idle_since = time.monotonic()
            except socket.timeout:
                pass
            with clients_lock:
                busy = bool(clients)
            if busy or host._manager.has_active_jobs():
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= idle_timeout:
                break
    finally:
        listener.close()
        with clients_lock:
            for client in clients.values():
                client.close()
        host.stop()
        endpoint.unlink(missing_ok=True)
    return 0


def stop_service():
    try:
        sock, stream, _ = connect()
    except (OSError, ValueError, KeyError):
        return 0
    try:
        sock.settimeout(10)
        sock.sendall(encode_message({"type": "stop_service", "requestId": "stop"}))
        result = read_message(stream)
        if result and result.get("type") == "bye":
            return 0
        print((result or {}).get("message", "Could not stop the host."), file=sys.stderr)
        return 1
    finally:
        stream.close()
        sock.close()
