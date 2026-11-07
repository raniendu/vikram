"""Terminal command presentation; shared services perform every operation."""

import asyncio
import binascii
import json
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from vikram.models import thinking_selection
from vikram.providers import ModelRef

MAX_ATTACHMENT_BYTES = 10 * 1024**2


def dropped_paths(value):
    """Recognize user-dropped paths, including one unquoted path with spaces."""
    try:
        candidate = Path(value).expanduser()
        if candidate.is_file():
            return [candidate]
        tokens = shlex.split(value)
        paths = [Path(token).expanduser() for token in tokens]
        return paths if paths and all(path.is_file() for path in paths) else []
    except (OSError, ValueError):
        return []


def attachment_prefix(text):
    """Consume leading @ shell-quoted paths without rewriting the remaining prompt."""
    paths = []
    token_pattern = re.compile(r"""(?:[^\s\\'"]+|\\.|"(?:\\.|[^"])*"|'[^']*')+""")
    while text.startswith("@"):
        match = token_pattern.match(text)
        if match is None:
            raise ValueError("Quote paths containing spaces after @.")
        token = shlex.split(match[0])[0]
        path = Path(token[1:]).expanduser()
        consumed = match.end()
        if not path.is_file():
            # Terminal drops often escape spaces; pasted raw paths may not.
            for candidate in token_pattern.finditer(text, consumed):
                possible = Path(text[1 : candidate.end()]).expanduser()
                if possible.is_file():
                    path, consumed = possible, candidate.end()
                    break
        paths.append(path)
        text = text[consumed:].lstrip()
    return paths, text


def image_notice(ui, name):
    model = next(
        (
            item
            for item in ui.models
            if ModelRef.parse(item["name"]) == ModelRef.parse(ui.session.model)
        ),
        {},
    )
    message = f"Attached image {name}. Image preview is unavailable in this terminal."
    if model and not model.get("vision"):
        message += (
            " This model is text-only; choose an image-capable model before sending."
        )
    return message


async def attach_paths(ui, paths):
    require_idle(ui)
    notice = "Attached. Files will be included with the next message."
    for path in paths:
        if not path.is_file():
            raise ValueError("Choose a regular file to attach.")
        await ui.session.attach_file(path)
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            notice = image_notice(ui, path.name)
    ui.notice = notice


def read_clipboard_image():
    """User-invoked macOS clipboard read; no key-bearing environment is inherited."""
    from vikram.coding import child_environment

    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            ["osascript", "-e", "get the clipboard as «class PNGf»"],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.DEVNULL,
            env=child_environment(),
            timeout=5,
        )
        output.seek(0)
        raw = output.read(MAX_ATTACHMENT_BYTES * 2 + 100)
    if (
        result.returncode
        or not raw.startswith("«data PNGf".encode())
        or len(raw) > MAX_ATTACHMENT_BYTES * 2 + 30
    ):
        raise ValueError(
            "Copy a PNG-compatible image first, then use Ctrl+V to attach it."
        )
    try:
        return binascii.unhexlify(raw[11:-3])
    except (ValueError, binascii.Error):
        raise ValueError("The clipboard image could not be read.") from None


def resource_prompt(ui, name, argument):
    resources = ui.resources
    if name.startswith("skill:"):
        skill = name.removeprefix("skill:")
        return f"Use the following skill for this request: {skill}\n\n{resources.read_skill(skill)}\n\nRequest: {argument}"
    if not any(item.name == name for item in resources.catalog.templates):
        return None
    text = resources.template(name)
    if text.startswith("---\n"):
        _header, separator, body = text[4:].partition("\n---")
        if separator:
            text = body.lstrip("\r\n")
    arguments = shlex.split(argument) if re.search(r"\$\d+", text) else []
    used = "$ARGUMENTS" in text or bool(re.search(r"\$\d+", text))
    text = re.sub(
        r"\$(ARGUMENTS|\d+)",
        lambda match: (
            argument
            if match[1] == "ARGUMENTS"
            else (
                arguments[int(match[1]) - 1]
                if 0 < int(match[1]) <= len(arguments)
                else ""
            )
        ),
        text,
    )
    return text + ("\n\n" + argument if argument and not used else "")


async def extension_catalog(ui):
    from vikram.integrations import start

    coding = getattr(ui.session, "coding", None)
    if coding is None or ui.session.mode != "normal":
        ui.extension_commands = {}
        return None
    await start(coding)
    manager = coding.extensions
    manager.interaction, manager.mode = ui, ui.session.mode
    ui.extension_commands = {
        item["name"]: item for item in await manager.list_commands()
    }
    return manager


