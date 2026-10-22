import asyncio
import base64
import json
import os
import sys
import tempfile
import unittest


class SidecarProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_is_processed_while_document_import_is_running(self):
        with tempfile.TemporaryDirectory() as root:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "vikram.desktop",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "VIKRAM_CHAT_DATA_DIR": root},
            )

            async def send(id, method, params={}):
                process.stdin.write(
                    (
                        json.dumps(
                            {"v": 1, "id": id, "method": method, "params": params}
                        )
                        + "\n"
                    ).encode()
                )
                await process.stdin.drain()

            async def read():
                line = await asyncio.wait_for(process.stdout.readline(), 10)
                self.assertTrue(line, "Sidecar failed to start")
                return json.loads(line)

            try:
                await send("new", "create_chat")
                chat = (await read())["result"]
                await send(
                    "file",
                    "import_attachment",
                    {
                        "chat_id": chat["id"],
                        "name": "notes.txt",
                        "data": base64.b64encode(b"hello document").decode(),
                    },
                )
                await send("stop", "stop")
                first = await read()
                second = await read()
                self.assertEqual(first["id"], "stop")
                self.assertEqual(second["id"], "file")
                self.assertIn("hello document", second["result"]["extracted_text"])
                process.stdin.close()
                await asyncio.wait_for(process.wait(), 10)
                self.assertEqual(process.returncode, 0)
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()
