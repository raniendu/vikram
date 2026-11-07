"""Lifecycle and explicit management of optional shared integrations."""

import asyncio
import inspect
import json


async def value(result):
    return await result if inspect.isawaitable(result) else result


async def start(coding):
    if not hasattr(coding, "integration_start_lock"):
        coding.integration_start_lock = asyncio.Lock()
    async with coding.integration_start_lock:
        if coding._closed:
            raise ValueError("This workspace session is closed.")
        await _start(coding)


async def _start(coding):
    from vikram.extensions import ExtensionManager
    from vikram.mcp_client import MCPManager

    if not hasattr(coding, "mcp"):
        manager = MCPManager(coding.settings, mode=coding.mode)
        try:
            await manager.start()
        except BaseException:
            await asyncio.shield(manager.close())
            raise
        coding.mcp = manager
    if not hasattr(coding, "extensions"):
        coding.extensions = ExtensionManager(coding.settings, mode=coding.mode)
    coding.extensions.mode = coding.mode
    try:
        await coding.extensions.start()
    except BaseException:
        manager = coding.extensions
        del coding.extensions
        await asyncio.shield(manager.close())
        raise


async def close(coding):
    if not hasattr(coding, "integration_start_lock"):
        coding.integration_start_lock = asyncio.Lock()
    async with coding.integration_start_lock:
        for name in ("mcp", "extensions"):
            manager = getattr(coding, name, None)
            if manager:
                await manager.close()
        await coding.close()


async def status(coding, kind):
    items = await value(getattr(coding, kind).status())
    if kind == "mcp":
        servers = coding.settings.get("mcp_servers", {})
        return [
            {
                **item,
                "status": item.get("state", "disconnected"),
                "enabled": servers.get(item["name"], {}).get("enabled", True),
                "tool_count": item.get("tools", 0),
            }
            for item in items
        ]
    return items


async def manage(coding, kind, options):
    await start(coding)
    manager = getattr(coding, kind)
    operation = options.get("operation", "list")
    name = options.get("name", "")
    if operation in {"list", "status"}:
        return await status(coding, kind)
    if operation in {"enable", "disable"}:
        await value(manager.enable(name, operation == "enable"))
    elif kind == "mcp":
        if operation == "reconnect":
            await manager.reconnect(name)
        elif operation == "login":
            if not await oauth_login(manager, name):
                raise ValueError(
                    "MCP sign-in did not finish. Check the server status and try again."
                )
        elif operation == "logout":
            await manager.logout(name)
        elif operation in {"add", "remove"}:
            servers = dict(coding.settings.get("mcp_servers", {}))
            if operation == "remove":
                servers.pop(name, None)
            else:
                if not name or not isinstance(options.get("config"), dict):
                    raise ValueError("Supply a server name and configuration object.")
                config = dict(options["config"])
                if config.get("url"):
                    config.setdefault("transport", "http")
                from vikram.mcp_client import MCPConfig

                servers[name] = MCPConfig.model_validate(config).model_dump()
            coding.settings.set("mcp_servers", servers)
            await manager.close()
            del coding.mcp
            await start(coding)
            manager = coding.mcp
        else:
            raise ValueError("Unknown MCP operation.")
    elif operation == "install":
        await value(manager.install(options.get("source", "")))
    elif operation == "update":
        await value(manager.update(name))
    elif operation == "remove":
        await value(manager.remove(name))
    elif operation == "settings":
        return await manager.list_settings(name)
    elif operation == "setting":
        await manager.set_setting(name, options.get("key", ""), options.get("value"))
    else:
        raise ValueError("Unknown package operation.")
    return await status(coding, kind)


