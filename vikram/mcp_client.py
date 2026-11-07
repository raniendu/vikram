"""MCP connections, discovery, OAuth and approvals shared by every interface.

Server descriptions and annotations are untrusted metadata. Only the person's
stored read_only allowlist removes per-call approval. SDK sessions live in an
owner task so their AnyIO scopes always close in the task that opened them.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

import anyio
import httpx
import jsonschema
from mcp import ClientSession, types
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
)
from mcp.shared.message import SessionMessage
from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from vikram.coding import child_environment
from vikram.interaction import ActionRequest, Approver, argument_preview
from vikram.providers import KeychainBackend, MacOSKeychain
from vikram.settings import Settings

MAX_MESSAGE = 1024 * 1024
MAX_OUTPUT = 16_000
MAX_ARGUMENTS = 64 * 1024
MAX_SCHEMA = 16_000
MAX_TOOLS = 500
MAX_PAGES = 50
MAX_SERVERS = 20
APPROVAL_TIMEOUT = 15 * 60
_PRIVATE_MCP = ContextVar("vikram_private_mcp", default=False)


class _PrivateProtocolLogs(logging.Filter):
    def filter(self, record):
        return not _PRIVATE_MCP.get()


# The SDK's debug/error logs include protocol bodies and token errors. Suppress
# them only in this manager's task context, retaining ordinary application logs.
for _logger in (
    "",
    "mcp.client.streamable_http",
    "mcp.client.auth.oauth2",
    "mcp.client.auth.utils",
    "mcp.shared.session",
    "mcp.client.session",
):
    logging.getLogger(_logger).addFilter(_PrivateProtocolLogs())


class MCPConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    transport: Literal["stdio", "http", "streamable-http"] = "stdio"
    enabled: bool = True
    command: str | None = Field(default=None, max_length=4096)
    args: list[str] = Field(default_factory=list, max_length=100)
    cwd: str | None = Field(default=None, max_length=4096)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = Field(default=None, max_length=4096)
    headers: dict[str, str] = Field(default_factory=dict)
    read_only: list[str] = Field(default_factory=list, max_length=MAX_TOOLS)
    exposed: list[str] = Field(default_factory=list, max_length=MAX_TOOLS)
    read_only_resources: list[str] = Field(default_factory=list, max_length=MAX_TOOLS)
    oauth: bool = False
    redirect_uri: str = "http://127.0.0.1:8989/callback"
    timeout: float = Field(default=120.0, gt=0, le=300)

    @model_validator(mode="after")
    def validate_transport(self):
        if self.transport == "stdio":
            if not self.command or self.url or self.oauth:
                raise ValueError(
                    "A stdio server requires a command and cannot use HTTP OAuth."
                )
        else:
            parsed = urlsplit(self.url or "")
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or (
                    parsed.scheme == "http"
                    and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                )
            ):
                raise ValueError(
                    "HTTP MCP requires HTTPS, or HTTP on loopback, without embedded credentials."
                )
            if self.command:
                raise ValueError("An HTTP server cannot specify a command.")
        safe_env = set(child_environment()) | {"LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"}
        if set(self.env) - safe_env:
            raise ValueError(
                "MCP subprocess environments accept terminal/build basics only."
            )
        if any(
            key.lower()
            in {"authorization", "cookie", "proxy-authorization", "x-api-key"}
            for key in self.headers
        ):
            raise ValueError(
                "Credentials belong in OAuth Keychain storage, not server settings."
            )
        if any(len(argument) > MAX_ARGUMENTS for argument in self.args):
            raise ValueError("A server argument exceeds the size limit.")
        return self


class MCPTool(BaseModel):
    name: str
    server: str
    original_name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    exposed: bool = False


class MCPResult(BaseModel):
    ok: bool
    output: str = ""
    structured_content: dict[str, Any] | None = None
    error: str | None = None
    truncated: bool = False
    activity: str = ""


class KeychainTokenStorage:
    """SDK TokenStorage; both tokens and confidential client registrations stay in Keychain."""

    def __init__(self, server_url: str, keychain: KeychainBackend | None = None):
        self.keychain = keychain if keychain is not None else MacOSKeychain()
        self.prefix = "mcp:" + hashlib.sha256(server_url.encode()).hexdigest()

    async def _get(self, suffix: str, model: type[BaseModel]):
        value = await asyncio.to_thread(self.keychain.get, f"{self.prefix}:{suffix}")
        if not value or len(value) > MAX_ARGUMENTS:
            return None
        try:
            return model.model_validate_json(value)
        except (ValidationError, ValueError):
            return None

    async def get_tokens(self) -> OAuthToken | None:
        value = await asyncio.to_thread(self.keychain.get, f"{self.prefix}:tokens")
        if not value or len(value) > MAX_ARGUMENTS:
            return None
        try:
            stored = json.loads(value)
            token = OAuthToken.model_validate(stored["tokens"])
            if stored.get("expires_at") is not None:
                token.expires_in = int(stored["expires_at"] - time.time())
            return token
        except (ValueError, KeyError, TypeError):
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        previous = await self.get_tokens()
        if tokens.refresh_token is None and previous is not None:
            tokens = tokens.model_copy(update={"refresh_token": previous.refresh_token})
        value = {
            "tokens": tokens.model_dump(),
            "expires_at": (
                time.time() + tokens.expires_in
                if tokens.expires_in is not None
                else None
            ),
        }
        await asyncio.to_thread(
            self.keychain.set, f"{self.prefix}:tokens", json.dumps(value)
        )

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return await self._get("client", OAuthClientInformationFull)

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        await asyncio.to_thread(
            self.keychain.set, f"{self.prefix}:client", client_info.model_dump_json()
        )

    async def clear(self) -> None:
        await asyncio.to_thread(self.keychain.delete, f"{self.prefix}:tokens")
        await asyncio.to_thread(self.keychain.delete, f"{self.prefix}:client")
        await asyncio.to_thread(self.keychain.delete, f"{self.prefix}:metadata")

    async def get_metadata(self) -> OAuthMetadata | None:
        return await self._get("metadata", OAuthMetadata)

    async def set_metadata(self, metadata: OAuthMetadata) -> None:
        await asyncio.to_thread(
            self.keychain.set, f"{self.prefix}:metadata", metadata.model_dump_json()
        )


class _PrivateOAuthProvider(OAuthClientProvider):
    async def async_auth_flow(self, request):
        token = _PRIVATE_MCP.set(True)
        flow = super().async_auth_flow(request)
        try:
            outgoing = await anext(flow)
            while True:
                response = yield outgoing
                try:
                    outgoing = await flow.asend(response)
                except StopAsyncIteration:
                    break
        finally:
            await flow.aclose()
            _PRIVATE_MCP.reset(token)

    async def _initialize(self):
        await super()._initialize()
        self.context.oauth_metadata = await self.context.storage.get_metadata()
        if self.context.current_tokens:
            self.context.update_token_expiry(self.context.current_tokens)
        client = self.context.client_info
        if client and client.issuer and not self.context.oauth_metadata:
            if (
                urlsplit(client.issuer).netloc
                != urlsplit(self.context.server_url).netloc
            ):
                self.context.clear_tokens()

    async def _handle_token_response(self, response):
        await super()._handle_token_response(response)
        if self.context.oauth_metadata:
            await self.context.storage.set_metadata(self.context.oauth_metadata)

    async def _handle_refresh_response(self, response):
        valid = await super()._handle_refresh_response(response)
        if valid:
            self.context.current_tokens = await self.context.storage.get_tokens()
        return valid


async def _terminate(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=0.5)
    except TimeoutError:
        pass
    # A wrapper may exit while grandchildren still hold its process group alive.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


@asynccontextmanager
async def _stdio(config: MCPConfig):
    """Bounded stdio transport using the SDK's typed SessionMessage streams."""
    incoming_writer, incoming = anyio.create_memory_object_stream(0)
    outgoing, outgoing_reader = anyio.create_memory_object_stream(0)
    process = await asyncio.create_subprocess_exec(
        config.command,
        *config.args,
        cwd=config.cwd,
        env={**child_environment(), **config.env},
        start_new_session=True,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=MAX_MESSAGE + 1,
    )

    async def read():
        async with incoming_writer:
            try:
                while line := await process.stdout.readline():
                    if len(line) > MAX_MESSAGE:
                        raise ValueError("MCP message exceeds the size limit.")
                    try:
                        message = types.JSONRPCMessage.model_validate_json(line)
                    except ValidationError:
                        raise ValueError(
                            "The MCP server returned invalid protocol data."
                        ) from None
                    await incoming_writer.send(SessionMessage(message))
            except (OSError, ValueError):
                await incoming_writer.send(
                    ValueError(
                        "The MCP stdio transport failed or exceeded its message limit."
                    )
                )

    async def write():
        async with outgoing_reader:
            try:
                async for message in outgoing_reader:
                    encoded = message.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    ).encode()
                    if len(encoded) > MAX_MESSAGE:
                        raise ValueError("MCP message exceeds the size limit.")
                    process.stdin.write(encoded + b"\n")
                    await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                return

    readers = [asyncio.create_task(read()), asyncio.create_task(write())]
    try:
        yield incoming, outgoing
    finally:
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        await _terminate(process)
        await incoming.aclose()
        await outgoing.aclose()


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream):
        self.stream = stream

    async def __aiter__(self):
        size = 0
        async for data in self.stream:
            size += len(data)
            if size > MAX_MESSAGE:
                raise ValueError("MCP HTTP response exceeds the size limit.")
            yield data

    async def aclose(self):
        await self.stream.aclose()