async def extension_command(ui, name, argument):
    from vikram.integrations import run_command

    require_idle(ui)
    manager = await extension_catalog(ui)
    if manager is None or name not in ui.extension_commands:
        raise ValueError("This extension command is unavailable in the current mode.")
    command = ui.extension_commands[name]
    schema = command.get("input_schema", command.get("schema", {}))
    properties = schema.get("properties", {})
    text_key = next(iter(properties)) if len(properties) == 1 else None
    if text_key and properties[text_key].get("type") != "string":
        text_key = None
    if not argument:
        argument = await ui.read(
            (f"{text_key} · " if text_key else "Command arguments as JSON · ") + name,
            "" if text_key else "{}",
        )
        if argument is None:
            return
    try:
        arguments = (
            {text_key: argument}
            if text_key and not argument.lstrip().startswith("{")
            else json.loads(argument)
        )
    except ValueError:
        raise ValueError("Enter valid JSON command arguments.") from None
    if not isinstance(arguments, dict):
        raise ValueError("Command arguments must be a JSON object.")
    ui.session.coding.mode = ui.session.mode
    result = await run_command(ui.session.coding, name, arguments)
    pieces = [result.get("error") or result.get("output") or ""]
    for item in result.get("ui", []):
        if item.get("title"):
            pieces.append(str(item["title"]))
        if item["type"] in {"text", "notice"}:
            pieces.append(item["text"])
        elif item["type"] == "progress":
            pieces.append(f"{item.get('label', '')}: {item['value']}/{item['total']}")
        elif item["type"] == "table":
            pieces.append(" | ".join(item["columns"]))
            pieces.extend(" | ".join(str(cell) for cell in row) for row in item["rows"])
    ui.messages.append(
        {
            "role": "assistant",
            "text": "\n\n".join(pieces),
            "status": "complete" if result.get("ok", True) else "failed",
        }
    )
    ui.redraw()


def require_idle(ui):
    if not ui.idle.is_set():
        raise ValueError("Stop the active reply before changing this session.")


def require_store(ui):
    store = getattr(ui.session, "store", None)
    if store is None:
        raise ValueError(
            "This command needs a saved session. Restart without --no-session."
        )
    return store


async def switch_session(ui, chat_id):
    from vikram.session import Session

    require_idle(ui)
    old = ui.session
    store = require_store(ui)
    if chat_id == old.chat_id:
        return
    ui.save_draft()
    # Claim the destination before releasing the current conversation. A failed
    # switch must not close the resources that the current session still uses.
    replacement = Session(
        old.model,
        store=store,
        chat_id=chat_id,
        streamer=old.streamer,
        summarizer=old.summarizer,
    )
    browser, tasks = old.browser, old.tasks
    old.browser = old.tasks = None
    try:
        await old.close()
    except BaseException:
        old.browser, old.tasks = browser, tasks
        await replacement.close()
        raise
    replacement.browser, replacement.tasks = browser, tasks
    ui.session = replacement
    ui.cloud_enabled = ui.session.model.startswith("openrouter:")
    ui.settings.workspace = Path(ui.session.workspace or Path.cwd())
    await configure_coding(ui)
    ui.editor.text = store.chat(chat_id)["draft"]
    ui.editor.buffer.cursor_position = len(ui.editor.text)
    ui._load_transcript()
    ui.apply_preferences()


async def configure_coding(ui):
    from vikram.coding import CodingSession
    from vikram.integrations import close
    from vikram.resources import ProjectResources

    if previous := getattr(ui.session, "coding", None):
        await close(previous)
    resources = ProjectResources(ui.settings)
    resources.reload()
    ui.resources = resources
    ui.session.coding = CodingSession(
        ui.settings.workspace or Path.cwd(), settings=ui.settings
    )
    ui.session.instructions = resources.instructions()
    await extension_catalog(ui)


