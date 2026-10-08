"""Framing round-trips and failure modes of the native messaging transport."""

from __future__ import annotations

import io
import json
import struct
import unittest

from ytdlp_host import protocol


def frame(payload: dict) -> bytes:
    return protocol.encode_message(payload)


class EncodeTests(unittest.TestCase):
    def test_header_is_native_endian_and_four_bytes(self):
        data = frame({"type": "ping"})
        length = struct.unpack("=I", data[:4])[0]
        self.assertEqual(len(data), 4 + length)
        self.assertEqual(json.loads(data[4:]), {"type": "ping"})

    def test_unicode_survives_the_round_trip(self):
        payload = {"type": "probe_result", "title": "Björk — Jóga 日本語"}
        stream = io.BytesIO(frame(payload))
        self.assertEqual(protocol.read_message(stream), payload)

    def test_multiple_messages_in_one_stream(self):
        stream = io.BytesIO(frame({"n": 1}) + frame({"n": 2}) + frame({"n": 3}))
        self.assertEqual(
            [protocol.read_message(stream)["n"] for _ in range(3)], [1, 2, 3]
        )

    def test_oversized_payload_is_refused(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.encode_message({"blob": "x" * protocol.MAX_MESSAGE_BYTES})


class ReadTests(unittest.TestCase):
    def test_clean_eof_returns_none(self):
        self.assertIsNone(protocol.read_message(io.BytesIO(b"")))

    def test_truncated_header_is_an_error(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.read_message(io.BytesIO(b"\x01\x02"))

    def test_truncated_body_is_an_error(self):
        stream = io.BytesIO(struct.pack("=I", 50) + b"{}")
        with self.assertRaises(protocol.ProtocolError):
            protocol.read_message(stream)

    def test_zero_length_is_rejected(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.read_message(io.BytesIO(struct.pack("=I", 0)))

    def test_non_object_body_is_rejected(self):
        body = b"[1, 2, 3]"
        stream = io.BytesIO(struct.pack("=I", len(body)) + body)
        with self.assertRaises(protocol.ProtocolError):
            protocol.read_message(stream)

    def test_invalid_json_is_rejected(self):
        body = b"{not json"
        stream = io.BytesIO(struct.pack("=I", len(body)) + body)
        with self.assertRaises(protocol.ProtocolError):
            protocol.read_message(stream)

    def test_oversized_incoming_frame_is_rejected_without_allocating(self):
        stream = io.BytesIO(struct.pack("=I", protocol.MAX_MESSAGE_BYTES + 1))
        with self.assertRaises(protocol.ProtocolError):
            protocol.read_message(stream)


class WriterTests(unittest.TestCase):
    def test_concurrent_writes_do_not_interleave_frames(self):
        """Download workers finish progress callbacks from many threads."""
        import threading

        buffer = io.BytesIO()
        writer = protocol.MessageWriter(buffer)
        errors: list[BaseException] = []

        def spam(worker: int) -> None:
            try:
                for i in range(200):
                    writer.send({"worker": worker, "i": i, "pad": "y" * 500})
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=spam, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])

        stream = io.BytesIO(buffer.getvalue())
        seen = 0
        while True:
            message = protocol.read_message(stream)
            if message is None:
                break
            self.assertEqual(set(message), {"worker", "i", "pad"})
            seen += 1
        self.assertEqual(seen, 8 * 200)


if __name__ == "__main__":
    unittest.main()
