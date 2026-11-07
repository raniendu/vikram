import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from vikram.agent import build_agent
from vikram.models import ModelCatalog, parse_model, prepare_model

LOCAL = {
    "name": "local:1",
    "size": 1000,
    "digest": "abc",
    "details": {"format": "gguf"},
}
SHOWN = {
    "capabilities": ["completion", "tools", "thinking"],
    "details": {"format": "gguf"},
    "thinking": {"values": [False, True], "default": True},
}
CLOUD = {
    "id": "example/reasoner:free",
    "name": "Example Reasoner",
    "context_length": 64000,
    "architecture": {
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
    },
    "supported_parameters": ["tools", "reasoning"],
    "reasoning": {
        "supported_efforts": ["high", "low"],
        "default_effort": "low",
        "mandatory": False,
    },
    "top_provider": {"max_completion_tokens": 8000},
    "pricing": {"prompt": "0.000001", "completion": "0.000002"},
}


class MemoryKeychain:
    def __init__(self):
        self.values = {}

    def get(self, provider):
        return self.values.get(provider)

    def set(self, provider, value):
        self.values[provider] = value

    def delete(self, provider):
        self.values.pop(provider, None)


class ProviderTests(unittest.TestCase):
    def test_qualified_local_selection_resolves_existing_metadata(self):
        with patch("vikram.models.sandbox.unavailable_reason", return_value=None):
            selected = prepare_model(
                [parse_model(LOCAL, SHOWN)], "ollama:local:1", None
            )
        self.assertTrue(selected.thinking)
        self.assertEqual(selected.web, "auto")

    def test_model_ids_keep_local_tags_and_cloud_variants(self):
        from vikram.providers import ModelRef

        for value, provider, model_id in (
            ("local:1", "ollama", "local:1"),
            ("ollama:local:1", "ollama", "local:1"),
            ("openrouter:example/reasoner:free", "openrouter", "example/reasoner:free"),
        ):
            with self.subTest(value=value):
                ref = ModelRef.parse(value)
                self.assertEqual((ref.provider, ref.model_id), (provider, model_id))
                self.assertEqual(ref.qualified_id, f"{provider}:{model_id}")
        for invalid in ("", "ollama:", "openrouter:", "openrouter:bare", "bad\nname"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ModelRef.parse(invalid)

    def test_provider_settings_do_not_send_ollama_controls_to_cloud(self):
        from vikram.providers import model_settings

        self.assertEqual(
            model_settings("ollama:local:1", False),
            {"extra_body": {"reasoning_effort": "none"}},
        )
        for value, requested in (
            (True, {"enabled": True}),
            (False, {"enabled": False}),
            ("low", {"effort": "low"}),
        ):
            settings = model_settings("openrouter:example/reasoner", value)
            self.assertEqual(settings["openrouter_reasoning"], requested)
            self.assertFalse(settings["openrouter_provider"]["allow_fallbacks"])
            self.assertNotIn("extra_body", settings)

    def test_cloud_metadata_uses_advertised_controls_limits_and_prices(self):
        from vikram.providers import parse_openrouter_model

        model = parse_openrouter_model(CLOUD, authenticated=True)
        self.assertEqual(model["name"], "openrouter:example/reasoner:free")
        self.assertEqual(model["thinking"], [False, "high", "low"])
        self.assertEqual(model["thinking_default"], "low")
        self.assertEqual(model["context_length"], 64000)
        self.assertEqual(model["max_output_tokens"], 8000)
        self.assertEqual(model["pricing"]["prompt"], "0.000001")
        self.assertTrue(model["vision"] and model["tools"] and model["available"])
        with patch("vikram.models.sandbox.unavailable_reason", return_value=None):
            prepared = prepare_model([model], model["name"], None)
        self.assertEqual(prepared.thinking, "low")
        self.assertIn("history", prepared.notice.lower())

    def test_missing_or_mandatory_reasoning_never_invents_disabled_control(self):
        from vikram.providers import parse_openrouter_model

        mandatory = {**CLOUD, "reasoning": {**CLOUD["reasoning"], "mandatory": True}}
        model = parse_openrouter_model(mandatory, authenticated=True)
        self.assertNotIn(False, model["thinking"])
        self.assertNotIn(True, model["thinking"])
        unknown = {**CLOUD, "reasoning": None, "supported_parameters": []}
        self.assertEqual(parse_openrouter_model(unknown)["thinking"], [])
        missing = parse_openrouter_model(CLOUD)
        self.assertFalse(missing["available"])
        self.assertIn("OpenRouter", missing["reason"])

    def test_malformed_local_metadata_cannot_hide_other_models(self):
        for shown in (None, {"details": None}, {"details": []}):
            with self.subTest(shown=shown):
                self.assertFalse(parse_model(LOCAL, shown)["available"])

    def test_disabled_only_reasoning_does_not_advertise_enabled(self):
        from vikram.providers import parse_openrouter_model

        model = parse_openrouter_model(
            {**CLOUD, "reasoning": {"supported_efforts": ["none"]}},
            authenticated=True,
        )
        self.assertEqual(model["thinking"], [False])
        self.assertIsNone(model["thinking_default"])

    def test_credentials_environment_precedence_and_logout_preserve_environment(self):
        from vikram.providers import CredentialStore

        keychain = MemoryKeychain()
        store = CredentialStore(environ={}, keychain=keychain)
        store.set("openrouter", "stored-test-key")
        self.assertEqual(store.get("openrouter"), "stored-test-key")
        self.assertEqual(store.source("openrouter"), "keychain")
        environ = {"OPENROUTER_API_KEY": "environment-test-key", "PATH": "/bin"}
        override = CredentialStore(environ=environ, keychain=keychain)
        self.assertEqual(override.get("openrouter"), "environment-test-key")
        self.assertEqual(override.source("openrouter"), "environment")
        override.delete("openrouter")
        self.assertIsNone(store.get("openrouter"))
        self.assertEqual(override.get("openrouter"), "environment-test-key")
        self.assertEqual(override.child_environment(), {"PATH": "/bin"})
        self.assertNotIn("test-key", repr(override))

    def test_credential_errors_do_not_echo_values_or_backend_errors(self):
        from vikram.providers import CredentialError, CredentialStore

        store = CredentialStore(environ={}, keychain=MemoryKeychain())
        with self.assertRaises(CredentialError) as caught:
            store.set("openrouter", "secret\nvalue")
        self.assertNotIn("secret", str(caught.exception))
        with patch.object(store.keychain, "set", side_effect=OSError("private value")):
            with self.assertRaises(CredentialError) as caught:
                store.set("openrouter", "test-key")
        self.assertNotIn("private value", str(caught.exception))


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_coding_instructions_reach_each_request(self):
        from pydantic_ai.messages import ModelResponse, TextPart
        from pydantic_ai.models.function import FunctionModel

        instructions = []

        def respond(messages, info):
            instructions.append(info.instructions)
            return ModelResponse(parts=[TextPart("Ready")])

        async with httpx.AsyncClient() as client:
            agent = build_agent("ollama:local:1", http_client=client)
            await agent.run(
                "Hello",
                deps=SimpleNamespace(
                    coding=SimpleNamespace(
                        settings=SimpleNamespace(is_trusted=lambda: True)
                    ),
                    instructions="Inspect the trusted workspace.",
                ),
                model=FunctionModel(respond),
            )
            await agent.run("Hello", model=FunctionModel(respond))
        self.assertIn("Inspect the trusted workspace.", instructions[0])
        self.assertNotIn("You have no direct filesystem access.", instructions[0])
        self.assertIn("You have no direct filesystem access.", instructions[1])
        self.assertNotIn("Inspect the trusted workspace.", instructions[1])

    async def test_public_metadata_drops_inherited_authorization(self):
        from vikram.providers import CredentialStore

        def respond(request):
            if request.url.host == "openrouter.ai":
                self.assertNotIn("authorization", request.headers)
                self.assertNotIn("cookie", request.headers)
                return httpx.Response(200, json={"data": [CLOUD]})
            return httpx.Response(200, json={"models": []})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond),
            headers={"Authorization": "Bearer test-key", "Cookie": "session=test"},
        ) as client:
            catalog = ModelCatalog(
                credentials=CredentialStore(environ={}, keychain=MemoryKeychain()),
                http_client=client,
            )
            models = await catalog.refresh(include_cloud=True)
        self.assertFalse(models[0]["available"])

    async def test_cloud_discovery_is_opt_in_cached_and_does_not_send_key(self):
        from vikram.providers import CredentialStore

        requests = []

        def respond(request):
            requests.append(request)
            if request.url.host == "openrouter.ai":
                return httpx.Response(200, json={"data": [CLOUD]})
            return httpx.Response(
                200,
                json=(
                    {"models": [LOCAL]}
                    if request.url.path == "/api/tags"
                    else SHOWN if request.url.path == "/api/show" else {"models": []}
                ),
            )

        store = CredentialStore(environ={"OPENROUTER_API_KEY": "test-key"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            catalog = ModelCatalog(credentials=store, http_client=client)
            local = await catalog.refresh()
            self.assertEqual([m["name"] for m in local], ["local:1"])
            self.assertTrue(all(r.url.host == "localhost" for r in requests))
            first = await catalog.refresh(include_cloud=True)
            first[-1]["name"] = "caller mutation"
            second = await catalog.refresh(include_cloud=True)
            self.assertIn(
                "openrouter:example/reasoner:free", [m["name"] for m in second]
            )
            cloud_requests = [r for r in requests if r.url.host == "openrouter.ai"]
            self.assertEqual(len(cloud_requests), 1)
            self.assertNotIn("authorization", cloud_requests[0].headers)

    async def test_explicit_cloud_works_with_local_service_down(self):
        from vikram.providers import CredentialStore

        def respond(request):
            if request.url.host == "localhost":
                raise httpx.ConnectError("private server detail")
            return httpx.Response(200, json={"data": [CLOUD]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            catalog = ModelCatalog(
                http_client=client,
                credentials=CredentialStore(environ={"OPENROUTER_API_KEY": "test-key"}),
            )
            models = await catalog.refresh(include_cloud=True)
            self.assertEqual(len(models), 1)
            self.assertTrue(models[0]["available"])
            self.assertIn("ollama", catalog.errors)
            with self.assertRaises(ValueError) as caught:
                await catalog.refresh()
            self.assertNotIn("private", str(caught.exception))

    async def test_failed_refresh_marks_cached_cloud_metadata_unavailable(self):
        from vikram.providers import CredentialStore

        broken = False

        def respond(request):
            if request.url.host == "localhost":
                return httpx.Response(200, json={"models": []})
            return (
                httpx.Response(503, json={"error": "private upstream detail"})
                if broken
                else httpx.Response(200, json={"data": [CLOUD]})
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            catalog = ModelCatalog(
                http_client=client,
                credentials=CredentialStore(environ={"OPENROUTER_API_KEY": "test-key"}),
            )
            self.assertTrue((await catalog.refresh(include_cloud=True))[0]["available"])
            broken = True
            models = await catalog.refresh(include_cloud=True, force=True)
            self.assertFalse(models[0]["available"])
            self.assertTrue(models[0]["stale"])
            self.assertNotIn("private", models[0]["reason"])

    async def test_native_adapter_keeps_cloud_model_usage_and_request_settings(self):
        from vikram.providers import CredentialStore, model_settings

        received = []

        def respond(request):
            import json

            received.append(json.loads(request.content))
            self.assertEqual(request.url.host, "openrouter.ai")
            return httpx.Response(
                200,
                json={
                    "id": "test",
                    "object": "chat.completion",
                    "created": 1,
                    "provider": "Example",
                    "model": "example/reasoner:free",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 3,
                        "total_tokens": 15,
                    },
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            agent = build_agent(
                "openrouter:example/reasoner:free",
                http_client=client,
                credentials=CredentialStore(environ={"OPENROUTER_API_KEY": "test-key"}),
            )
            result = await agent.run(
                "Hello",
                model_settings=model_settings(
                    "openrouter:example/reasoner:free", "low"
                ),
            )
            self.assertEqual(result.output, "Hello")
            self.assertEqual(result.usage.input_tokens, 12)
            self.assertEqual(received[0]["model"], "example/reasoner:free")
            self.assertEqual(received[0]["reasoning"], {"effort": "low"})
            self.assertFalse(received[0]["provider"]["allow_fallbacks"])

    async def test_missing_cloud_credentials_fail_before_any_request(self):
        from vikram.providers import CredentialError, CredentialStore

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: self.fail("Unexpected network request")
            )
        ) as client:
            with self.assertRaises(CredentialError):
                build_agent(
                    "openrouter:example/reasoner",
                    http_client=client,
                    credentials=CredentialStore(environ={}, keychain=MemoryKeychain()),
                )


if __name__ == "__main__":
    unittest.main()