async def dispatch(ui, name, argument):
    session = ui.session
    if name == "session":
        state = session.context_state()
        ui.notice = f"Session {session.session_id} · {session.workspace or 'no workspace'} · {json.dumps(state)}"
    elif name == "settings":
        choice = await ui.select(
            "Settings",
            [
                ("theme", "Theme"),
                ("default_model", "Default model · new CLI and desktop conversations"),
                ("external_editor", "External editor"),
                ("tui_mode", "Default terminal mode"),
            ],
        )
        if choice == "theme":
            value = await ui.select(
                "Theme",
                [(x, x.capitalize()) for x in ("system", "dark", "light")]
                + [(item.name, item.name) for item in ui.resources.catalog.themes],
            )
            if value:
                ui.settings.set("theme", value)
                ui.app.style = ui._style()
        elif choice in {"default_model", "local_default"}:
            await ui.pick_model(default_only=True)
        elif choice == "external_editor":
            value = await ui.read(
                "External editor command", ui.settings.get("editor", "")
            )
            if value is not None:
                ui.settings.set("editor", value)
        elif choice == "tui_mode":
            value = await ui.select(
                "Default terminal mode",
                [("fullscreen", "Fullscreen"), ("regular", "Regular scrollback")],
            )
            if value:
                ui.settings.set("interface", value)
    elif name == "scoped-models":
        models = await ui.refresh_models()
        scoped = list(ui.settings.get("models", []))
        while True:
            choice = await ui.select(
                "Models in Ctrl+P cycle · select to toggle",
                [("done", "Done"), ("all", "Use all available models")]
                + [
                    (m["name"], ("✓ " if m["name"] in scoped else "  ") + m["name"])
                    for m in models
                    if m["available"]
                ],
            )
            if choice in (None, "done"):
                break
            if choice == "all":
                scoped = []
                break
            if choice in scoped:
                scoped.remove(choice)
            else:
                scoped.append(choice)
        ui.settings.set("models", scoped)
    elif name in {"new", "resume", "clone", "fork", "tree", "name", "import", "export"}:
        require_idle(ui)
        store = require_store(ui)
        if name == "new":
            from vikram.agent import DEFAULT_MODEL

            model = ui.settings.get("model") or DEFAULT_MODEL
            await switch_session(
                ui, store.create_chat(model, workspace=session.workspace)["id"]
            )
        elif name == "resume":
            chats = store.list_chats(query=argument)
            choice = await ui.select(
                "Resume conversation",
                [
                    (
                        chat["id"],
                        f"{chat['title']} · {chat.get('workspace') or 'chat'} · {chat.get('model') or ''}",
                    )
                    for chat in chats
                ],
            )
            if choice:
                selected = store.chat(choice)
                action = await ui.select(
                    selected["title"],
                    [("resume", "Resume"), ("rename", "Rename"), ("delete", "Delete…")],
                )
                if action == "resume":
                    await switch_session(ui, choice)
                elif action == "rename":
                    value = await ui.read("Conversation name", selected["title"])
                    if value and value.strip():
                        store.update_chat(choice, title=value.strip())
                        ui.notice = "Conversation renamed."
                elif action == "delete":
                    confirm = await ui.select(
                        "Delete this saved conversation and its imported copies?",
                        [
                            (False, "Keep conversation"),
                            (True, "Delete “" + selected["title"] + "”"),
                        ],
                    )
                    if confirm is True:
                        if choice == session.chat_id:
                            remaining = [chat for chat in chats if chat["id"] != choice]
                            target = (
                                remaining[0]
                                if remaining
                                else store.create_chat(
                                    session.model, workspace=session.workspace
                                )
                            )
                            await switch_session(ui, target["id"])
                        store.delete_chat(choice)
                        ui.notice = "Conversation deleted."
        elif name == "name":
            value = argument or await ui.read(
                "Conversation name", store.chat(session.chat_id)["title"]
            )
            if value:
                store.update_chat(session.chat_id, title=value)
                ui.notice = "Named " + value
        elif name in {"tree", "fork", "clone"}:
            if name == "clone":
                chat = store.clone_chat(session.chat_id)
                await switch_session(ui, chat["id"])
            else:
                entries = store.tree(session.chat_id)
                choice = await ui.select(
                    "Conversation branches · workspace files are unchanged",
                    [
                        (item["id"], f"{item.get('text', '')[:100]} · {item['id'][:8]}")
                        for item in entries
                    ],
                )
                if choice:
                    if name == "fork":
                        await switch_session(
                            ui, store.fork_chat(session.chat_id, choice)["id"]
                        )
                    else:
                        store.select_branch(session.chat_id, choice)
                        session.reload()
                        ui._load_transcript()
        elif name == "export":
            from vikram.session_io import export_session

            value = argument or await ui.read(
                "Export path (.md, .html or .jsonl)", f"vikram-{session.chat_id[:8]}.md"
            )
            if value:
                path = Path(value).expanduser()
                if path.exists():
                    raise ValueError(
                        "Choose a new export path; existing files are preserved."
                    )
                format = {".md": "markdown", ".html": "html", ".jsonl": "jsonl"}.get(
                    path.suffix
                )
                if not format:
                    raise ValueError("Use .md, .html or .jsonl.")
                content = export_session(store, session.chat_id, format=format)
                with path.open("x", encoding="utf-8") as handle:
                    handle.write(content)
                path.chmod(0o600)
                ui.notice = "Exported to " + str(path)
        else:
            from vikram.session_io import import_session_async

            value = argument or await ui.read("Import JSONL path")
            if value:
                path = Path(value).expanduser()
                if path.stat().st_size > 20 * 1024**2:
                    raise ValueError("Import exceeds 20 MiB.")
                chat = await import_session_async(
                    store, path.read_text(), workspace=session.workspace
                )
                await switch_session(ui, chat["id"])
    elif name == "compact":
        require_idle(ui)
        await session.compact(argument)
        ui.notice = "Context compacted. The full transcript is retained."
    elif name == "queue":
        values = session.queued_inputs
        selected = await ui.select(
            "Queued input",
            [(item["id"], f"{item['kind']} · {item['text'][:100]}") for item in values],
        )
        item = next((item for item in values if item["id"] == selected), None)
        if item:
            value = await ui.read("Edit queued message · empty removes", item["text"])
            if value is not None:
                if value.strip():
                    session.edit_queued(selected, value)
                else:
                    session.remove_queued(selected)
    elif name in {"attach", "attachments", "paste-image"}:
        require_idle(ui)
        store = getattr(session, "store", None)
        if name == "paste-image":
            data = await asyncio.to_thread(read_clipboard_image)
            with tempfile.TemporaryDirectory(prefix="vikram-clipboard-") as directory:
                path = Path(directory) / "Clipboard image.png"
                path.write_bytes(data)
                path.chmod(0o600)
                await session.attach_file(path)
            ui.notice = image_notice(ui, "Clipboard image.png")
        elif name == "attachments":
            items = (
                [
                    item
                    for item in store.chat(session.chat_id)["attachments"]
                    if item["message_id"] is None
                ]
                if store
                else session.attachments
            )
            selected = await ui.select(
                "Pending attachments · select to remove",
                [(item["id"], item["name"]) for item in items],
            )
            if selected:
                if store:
                    store.remove_image(session.chat_id, selected)
                else:
                    session.attachments = [
                        item for item in items if item["id"] != selected
                    ]
        else:
            value = argument or await ui.read("Document or image path")
            if value:
                paths = dropped_paths(value) or [
                    Path(name).expanduser() for name in shlex.split(value)
                ]
                await attach_paths(ui, paths)
    elif name == "workspace":
        require_idle(ui)
        value = argument or await ui.read(
            "Workspace directory", str(session.workspace or Path.cwd())
        )
        if value:
            path = Path(value).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise ValueError("Choose a directory.")
            session.workspace = str(path)
            ui.settings.workspace = path
            if session.store:
                session.store.update_chat(session.chat_id, workspace=str(path))
            await configure_coding(ui)
            ui.apply_preferences()
            ui.notice = "Workspace: " + str(path)
    elif name == "trust":
        require_idle(ui)
        value = await ui.select(
            "Workspace trust grants host user permissions; it is not a sandbox",
            [
                ("no", "Keep inspection-only / revoke trust"),
                ("yes", "Trust " + str(ui.settings.workspace)),
            ],
        )
        if value:
            (ui.settings.trust if value == "yes" else ui.settings.untrust)()
            await configure_coding(ui)
            ui.apply_preferences()
            ui.notice = (
                "Workspace trusted." if value == "yes" else "Workspace trust removed."
            )
    elif name in {"login", "logout"}:
        from vikram.providers import CredentialStore

        require_idle(ui)
        credentials = CredentialStore()
        if name == "login":
            value = await ui.read(
                "OpenRouter API key · saved in macOS Keychain", password=True
            )
            if value:
                await asyncio.to_thread(credentials.set, "openrouter", value)
                ui.cloud_enabled = True
                ui.notice = (
                    "OpenRouter connected. Select a cloud model explicitly with /model."
                )
        else:
            await asyncio.to_thread(credentials.delete, "openrouter")
            ui.cloud_enabled = False
            ui.notice = "Saved OpenRouter key removed. An environment key, if set, must be removed from your shell."
    elif name == "reload":
        require_idle(ui)
        ui.settings.load()
        await configure_coding(ui)
        ui.apply_preferences()
        ui.notice = "Reloaded trusted resources and preferences."
    elif name == "commands":
        await extension_catalog(ui)
        selected = await ui.select(
            "Extension commands",
            [
                (name, name + " · " + item.get("description", ""))
                for name, item in ui.extension_commands.items()
            ],
        )
        if selected:
            await extension_command(ui, selected, argument)
    elif name in {"mcp", "extensions", "packages"}:
        from vikram.integrations import manage_terminal

        await manage_terminal(
            ui, "extensions" if name == "packages" else name, argument
        )
        await extension_catalog(ui)
    ui.app.invalidate()