async def manage_terminal(ui, kind, argument):
    from vikram.terminal_commands import require_idle

    require_idle(ui)
    coding = ui.session.coding
    await start(coding)
    manager = getattr(coding, kind)
    items = await status(coding, kind)
    choice = await ui.select(
        "MCP servers" if kind == "mcp" else "Python extensions · trusted host code",
        [("add", "Add a server" if kind == "mcp" else "Install a package")]
        + [
            (item["name"], f"{item['name']} · {item.get('status', '')}")
            for item in items
        ],
    )
    if choice is None:
        return
    if choice == "add":
        if kind == "extensions":
            source = argument or await ui.read(
                "Package source · local path, PyPI requirement, or git URL"
            )
            if source:
                await manage(coding, kind, {"operation": "install", "source": source})
        else:
            name = await ui.read("MCP server name")
            config = await ui.read(
                'Configuration JSON · {"command":"...","args":[]} or {"url":"https://..."}'
            )
            if name and config:
                await manage(
                    coding,
                    kind,
                    {"operation": "add", "name": name, "config": json.loads(config)},
                )
    else:
        operation = await ui.select(
            choice,
            [
                (name, name.capitalize())
                for name in (
                    ("enable", "disable", "reconnect", "login", "logout", "remove")
                    if kind == "mcp"
                    else ("enable", "disable", "update", "settings", "remove")
                )
            ],
        )
        if operation:
            if operation == "settings":
                settings = await manager.list_settings(choice)
                key = await ui.select(
                    "Extension setting", [(key, key) for key in settings]
                )
                if key:
                    supplied = await ui.read(
                        key + " (JSON value)", json.dumps(settings[key]["value"])
                    )
                    if supplied is not None:
                        await manager.set_setting(choice, key, json.loads(supplied))
            else:
                await manage(coding, kind, {"operation": operation, "name": choice})
    ui.notice = "Integration settings updated."


async def lifecycle(reply, event, payload):
    """Only explicitly installed extensions receive lifecycle data, without traces."""
    from uuid import uuid4

    manager = reply.coding.extensions
    manager.mode = reply.mode
    for result in await manager.hook(event, payload):
        if result.get("output") or result.get("ui") or result.get("error"):
            reply.activity.append(
                {
                    "kind": "extension",
                    "id": uuid4().hex,
                    "tool": event,
                    "status": "complete" if result.get("ok", True) else "failed",
                    "output": result.get("output", ""),
                    "error": result.get("error"),
                    "ui": result.get("ui", []),
                }
            )
            reply.notify("coding")


async def run_command(coding, name, arguments, interaction=None):
    """An explicit user command is authorized just like an explicit ! command."""
    await start(coding)

    class UserApproved:
        async def approve(self, request):
            return True

    coding.extensions.interaction = interaction or UserApproved()
    coding.extensions.mode = coding.mode
    return await coding.extensions.command(name, arguments)


async def oauth_login(manager, name):
    """Receive one bounded OAuth callback on the configured loopback URI."""
    import contextlib
    import webbrowser
    from urllib.parse import parse_qs, urlsplit

    connection = manager._connections.get(name)
    if connection is None:
        raise ValueError("Unknown MCP server.")
    redirect = urlsplit(connection.config.redirect_uri)
    if (
        redirect.scheme != "http"
        or redirect.hostname != "127.0.0.1"
        or not redirect.port
        or redirect.query
        or redirect.fragment
    ):
        raise ValueError(
            "Use an OAuth redirect_uri such as http://127.0.0.1:8989/callback."
        )
    callback = asyncio.get_running_loop().create_future()
    expected_state = None

    async def receive(reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 5)
            fields = line.decode("ascii").split()
            target = (
                urlsplit(fields[1]) if len(fields) == 3 and fields[0] == "GET" else None
            )
            query = parse_qs(target.query) if target else {}
            code, state = query.get("code", [None])[0], query.get("state", [None])[0]
            valid = (
                target
                and target.path == redirect.path
                and code
                and state
                and state == expected_state
            )
            body = (
                b"Sign-in received. Return to Vikram."
                if valid
                else b"Invalid sign-in callback."
            )
            writer.write(
                b"HTTP/1.1 "
                + (b"200 OK" if valid else b"400 Bad Request")
                + b"\r\nContent-Type: text/plain\r\nConnection: close\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )
            await writer.drain()
            if valid and not callback.done():
                callback.set_result((code, state))
        except (ValueError, UnicodeError, TimeoutError, OSError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def open_login(url):
        nonlocal expected_state
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("The MCP server returned an invalid sign-in URL.")
        expected_state = parse_qs(parsed.query).get("state", [None])[0]
        if not expected_state:
            raise ValueError("The MCP server did not supply an OAuth state.")
        await asyncio.to_thread(webbrowser.open, url)

    async def result():
        return await asyncio.wait_for(callback, 300)

    server = await asyncio.start_server(receive, "127.0.0.1", redirect.port, limit=8192)
    async with server:
        return await manager.login(
            name, open_login, result, connection.config.redirect_uri
        )
