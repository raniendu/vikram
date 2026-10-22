import asyncio
import gzip
import unittest

import httpx

from vikram import web


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_rejects_compression_before_decoding(self):
        response = httpx.Response(
            200,
            headers={"content-encoding": "gzip"},
            stream=httpx.ByteStream(gzip.compress(b"x" * (2 * web.MAX_RESPONSE))),
        )
        with self.assertRaisesRegex(ValueError, "encoding"):
            await web.bound_response(response)
        self.assertTrue(response.is_closed)

    async def test_transport_caps_uncompressed_bytes_before_json_parsing(self):
        response = httpx.Response(
            200, stream=httpx.ByteStream(b"x" * (web.MAX_RESPONSE + 1))
        )
        await web.bound_response(response)
        with self.assertRaisesRegex(ValueError, "limit"):
            await response.aread()
        await response.aclose()

    async def test_remote_results_become_bounded_sources_and_budget_stops_calls(self):
        calls = []

        async def remote(name, args):
            calls.append((name, args))
            return {
                "results": [
                    {
                        "title": "Docs",
                        "url": "https://example.org/docs",
                        "excerpts": ["text " * 9000],
                    }
                ]
            }

        session = web.WebSession("a" * 32, remote=remote)
        for _ in range(4):
            result = await session.call(
                "web_search",
                {
                    "objective": "Find docs",
                    "search_queries": ["current official documentation"],
                },
            )
        self.assertEqual(len(calls), 3)
        self.assertIn("limit", result["error"].lower())
        self.assertEqual(len(session.sources), 1)
        self.assertLessEqual(sum(len(s["excerpt"]) for s in session.sources), 20_000)
        self.assertEqual(calls[0][1]["session_id"], "a" * 32)
        self.assertNotIn("model_name", calls[0][1])

    async def test_invalid_inputs_never_reach_provider(self):
        async def remote(*args):
            self.fail("Invalid input reached remote provider")

        session = web.WebSession("b" * 32, remote=remote)
        for url in [
            "file:///etc/passwd",
            "http://127.0.0.1",
            "http://user:pw@example.org",
            "http://192.168.1.1",
            "http://printer.local",
        ]:
            result = await session.call("web_fetch", {"urls": [url]})
            self.assertIn("error", result)
        result = await session.call("shell", {"command": "pwd"})
        self.assertIn("error", result)

    async def test_failure_disables_remote_without_leaking_exception(self):
        async def remote(*args):
            raise RuntimeError("PRIVATE PROVIDER BODY")

        session = web.WebSession("c" * 32, remote=remote)
        result = await session.call(
            "web_search",
            {"objective": "Find docs", "search_queries": ["official docs"]},
        )
        self.assertIn("error", result)
        self.assertNotIn("PRIVATE", str(result))
        self.assertTrue(session.disabled)
        self.assertEqual(session.searches[0]["status"], "failed")

    async def test_cancel_propagates_and_records_stopped(self):
        async def remote(*args):
            raise asyncio.CancelledError()

        session = web.WebSession("d" * 32, remote=remote)
        with self.assertRaises(asyncio.CancelledError):
            await session.call(
                "web_search",
                {"objective": "Find docs", "search_queries": ["official docs"]},
            )
        self.assertEqual(session.searches[0]["status"], "stopped")

    async def test_fetch_updates_existing_source_with_deeper_excerpt(self):
        async def remote(name, args):
            return {
                "results": [
                    {
                        "title": "Docs",
                        "url": "https://example.org/docs",
                        "excerpts": [
                            (
                                "Detailed page content"
                                if name == "web_fetch"
                                else "Short excerpt"
                            )
                        ],
                    }
                ]
            }

        session = web.WebSession("x" * 32, remote=remote)
        await session.call(
            "web_search",
            {"objective": "Find docs", "search_queries": ["official docs"]},
        )
        await session.call("web_fetch", {"urls": ["https://example.org/docs"]})
        self.assertEqual(len(session.sources), 1)
        self.assertIn("Detailed", session.sources[0]["excerpt"])

    async def test_oversized_result_disables_search_and_keeps_no_sources(self):
        async def remote(*args):
            return {
                "results": [
                    {"url": "https://example.org", "excerpts": ["x" * (1024 * 1024)]}
                ]
            }

        session = web.WebSession("x" * 32, remote=remote)
        result = await session.call(
            "web_search",
            {"objective": "Find docs", "search_queries": ["official docs"]},
        )
        self.assertIn("error", result)
        self.assertEqual(session.sources, [])
