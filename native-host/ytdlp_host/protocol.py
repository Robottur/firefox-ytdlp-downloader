"""Native messaging framing.

Firefox's native messaging transport is a pair of pipes carrying messages in the
following shape::

    +-----------------+------------------------+
    | 4-byte length   | UTF-8 JSON body        |
    +-----------------+------------------------+

The length is unsigned and in *native* byte order, so it must be packed with
``=`` (native byte order, standard size, no alignment) rather than ``<`` or ``>``.

Only the JSON payloads described in the spec travel over this module, so the
practical size of a message is a few kilobytes. We still enforce a hard cap so a
runaway payload can never wedge the pipe, and :class:`MessageWriter` guards
against concurrent writes from several download worker threads.
"""

from __future__ import annotations

import json
import struct
import threading
from typing import Any, BinaryIO

#: Largest message we will read from or write to the pipe.
#:
#: Firefox caps host -> extension messages at 1 MiB, so staying below that is
#: always safe. :mod:`probe` keeps probe payloads well under the limit by
#: trimming long format and playlist-entry lists.
MAX_MESSAGE_BYTES = 1_000_000

_HEADER = struct.Struct("=I")


class ProtocolError(Exception):
    """Raised when the byte stream is not a valid native messaging stream."""


def _read_exactly(stream: BinaryIO, size: int) -> bytes | None:
    """Read exactly ``size`` bytes, tolerating short reads.

    Returns ``None`` on a clean end of stream (extension quit / host shut down).
    """
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            if not chunks:
                return None
            raise ProtocolError(
                f"unexpected end of stream: wanted {size} bytes, got {size - remaining}"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    """Read one message from ``stream``.

    Returns the decoded object, or ``None`` when the peer closed the pipe.
    """
    header = _read_exactly(stream, _HEADER.size)
    if header is None:
        return None
    (length,) = _HEADER.unpack(header)
    if length == 0:
        raise ProtocolError("received a zero-length message")
    if length > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"message too large: {length} bytes")
    body = _read_exactly(stream, length)
    if body is None:
        raise ProtocolError("unexpected end of stream before message body")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"malformed message body: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("message body must be a JSON object")
    return payload


def encode_message(payload: dict[str, Any]) -> bytes:
    """Serialise ``payload`` into a single length-prefixed frame."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_MESSAGE_BYTES:
        raise ProtocolError(f"message too large to send: {len(body)} bytes")
    return _HEADER.pack(len(body)) + body


class MessageWriter:
    """Thread-safe writer for the host -> extension direction.

    yt-dlp calls progress hooks from inside its own downloader threads, so
    several threads may finish a write at the same moment. Frames must never be
    interleaved, hence the lock.
    """

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def send(self, payload: dict[str, Any]) -> None:
        frame = encode_message(payload)
        with self._lock:
            self._stream.write(frame)
            self._stream.flush()