class _BoundedTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport: httpx.AsyncBaseTransport):
        self.transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.transport.handle_async_request(request)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_BoundedStream(response.stream),
            extensions=response.extensions,
        )

    async def aclose(self):
        await self.transport.aclose()


@dataclass
class _Connection:
    config: MCPConfig
    state: str = "disconnected"
    session: ClientSession | None = None
    task: asyncio.Task | None = None
    ready: asyncio.Future | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    resources: bool = False
    error: str | None = None


def _stable_name(server: str, tool: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", tool)
    if normalized != tool or len(normalized) > 80:
        normalized = (
            normalized[:60] + "_" + hashlib.sha256(tool.encode()).hexdigest()[:12]
        )
    return f"mcp__{server}__{normalized}"


def _local_schema(value: Any) -> bool:
    if isinstance(value, dict):
        return all(
            (key != "$ref" or (isinstance(item, str) and item.startswith("#")))
            and _local_schema(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return all(_local_schema(item) for item in value)
    return True


class MCPManager:
    def __init__(
        self,
        settings: Settings,
        interaction: Any = None,
        mode: str = "normal",
        *,
        keychain: KeychainBackend | None = None,
        http_transport_factory: Callable[[str], httpx.AsyncBaseTransport] | None = None,
        max_calls: int = 40,
        max_total_output: int = 256_000,
    ) -> None:
        self.settings, self.interaction, self.mode = settings, interaction, mode
        self.keychain = keychain
        self.http_transport_factory = http_transport_factory
        self.max_calls, self.max_total_output = max_calls, max_total_output
        self._connections: dict[str, _Connection] = {}
        self._invalid: set[str] = set()
        self._tools: dict[str, MCPTool] = {}
        self._oauth: dict[str, OAuthClientProvider] = {}
        self._calls: dict[str, tuple[str, asyncio.Future[MCPResult]]] = {}
        self._active: set[asyncio.Task] = set()
        self._count, self._output = 0, 0
        self._mutations = asyncio.Lock()
        self._closed = False
        self._load_config()

    def _load_config(self) -> None:
        values = self.settings.get("mcp_servers", {})
        for name, raw in list(values.items())[:MAX_SERVERS]:
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", name):
                continue
            try:
                config = MCPConfig.model_validate(raw)
            except (ValidationError, ValueError):
                self._invalid.add(name)
                continue
            self._connections[name] = _Connection(config)

    def oauth_provider(
        self,
        name: str,
        *,
        redirect_handler: Callable[[str], Awaitable[None]] | None = None,
        callback_handler: Callable[[], Awaitable[tuple[str, str | None]]] | None = None,
        redirect_uri: str | None = None,
    ) -> OAuthClientProvider:
        connection = self._connections[name]
        if not connection.config.url:
            raise ValueError("OAuth requires an HTTP MCP server.")
        return _PrivateOAuthProvider(
            connection.config.url,
            OAuthClientMetadata(
                redirect_uris=[AnyUrl(redirect_uri or connection.config.redirect_uri)],
                client_name="Vikram",
                token_endpoint_auth_method="none",
            ),
            KeychainTokenStorage(connection.config.url, self.keychain),
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )

    async def login(
        self,
        name: str,
        redirect_handler: Callable[[str], Awaitable[None]],
        callback_handler: Callable[[], Awaitable[tuple[str, str | None]]],
        redirect_uri: str | None = None,
    ) -> bool:
        self._oauth[name] = self.oauth_provider(
            name,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
            redirect_uri=redirect_uri,
        )
        await self.reconnect(name)
        return self._connections[name].state == "connected"

    async def logout(self, name: str) -> None:
        connection = self._connections[name]
        await self._disconnect(connection)
        self._oauth.pop(name, None)
        if connection.config.url:
            await KeychainTokenStorage(connection.config.url, self.keychain).clear()

    async def start(self) -> None:
        if self._closed:
            raise ValueError("The MCP manager is closed.")
        await asyncio.gather(
            *(
                self._connect(name, connection)
                for name, connection in self._connections.items()
            )
        )

    async def _connect(self, name: str, connection: _Connection) -> None:
        if not connection.config.enabled:
            connection.state = "disabled"
            return
        if connection.task and not connection.task.done():
            if connection.ready:
                await asyncio.shield(connection.ready)
            return
        connection.stop = asyncio.Event()
        connection.ready = asyncio.get_running_loop().create_future()
        connection.state, connection.error = "connecting", None
        connection.task = asyncio.create_task(self._serve(name, connection))
        try:
            await asyncio.wait_for(
                asyncio.shield(connection.ready),
                timeout=300 if name in self._oauth else 30,
            )
        except TimeoutError:
            await self._disconnect(connection)
            connection.state, connection.error = (
                "failed",
                "The MCP connection timed out.",
            )
        except asyncio.CancelledError:
            connection.stop.set()
            connection.task.cancel()
            await asyncio.gather(connection.task, return_exceptions=True)
            raise

    async def _serve(self, name: str, connection: _Connection) -> None:
        private = _PRIVATE_MCP.set(True)
        try:
            async with AsyncExitStack() as stack:
                config = connection.config
                if config.transport == "stdio":
                    streams = await stack.enter_async_context(_stdio(config))
                else:
                    auth = self._oauth.get(name)
                    if auth is None and config.oauth:
                        auth = self.oauth_provider(name)
                    transport = (
                        self.http_transport_factory(name)
                        if self.http_transport_factory
                        else httpx.AsyncHTTPTransport()
                    )
                    client = await stack.enter_async_context(
                        httpx.AsyncClient(
                            auth=auth,
                            headers=config.headers,
                            transport=_BoundedTransport(transport),
                            trust_env=False,
                            timeout=httpx.Timeout(30, read=config.timeout),
                        )
                    )
                    streams = await stack.enter_async_context(
                        streamable_http_client(config.url, http_client=client)
                    )
                session = await stack.enter_async_context(
                    ClientSession(
                        streams[0],
                        streams[1],
                        read_timeout_seconds=timedelta(seconds=config.timeout),
                    )
                )
                initialized = await session.initialize()
                connection.session = session
                connection.resources = initialized.capabilities.resources is not None
                if initialized.capabilities.tools is not None:
                    for tool in await self._pages(session.list_tools, "tools"):
                        schema = tool.inputSchema
                        if (
                            not _local_schema(schema)
                            or len(json.dumps(schema)) > MAX_SCHEMA
                        ):
                            continue
                        try:
                            jsonschema.Draft202012Validator.check_schema(schema)
                        except jsonschema.SchemaError:
                            continue
                        stable = _stable_name(name, tool.name)
                        if stable in self._tools or len(self._tools) >= MAX_TOOLS:
                            raise ValueError(
                                "MCP tools exceed the shared limit or contain duplicate names."
                            )
                        self._tools[stable] = MCPTool(
                            name=stable,
                            server=name,
                            original_name=tool.name,
                            description=(tool.description or "")[:4000],
                            input_schema=schema,
                            read_only=tool.name in config.read_only,
                            exposed=tool.name in config.exposed,
                        )
                connection.state = "connected"
                connection.ready.set_result(True)
                await connection.stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            connection.state, connection.error = (
                "failed",
                "The MCP connection failed; check its configuration or sign in.",
            )
        finally:
            _PRIVATE_MCP.reset(private)
            connection.session = None
            if connection.ready and not connection.ready.done():
                connection.ready.set_result(False)
            if connection.state != "failed":
                connection.state = (
                    "disconnected" if connection.config.enabled else "disabled"
                )

    @staticmethod
    async def _pages(method, attribute: str) -> list:
        result, cursor, seen = [], None, set()
        for _ in range(MAX_PAGES):
            page = await method(cursor=cursor)
            result.extend(getattr(page, attribute))
            if len(result) > MAX_TOOLS:
                raise ValueError("MCP discovery exceeds its item limit.")
            cursor = page.nextCursor
            if not cursor:
                return result
            if cursor in seen:
                raise ValueError("MCP discovery repeated a pagination cursor.")
            seen.add(cursor)
        raise ValueError("MCP discovery exceeds its page limit.")

    async def _disconnect(self, connection: _Connection) -> None:
        if connection.task and not connection.task.done():
            connection.stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(connection.task), timeout=3)
            except TimeoutError:
                connection.task.cancel()
                await asyncio.gather(connection.task, return_exceptions=True)

    async def reconnect(self, name: str) -> None:
        if self._closed:
            raise ValueError("The MCP manager is closed.")
        connection = self._connections[name]
        await self._disconnect(connection)
        self._tools = {
            key: tool for key, tool in self._tools.items() if tool.server != name
        }
        await self._connect(name, connection)

    async def enable(self, name: str, enabled: bool) -> None:
        connection = self._connections[name]
        config = self.settings.get("mcp_servers", {})
        config[name]["enabled"] = bool(enabled)
        self.settings.set("mcp_servers", config)
        connection.config.enabled = bool(enabled)
        if enabled:
            await self.reconnect(name)
        else:
            await self._disconnect(connection)
            connection.state = "disabled"

    async def status(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "transport": connection.config.transport,
                "state": connection.state,
                "error": connection.error,
                "tools": sum(t.server == name for t in self._tools.values()),
            }
            for name, connection in self._connections.items()
        ] + [
            {"name": name, "state": "invalid", "error": "Invalid MCP configuration."}
            for name in sorted(self._invalid)
        ]

    def begin_reply(self) -> None:
        """Keep connections and exposure while starting fresh per-reply budgets."""
        if (
            self._closed
            or self._active
            or any(not pending.done() for _, pending in self._calls.values())
        ):
            raise RuntimeError(
                "Cannot begin a reply while MCP actions are pending or closed."
            )
        self._count, self._output = 0, 0
        self._calls.clear()

    async def list_tools(self, exposed_only: bool = False) -> list[MCPTool]:
        return [
            tool.model_copy(deep=True)
            for tool in self._tools.values()
            if self._connections[tool.server].state == "connected"
            and (not exposed_only or tool.exposed)
        ]

    async def search_tools(self, query: str, limit: int = 10) -> list[MCPTool]:
        terms = query.casefold().split()[:20]
        scored = []
        for tool in await self.list_tools():
            haystack = f"{tool.name} {tool.description}".casefold()
            score = sum(term in haystack for term in terms)
            if not terms or score:
                scored.append((score, tool.name))
        selected = sorted(scored, key=lambda item: (-item[0], item[1]))[
            : max(1, min(limit, 50))
        ]
        for _, name in selected:
            self._tools[name].exposed = True
        return [self._tools[name].model_copy(deep=True) for _, name in selected]

    def _failure(self, message: str) -> MCPResult:
        return MCPResult(ok=False, error=message, activity="MCP action declined")

    async def _approve(
        self, name: str, identifier: str, reason: str, interaction: Any, arguments: dict
    ) -> bool:
        if not isinstance(interaction, Approver):
            return False
        try:
            return (
                await asyncio.wait_for(
                    interaction.approve(
                        ActionRequest(
                            id=identifier,
                            summary=f"Call {name}",
                            reason=reason,
                            url="",
                            kind="mcp",
                            details=argument_preview(arguments),
                        )
                    ),
                    timeout=APPROVAL_TIMEOUT,
                )
                is True
            )
        except TimeoutError:
            return False

    async def call_tool(
        self,
        name: str,
        args: Mapping[str, Any],
        call_id: str | None = None,
        *,
        interaction: Any = None,
        mode: str | None = None,
    ) -> MCPResult:
        tool = self._tools.get(name)
        if tool is None:
            return self._failure("Unknown MCP tool; discover the server's tools first.")
        try:
            encoded = json.dumps(args, allow_nan=False)
            if len(encoded.encode()) > MAX_ARGUMENTS:
                raise ValueError()
            jsonschema.Draft202012Validator(tool.input_schema).validate(args)
        except (ValueError, TypeError, jsonschema.ValidationError):
            return self._failure("Invalid MCP tool arguments; use the declared schema.")
        connection = self._connections[tool.server]
        return await self._dispatch(
            name,
            dict(args),
            tool.read_only,
            connection,
            lambda session: session.call_tool(tool.original_name, dict(args)),
            call_id,
            interaction=interaction,
            mode=mode,
        )

    async def _dispatch(
        self,
        name: str,
        args: dict,
        read_only: bool,
        connection: _Connection,
        invoke,
        call_id: str | None,
        *,
        interaction: Any = None,
        mode: str | None = None,
    ) -> MCPResult:
        interaction = self.interaction if interaction is None else interaction
        mode = self.mode if mode is None else mode
        identifier = call_id or uuid.uuid4().hex
        fingerprint = hashlib.sha256(
            json.dumps([name, args], sort_keys=True).encode()
        ).hexdigest()
        if identifier in self._calls:
            previous, future = self._calls[identifier]
            if fingerprint != previous:
                return self._failure(
                    "A call ID cannot identify a different MCP action."
                )
            return await asyncio.shield(future)
        if self._closed or connection.state != "connected" or not connection.session:
            return self._failure("The MCP server is not connected.")
        if self._count >= self.max_calls or self._output >= self.max_total_output:
            return self._failure("The shared MCP tool budget is exhausted.")
        self._count += 1
        future = asyncio.get_running_loop().create_future()
        self._calls[identifier] = fingerprint, future
        current = asyncio.current_task()
        self._active.add(current)
        private = _PRIVATE_MCP.set(True)
        try:
            if mode not in {"normal", "plan"} or (mode == "plan" and not read_only):
                result = self._failure(
                    "Plan mode only permits tools on the user's read-only allowlist."
                )
            elif not read_only and not await self._approve(
                name,
                identifier,
                "This MCP tool's effects are unknown; its server annotations do not establish permission.",
                interaction,
                args,
            ):
                result = self._failure("The user did not approve this MCP action.")
            else:

                async def execute():
                    if (
                        self._closed
                        or connection.state != "connected"
                        or not connection.session
                    ):
                        return self._failure(
                            "The MCP connection closed before this action ran."
                        )
                    raw = await asyncio.wait_for(
                        invoke(connection.session), timeout=connection.config.timeout
                    )
                    return self._result(raw, name)

                if read_only:
                    result = await execute()
                else:
                    async with self._mutations:
                        result = await execute()
        except asyncio.CancelledError:
            future.set_result(
                self._failure(
                    "The MCP action was cancelled with uncertain effects; it will not be replayed."
                )
            )
            raise
        except Exception:
            result = self._failure(
                "The MCP action failed with uncertain effects; it will not be replayed automatically."
            )
        finally:
            _PRIVATE_MCP.reset(private)
            self._active.discard(current)
        future.set_result(result)
        return result

    def _result(self, raw: Any, name: str) -> MCPResult:
        blocks = getattr(raw, "content", getattr(raw, "contents", []))
        parts = []
        for block in blocks:
            text = getattr(block, "text", None)
            parts.append(
                text if isinstance(text, str) else "[Non-text MCP content omitted]"
            )
        output = "\n".join(parts)
        remaining = max(0, min(MAX_OUTPUT, self.max_total_output - self._output))
        truncated = len(output) > remaining
        output = output[:remaining]
        structured = getattr(raw, "structuredContent", None)
        if structured is not None:
            encoded = json.dumps(structured)
            if len(encoded) + len(output) > remaining:
                structured, truncated = None, True
            else:
                self._output += len(encoded)
        self._output += len(output)
        return MCPResult(
            ok=not getattr(raw, "isError", False),
            output=output,
            structured_content=structured,
            truncated=truncated,
            activity=f"Called {name}",
        )

    async def _resources(
        self, server: str | None, templates: bool
    ) -> list[dict[str, Any]]:
        result = []
        for name, connection in self._connections.items():
            if server is not None and name != server:
                continue
            if (
                connection.state != "connected"
                or not connection.session
                or not connection.resources
            ):
                continue
            method = (
                connection.session.list_resource_templates
                if templates
                else connection.session.list_resources
            )
            attribute = "resourceTemplates" if templates else "resources"
            for resource in await self._pages(method, attribute):
                if len(result) >= MAX_TOOLS:
                    raise ValueError("MCP resources exceed the shared discovery limit.")
                value = {
                    "server": name,
                    "name": resource.name[:200],
                    "description": (resource.description or "")[:4000],
                    "mimeType": (resource.mimeType or "")[:200],
                }
                value["uriTemplate" if templates else "uri"] = str(
                    resource.uriTemplate if templates else resource.uri
                )[:4096]
                result.append(value)
        return result

    async def list_resources(self, server: str | None = None) -> list[dict[str, Any]]:
        return await self._resources(server, False)

    async def list_resource_templates(
        self, server: str | None = None
    ) -> list[dict[str, Any]]:
        return await self._resources(server, True)

    async def read_resource(
        self,
        server: str,
        uri: str,
        call_id: str | None = None,
        *,
        interaction: Any = None,
        mode: str | None = None,
    ) -> MCPResult:
        connection = self._connections.get(server)
        if connection is None or len(uri) > 4096:
            return self._failure("Unknown MCP resource server or invalid URI.")
        try:
            parsed = AnyUrl(uri)
        except ValueError:
            return self._failure("Invalid MCP resource URI.")
        return await self._dispatch(
            f"mcp__{server}__read_resource",
            {"uri": uri},
            uri in connection.config.read_only_resources,
            connection,
            lambda session: session.read_resource(parsed),
            call_id,
            interaction=interaction,
            mode=mode,
        )

    async def close(self) -> None:
        self._closed = True
        pending = tuple(
            task for task in self._active if task is not asyncio.current_task()
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.gather(*(self._disconnect(c) for c in self._connections.values()))

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.close()
