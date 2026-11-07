import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp.client.auth.exceptions import OAuthFlowError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from vikram.mcp_client import KeychainTokenStorage, MCPManager
from vikram.settings import Settings

SERVER = r"""
import json, os, subprocess, sys, time
from pathlib import Path
root = Path(sys.argv[1])
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
(root / "child").write_text(str(child.pid))
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method, params = request["method"], request.get("params", {})
    result = {}
    if method == "initialize":
        result = {"protocolVersion": params["protocolVersion"], "capabilities": {"tools": {}, "resources": {}}, "serverInfo": {"name": "fixture", "version": "1"}}
    elif method == "tools/list":
        tool = "mutate" if params.get("cursor") else "inspect"
        result = {"tools": [{"name": tool, "description": tool + " workspace", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}, "annotations": {"readOnlyHint": True}}]}
        if not params.get("cursor"):
            result["nextCursor"] = "next"
    elif method == "tools/call":
        name, args = params["name"], params["arguments"]
        if name == "mutate":
            counter = root / "count"
            counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else "1")
            if args["text"] == "slow":
                time.sleep(60)
        result = {"content": [{"type": "text", "text": args["text"] + str(os.environ.get("OPENROUTER_API_KEY"))}], "isError": False}
    elif method == "resources/list":
        result = {"resources": [{"uri": "fixture://file", "name": "file", "mimeType": "text/plain"}]}
    elif method == "resources/templates/list":
        result = {"resourceTemplates": [{"uriTemplate": "fixture://{name}", "name": "named"}]}
    elif method == "resources/read":
        result = {"contents": [{"uri": params["uri"], "mimeType": "text/plain", "text": "resource text"}]}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""


class MemoryKeychain:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


class Person:
    def __init__(self, allow=False):
        self.allow, self.requests = allow, []

    async def approve(self, request):
        self.requests.append(request)
        return self.allow


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "server.py"
        self.script.write_text(SERVER)
        self.settings = Settings(self.root / "settings")
        self.settings.set(
            "mcp_servers",
            {
                "fixture": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(self.script), str(self.root)],
                    "read_only": ["inspect"],
                }
            },
        )
        self.person = Person()
        self.manager = MCPManager(self.settings, interaction=self.person)
        self.addAsyncCleanup(self.manager.close)

    async def test_real_stdio_lifecycle_pagination_exposure_and_resources(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "never-in-child"}):
            await self.manager.start()
        tools = await self.manager.list_tools()
        self.assertEqual(
            [t.name for t in tools], ["mcp__fixture__inspect", "mcp__fixture__mutate"]
        )
        self.assertEqual(await self.manager.list_tools(exposed_only=True), [])
        exposed = await self.manager.search_tools("inspect")
        self.assertEqual(exposed[0].name, "mcp__fixture__inspect")
        result = await self.manager.call_tool("mcp__fixture__inspect", {"text": "hi"})
        self.assertTrue(result.ok)
        self.assertEqual(result.output, "hiNone")
        self.assertEqual(self.person.requests, [])
        self.assertEqual(len(await self.manager.list_resources()), 1)
        self.assertEqual(len(await self.manager.list_resource_templates()), 1)
        self.person.allow = True
        resource = await self.manager.read_resource("fixture", "fixture://file")
        self.assertIn("resource text", resource.output)
        self.assertIn("fixture://file", self.person.requests[-1].details)
        pid = int((self.root / "child").read_text())
        await self.manager.close()
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("Server shutdown left its wrapper child running")

    async def test_annotations_cannot_bypass_approval_plan_or_argument_validation(self):
        await self.manager.start()
        denied = await self.manager.call_tool("mcp__fixture__mutate", {"text": "a"})
        self.assertFalse(denied.ok)
        self.assertFalse((self.root / "count").exists())
        self.assertEqual(self.person.requests[0].kind, "mcp")
        self.person.allow = True
        self.manager.mode = "plan"
        denied = await self.manager.call_tool("mcp__fixture__mutate", {"text": "a"})
        self.assertFalse(denied.ok)
        allowed = await self.manager.call_tool("mcp__fixture__inspect", {"text": "a"})
        self.assertTrue(allowed.ok)
        invalid = await self.manager.call_tool("mcp__fixture__inspect", {"text": 3})
        self.assertFalse(invalid.ok)

    async def test_cancelled_mutation_is_never_replayed_after_reconnect(self):
        self.person.allow = True
        await self.manager.start()
        task = asyncio.create_task(
            self.manager.call_tool(
                "mcp__fixture__mutate", {"text": "slow"}, call_id="once"
            )
        )
        for _ in range(100):
            if (self.root / "count").exists():
                break
            await asyncio.sleep(0.01)
        self.assertTrue((self.root / "count").exists())
        with self.assertRaises(RuntimeError):
            self.manager.begin_reply()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.manager.reconnect("fixture")
        replay = await self.manager.call_tool(
            "mcp__fixture__mutate", {"text": "slow"}, call_id="once"
        )
        self.assertFalse(replay.ok)
        self.assertEqual((self.root / "count").read_text(), "1")

    async def test_disabled_servers_stay_disabled_and_status_is_safe(self):
        await self.manager.start()
        await self.manager.enable("fixture", False)
        await self.manager.start()
        status = await self.manager.status()
        self.assertEqual(status[0]["state"], "disabled")
        self.assertNotIn(str(self.script), json.dumps(status))
        await self.manager.enable("fixture", True)
        self.assertTrue(await self.manager.list_tools())
        self.assertEqual((await self.manager.status())[0]["state"], "connected")

    async def test_oversized_protocol_line_fails_without_logging_server_content(self):
        self.script.write_text(
            "import sys,time\nprint('private-' + 'x' * (1024*1024),flush=True)\ntime.sleep(60)\n"
        )
        with self.assertNoLogs(level="WARNING"):
            await asyncio.wait_for(self.manager.start(), timeout=3)
        status = await self.manager.status()
        self.assertEqual(status[0]["state"], "failed")
        self.assertNotIn("private-", json.dumps(status))

    async def test_different_arguments_cannot_reuse_call_identifier(self):
        await self.manager.start()
        first = await self.manager.call_tool(
            "mcp__fixture__inspect", {"text": "a"}, call_id="one"
        )
        self.assertTrue(first.ok)
        second = await self.manager.call_tool(
            "mcp__fixture__inspect", {"text": "b"}, call_id="one"
        )
        self.assertFalse(second.ok)
        self.assertIn("call ID", second.error)

    async def test_per_call_approval_and_mode_do_not_race_and_reply_budgets_reset(self):
        await self.manager.start()
        allowed, declined = Person(True), Person(False)
        results = await asyncio.gather(
            self.manager.call_tool(
                "mcp__fixture__mutate", {"text": "allowed"}, interaction=allowed
            ),
            self.manager.call_tool(
                "mcp__fixture__mutate", {"text": "denied"}, interaction=declined
            ),
            self.manager.call_tool(
                "mcp__fixture__mutate",
                {"text": "plan"},
                interaction=allowed,
                mode="plan",
            ),
        )
        self.assertEqual([r.ok for r in results], [True, False, False])
        self.assertEqual(len(allowed.requests), 1)
        self.assertEqual(len(declined.requests), 1)
        self.assertEqual((self.root / "count").read_text(), "1")
        self.manager.max_calls = 1
        self.manager.begin_reply()
        self.assertTrue(
            (await self.manager.call_tool("mcp__fixture__inspect", {"text": "new"})).ok
        )
        self.assertFalse(
            (
                await self.manager.call_tool("mcp__fixture__inspect", {"text": "spent"})
            ).ok
        )
        self.manager.begin_reply()
        self.assertTrue(
            (await self.manager.call_tool("mcp__fixture__inspect", {"text": "next"})).ok
        )


class OAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_keychain_storage_is_scoped_and_oauth_refresh_uses_real_sdk(self):
        keychain = MemoryKeychain()
        storage = KeychainTokenStorage("https://example.test/mcp", keychain)
        await storage.set_tokens(
            OAuthToken(
                access_token="old-access", refresh_token="refresh", expires_in=-1
            )
        )
        await storage.set_client_info(
            OAuthClientInformationFull(
                client_id="client",
                redirect_uris=["http://127.0.0.1:8989/callback"],
                token_endpoint_auth_method="none",
                issuer="https://example.test/",
            )
        )
        self.assertEqual((await storage.get_tokens()).access_token, "old-access")
        self.assertIsNone(
            await KeychainTokenStorage("https://other.test/mcp", keychain).get_tokens()
        )
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings(Path(temporary))
            settings.set(
                "mcp_servers",
                {
                    "remote": {
                        "transport": "http",
                        "url": "https://example.test/mcp",
                        "oauth": True,
                    }
                },
            )
            manager = MCPManager(settings, keychain=keychain)
            provider = manager.oauth_provider("remote")
            requests = []

            async def respond(request):
                requests.append(request)
                if request.url.path.endswith("oauth-authorization-server"):
                    return httpx.Response(
                        200,
                        json={
                            "issuer": "https://example.test",
                            "authorization_endpoint": "https://example.test/authorize",
                            "token_endpoint": "https://example.test/token",
                            "response_types_supported": ["code"],
                        },
                    )
                if request.url.path == "/token":
                    return httpx.Response(
                        200,
                        json={
                            "access_token": "fresh-access",
                            "token_type": "Bearer",
                            "expires_in": 3600,
                        },
                    )
                if request.headers.get("authorization") == "Bearer fresh-access":
                    return httpx.Response(200, json={"ok": True})
                return httpx.Response(401)

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(respond), auth=provider
            ) as client:
                response = await client.get("https://example.test/mcp")
            self.assertEqual(response.status_code, 200)
            self.assertEqual((await storage.get_tokens()).access_token, "fresh-access")
            token_request = next(r for r in requests if r.url.path == "/token")
            self.assertEqual(
                parse_qs(token_request.content.decode())["grant_type"],
                ["refresh_token"],
            )

    async def test_authorization_code_flow_uses_pkce_state_and_keychain(self):
        keychain, redirects = MemoryKeychain(), []

        async def redirect(url):
            redirects.append(url)

        async def callback():
            state = parse_qs(urlsplit(redirects[-1]).query)["state"][0]
            return "authorization-code", state

        async def respond(request):
            if request.url.path.startswith("/.well-known/oauth-protected-resource"):
                return httpx.Response(
                    200,
                    json={
                        "resource": "https://example.test/mcp",
                        "authorization_servers": ["https://login.test"],
                    },
                )
            if request.url.path == "/.well-known/oauth-authorization-server":
                return httpx.Response(
                    200,
                    json={
                        "issuer": "https://login.test",
                        "authorization_endpoint": "https://login.test/authorize",
                        "token_endpoint": "https://login.test/token",
                        "registration_endpoint": "https://login.test/register",
                        "response_types_supported": ["code"],
                        "code_challenge_methods_supported": ["S256"],
                    },
                )
            if request.url.path == "/register":
                registered = json.loads(request.content)
                registered["client_id"] = "new-client"
                return httpx.Response(201, json=registered)
            if request.url.path == "/token":
                body = parse_qs(request.content.decode())
                self.assertEqual(body["code"], ["authorization-code"])
                self.assertGreaterEqual(len(body["code_verifier"][0]), 43)
                return httpx.Response(
                    200,
                    json={
                        "access_token": "new-token",
                        "token_type": "Bearer",
                        "refresh_token": "new-refresh",
                        "expires_in": 3600,
                    },
                )
            if request.headers.get("authorization") == "Bearer new-token":
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(401)

        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings(Path(temporary))
            settings.set(
                "mcp_servers",
                {
                    "remote": {
                        "transport": "http",
                        "url": "https://example.test/mcp",
                        "oauth": True,
                    }
                },
            )
            manager = MCPManager(settings, keychain=keychain)
            provider = manager.oauth_provider(
                "remote", redirect_handler=redirect, callback_handler=callback
            )
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(respond), auth=provider
            ) as client:
                response = await client.get("https://example.test/mcp")
            self.assertEqual(response.status_code, 200)
            authorization = parse_qs(urlsplit(redirects[0]).query)
            self.assertEqual(authorization["code_challenge_method"], ["S256"])
            self.assertNotIn("new-token", redirects[0])
            storage = KeychainTokenStorage("https://example.test/mcp", keychain)
            self.assertEqual((await storage.get_tokens()).access_token, "new-token")
            self.assertEqual(
                str((await storage.get_metadata()).token_endpoint),
                "https://login.test/token",
            )
            await storage.clear()

            async def wrong_callback():
                return "authorization-code", "incorrect-state"

            provider = manager.oauth_provider(
                "remote", redirect_handler=redirect, callback_handler=wrong_callback
            )
            with self.assertNoLogs(level="WARNING"):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(respond), auth=provider
                ) as client:
                    with self.assertRaisesRegex(
                        OAuthFlowError, "State parameter mismatch"
                    ):
                        await client.get("https://example.test/mcp")
            self.assertIsNone(await storage.get_tokens())


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_streamable_http_discovery_calls_and_termination(self):
        requests = []

        async def respond(request):
            requests.append(request)
            if request.method == "DELETE":
                return httpx.Response(200)
            if request.method == "GET":
                return httpx.Response(405)
            message = json.loads(request.content)
            if "id" not in message:
                return httpx.Response(202)
            method = message["method"]
            if method == "initialize":
                result = {
                    "protocolVersion": message["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "http-fixture", "version": "1"},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "echo",
                            "inputSchema": {"type": "object"},
                            "description": "Echo text",
                        }
                    ]
                }
            elif method == "tools/call":
                result = {"content": [{"type": "text", "text": "x" * 20000}]}
            else:
                result = {}
            return httpx.Response(
                200,
                headers={"mcp-session-id": "fixture-session"},
                json={"jsonrpc": "2.0", "id": message["id"], "result": result},
            )

        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings(Path(temporary))
            settings.set(
                "mcp_servers",
                {
                    "remote": {
                        "transport": "http",
                        "url": "https://example.test/mcp",
                        "read_only": ["echo"],
                    }
                },
            )
            manager = MCPManager(
                settings,
                http_transport_factory=lambda name: httpx.MockTransport(respond),
                max_calls=1,
            )
            self.addAsyncCleanup(manager.close)
            await manager.start()
            self.assertEqual((await manager.status())[0]["state"], "connected")
            result = await manager.call_tool("mcp__remote__echo", {})
            self.assertTrue(result.ok)
            self.assertTrue(result.truncated)
            self.assertEqual(len(result.output), 16000)
            denied = await manager.call_tool("mcp__remote__echo", {})
            self.assertFalse(denied.ok)
            self.assertIn("budget", denied.error)
            await manager.close()
            self.assertTrue(any(r.method == "DELETE" for r in requests))


if __name__ == "__main__":
    unittest.main()
