"""Shared workbench operations exposed by the private desktop/stdin protocol."""

import asyncio
from pathlib import Path

from vikram.coding import CodingSession
from vikram.providers import CredentialError, CredentialStore
from vikram.resources import ProjectResources
from vikram.settings import Settings


class Workbench:
    def __init__(self, runtime):
        from vikram.providers import ModelRef

        self.runtime = runtime
        default = Settings(root=runtime.store.root).get("model")
        self.cloud_enabled = bool(default and ModelRef.parse(default).is_cloud)
        self.services = {}
        self.contexts = {}

    def settings(self, chat_id=None):
        chat = self.runtime.store.chat(chat_id) if chat_id else {}
        return Settings(
            root=self.runtime.store.root,
            workspace=Path(chat["workspace"]) if chat.get("workspace") else None,
        )

    def default_model(self):
        from vikram.agent import DEFAULT_MODEL

        return (
            Settings(root=self.runtime.store.root).get("model")
            or self.runtime.store.snapshot()["preferences"].get("last_model")
            or DEFAULT_MODEL
        )

    def context(self, chat_id, *, selected=None):
        from vikram.providers import ModelRef
        from vikram.session import Session, stored_turns

        chat = self.runtime.store.chat(chat_id)
        session = self.contexts.get(chat_id)
        if session is None:
            session = self.contexts[chat_id] = Session(
                chat["model"] or "", streamer=self.runtime.streamer
            )
        if selected is None and chat["model"]:
            requested = ModelRef.parse(chat["model"])
            selected = next(
                (
                    m
                    for m in self.runtime.models
                    if ModelRef.parse(m["name"]) == requested
                ),
                {},
            )
        selected = selected or {}
        session.model, session.mode = (
            selected.get("name", chat["model"] or ""),
            chat["mode"],
        )
        session.session_id = chat_id
        session.turns = stored_turns(self.runtime.store, chat_id)
        session.workspace = chat["workspace"]
        session.summary = chat["context_summary"]
        session._summary_count = next(
            (
                index + 1
                for index, turn in enumerate(session.turns)
                if turn["id"] == chat["summary_through"]
            ),
            0,
        )
        limit = selected.get("context_length")
        session.context_limit = limit if type(limit) is int and limit > 0 else None
        return session

    async def compact(
        self,
        chat_id,
        *,
        prompt="",
        selected=None,
        manual=False,
        instructions="",
        include_draft=True,
    ):
        import math

        settings = self.settings(chat_id)
        if not manual and not settings.get("auto_compact", True):
            return self.context(chat_id, selected=selected).context_state()
        with self.runtime.store.mutation(chat_id):
            context = self.context(chat_id, selected=selected)
            incoming = math.ceil(len(prompt) / 4)
            if include_draft:
                chat = self.runtime.store.chat(chat_id)
                incoming += sum(
                    (
                        2048
                        if item["kind"] == "image"
                        else math.ceil(len(item.get("extracted_text") or "") / 4)
                    )
                    for item in chat["attachments"]
                    if item["message_id"] is None
                )
            force = manual or (
                context.context_limit is not None
                and context.context_state()["estimated_tokens"] + incoming
                >= context.context_limit * 0.8
            )
            previous = (context.summary, context._summary_count)
            try:
                result = await context.compact(instructions, force=force)
                if (context.summary, context._summary_count) != previous:
                    cutoff = (
                        context.turns[context._summary_count - 1]["id"]
                        if context._summary_count
                        else None
                    )
                    self.runtime.store.set_summary(chat_id, context.summary, cutoff)
                    self.runtime.event("workbench", chat_id=chat_id)
                return result
            except BaseException:
                context.summary, context._summary_count = previous
                raise

    def coding(self, chat_id):
        if chat_id not in self.services:
            settings = self.settings(chat_id)
            self.services[chat_id] = CodingSession(
                settings.workspace or self.runtime.store.root, settings=settings
            )
        self.services[chat_id].mode = self.runtime.store.chat(chat_id)["mode"]
        return self.services[chat_id]

    async def close(self):
        from vikram.integrations import close

        for coding in self.services.values():
            await close(coding)
        self.services.clear()

    def boundary(self, chat_id, attempt_id):
        context = self.context(chat_id)
        pending = [item for item in context.queued_inputs if item["kind"] == "steer"]
        texts = [item["text"] for item in pending]
        if texts:
            self.runtime.store.append_steering(attempt_id, texts)
            for item in pending:
                context.remove_queued(item["id"])
            self.runtime.event("workbench", chat_id=chat_id)
        return texts

    async def command(self, params):
        readonly = {"status", "queue", "queue_edit", "queue_remove", "tree"}
        if params["action"] in readonly:
            return await self._command(params)
        if self.runtime.active or self.runtime.starting:
            raise ValueError(
                "Stop the active reply before changing workbench settings."
            )
        self.runtime.starting = True
        self.runtime.start_task = asyncio.current_task()
        try:
            return await self._command(params)
        finally:
            self.runtime.starting = False
            self.runtime.start_task = None

    async def _command(self, params):
        action = params["action"]
        cid, value, options = (
            params.get("chat_id"),
            params.get("value", ""),
            params.get("options", {}),
        )
        store = self.runtime.store
        settings = self.settings(cid)
        if action == "status":
            from vikram.integrations import start, status

            context = self.context(cid) if cid else None
            coding = self.services.get(cid)
            if cid and (
                settings.get("mcp_servers")
                or (settings.root / "extensions/registry.json").exists()
            ):
                coding = self.coding(cid)
                await start(coding)
            try:
                credential_source = CredentialStore().source("openrouter")
            except CredentialError:
                credential_source = None
            return {
                "default_model": self.default_model(),
                "workspace": str(settings.workspace) if settings.workspace else None,
                "trusted": settings.is_trusted() if settings.workspace else False,
                "provider": (
                    "openrouter"
                    if context and context.model.startswith("openrouter:")
                    else "ollama"
                ),
                "cloud_enabled": self.cloud_enabled,
                "credentials": credential_source,
                "context": context.context_state() if context else {},
                "queue": context.queued_inputs if context else [],
                "resources": {
                    "skills": [
                        item.name for item in ProjectResources(settings).reload().skills
                    ]
                },
                "mcp": (
                    await status(coding, "mcp")
                    if coding and hasattr(coding, "mcp")
                    else []
                ),
                "extensions": (
                    await status(coding, "extensions")
                    if coding and hasattr(coding, "extensions")
                    else []
                ),
            }
        if action == "default_model":
            from vikram.providers import ModelRef

            if not any(
                ModelRef.parse(item["name"]) == ModelRef.parse(value)
                and item["available"]
                for item in self.runtime.models
            ):
                raise ValueError("Choose an available model as the default.")
            Settings(root=store.root).set("model", value)
            return {"default_model": value}
        if action in {"login", "logout", "cloud"}:
            credentials = CredentialStore()
            if action == "login":
                credentials.set("openrouter", value)
                self.cloud_enabled = True
            elif action == "logout":
                credentials.delete("openrouter")
                self.cloud_enabled = False
            else:
                self.cloud_enabled = options.get("enabled") is True
            return await self.runtime.refresh()
        if action == "workspace":
            path = Path(value).expanduser().resolve(strict=True)
            if not path.is_dir():
                raise ValueError("Choose a directory.")
            if cid in self.services:
                from vikram.integrations import close

                await close(self.services.pop(cid))
            return store.update_chat(cid, workspace=str(path))
        if action == "trust":
            if settings.workspace is None:
                raise ValueError("Choose a workspace first.")
            (settings.trust if options.get("trusted") is True else settings.untrust)()
            return await self.command({"action": "status", "chat_id": cid})
        if action == "tree":
            return store.tree(cid)
        if action == "branch":
            return store.select_branch(cid, value or None)
        if action == "fork":
            return store.fork_chat(cid, value or None)
        if action == "clone":
            return store.clone_chat(cid)
        if action == "name":
            return store.update_chat(cid, title=value)
        if action == "compact":
            return await self.compact(
                cid, instructions=value, manual=True, include_draft=False
            )
        if action in {"queue", "queue_edit", "queue_remove"}:
            context = self.context(cid)
            if action == "queue":
                context.queue_input(value, kind=options.get("kind", "steer"))
            elif action == "queue_edit":
                context.edit_queued(value, options.get("text", ""))
            else:
                context.remove_queued(value)
            self.runtime.event("workbench", chat_id=cid)
            return context.queued_inputs
        if action == "export":
            from vikram.session_io import export_session

            format = options.get("format", "markdown")
            return {
                "content": export_session(store, cid, format=format),
                "format": format,
            }
        if action == "import":
            from vikram.session_io import import_session_async

            return await import_session_async(store, value)
        if action in {"mcp", "extensions"}:
            from vikram.integrations import manage

            return await manage(self.coding(cid), action, options)
        if action in {"extension_commands", "extension_command"}:
            from vikram.integrations import run_command, start

            coding = self.coding(cid)
            with store.mutation(cid):
                await start(coding)
                coding.extensions.mode = coding.mode
                if action == "extension_commands":
                    return await coding.extensions.list_commands()
                arguments = options.get("arguments", {})
                if not isinstance(arguments, dict):
                    raise ValueError("Extension command arguments must be an object.")
                return await run_command(
                    coding, value or options.get("name", ""), arguments
                )
        if action == "reload":
            if cid in self.services:
                from vikram.integrations import close

                await close(self.services.pop(cid))
            return await self.command({"action": "status", "chat_id": cid})
        raise ValueError("Unknown workbench operation.")
