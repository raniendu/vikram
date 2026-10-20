import asyncio
import io
import json
import struct
import unittest
import zlib
from unittest.mock import patch

import httpx
from test_desktop_store import png

from vikram.desktop.attachments import validate_image
from vikram.desktop.inference import ObservedStream, stream_reply
from vikram.desktop.protocol import validate_command
from vikram.ollama import ThinkingControls, thinking_request


class ProtocolTests(unittest.TestCase):
    def test_rejects_unknown_commands_and_paths(self):
        for value in (
            {"v": 2, "id": "r", "method": "hello"},
            {"v": 1, "id": "r", "method": "execute"},
            {
                "v": 1,
                "id": "r",
                "method": "image",
                "params": {"chat_id": "../outside", "attachment_id": "file"},
            },
            {
                "v": 1,
                "id": "r",
                "method": "import_image",
                "params": {"chat_id": "a" * 32, "path": "/etc/passwd"},
            },
            {
                "v": 1,
                "id": "r",
                "method": "update_chat",
                "params": {"chat_id": "a" * 32, "fields": {"draft": "overwrite"}},
            },
            {
                "v": 1,
                "id": "r",
                "method": "update_chat",
                "params": {"chat_id": "a" * 32, "fields": {"web": "off"}},
            },
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_command(json.dumps(value))

    def test_thinking_preserves_booleans_and_named_levels(self):
        self.assertEqual(
            ThinkingControls.model_validate({"values": [False, True]}).values,
            [False, True],
        )
        self.assertEqual(thinking_request(False), {"reasoning_effort": "none"})
        self.assertEqual(thinking_request("low"), {"reasoning_effort": "low"})
        self.assertEqual(thinking_request(None), {})
        with self.assertRaises(ValueError):
            ThinkingControls.model_validate({"values": [1]})

    def test_pixel_limit_before_decoding(self):
        data = bytearray(png())
        data[16:24] = struct.pack(">II", 6000, 4001)
        data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]))
        with self.assertRaisesRegex(ValueError, "24 megapixels"):
            validate_image(bytes(data))
        with self.assertRaisesRegex(ValueError, "10 MiB"):
            validate_image(b"x" * (10 * 1024 * 1024 + 1))


class ProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_transport_is_not_automatically_retried(self):
        calls = []

        async def respond(client, request, **kwargs):
            calls.append(request)
            return httpx.Response(
                500, request=request, json={"error": {"message": "unavailable"}}
            )

        with patch.object(
            httpx.AsyncClient, "send", autospec=True, side_effect=respond
        ):
            with self.assertRaises(Exception):
                async for _ in stream_reply("local", None, [], ["hello"]):
                    pass
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].url.host, "localhost")

    async def test_records_only_explicit_reported_model_across_chunks(self):
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"model": "actual", "choices": []}\n'
                yield b'data: {"choices": [{"delta": {"reasoning": "never persisted"}}]}\n'
                yield b"data: [DONE]\n"

        reported = {}
        stream = ObservedStream(Stream(), reported)
        async for _ in stream:
            pass
        self.assertEqual(reported, {"model": "actual"})
        await stream.aclose()
