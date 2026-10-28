"""Single generation slot, captured request settings, cancellation and recovery."""

import asyncio
import base64
import contextlib
import json
import sqlite3
from collections.abc import Callable
from uuid import uuid4

from vikram.agent import DEFAULT_MODEL
from vikram.browser import ACTION_TIMEOUT, BrowserUnavailable
from vikram.documents import ensure_space, extract_in_worker
from vikram.errors import reply_error
from vikram.interaction import (
    AskUserAnswer,
    AskUserResponse,
    PlanReview,
    check_response,
)
from vikram.models import prepare_model
from vikram.schedules import (
    Agent,
    Schedule,
    ScheduleStore,
    TaskBook,
    ensure_agent,
    start_runner,
)

from .history import inference_messages
from .inference import stream_reply
from .models import ModelCatalog
from .store import Store


def _plain(text: str) -> str:
    # Typed answers reach the model and the terminal: keep only newlines as controls.
    return "".join(c for c in text.replace("\t", " ") if c == "\n" or c.isprintable())


class DesktopInteraction:
    """Questions, plan reviews, and browser approvals for one attempt, answered
    with `respond`."""

    def __init__(self, runtime: "Runtime", route: dict):
        self.runtime, self.route = runtime, route

    async def ask(self, request, /):
        return await self.runtime.wait_for_person(self.route, "question", request)

    async def review_plan(self, request, /):
        return await self.runtime.wait_for_person(self.route, "plan", request)

    async def approve(self, request, /):
        # A browser action, or a scheduled task the model wants to save.
        return await self.runtime.wait_for_person(self.route, request.kind, request)


CLOSED = {
    "ok": False,
    "error": (
        "The user closed the browser. Do not use it again in this reply; answer "
        "with what you have."
    ),
}


class DesktopBrowser:
    """The app's browser panel, driven through the Tauri host.

    Each action is a `browser` event that the host runs on its WebKit panel; the
    result returns with `browser_result`. The person can take the panel over
    (actions wait until they hand it back) or close it, which ends its private
    session and the browser's use for the rest of that reply.
    """

    def __init__(self, runtime: "Runtime"):
        self.runtime = runtime
        self.calls: dict[str, tuple[str | None, asyncio.Future]] = {}
        self.control = "agent"
        self.closed_for: set[str] = set()
        self._agent = asyncio.Event()
        self._agent.set()

    @property
    def paused(self) -> bool:
        return self.control == "person"

    async def resumed(self) -> None:
        await self._agent.wait()

    def _attempt(self) -> str | None:
        return self.runtime.active["attempt_id"] if self.runtime.active else None

    async def act(self, call: dict, /) -> dict:
        attempt = self._attempt()
        if attempt in self.closed_for:
            return CLOSED
        key = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.calls[key] = (attempt, future)
        try:
            self.runtime.event("browser", call_id=key, call=call)
            async with asyncio.timeout(ACTION_TIMEOUT):
                return await future
        except TimeoutError:
            raise BrowserUnavailable("The browser did not respond.") from None
        finally:
            self.calls.pop(key, None)

    def result(self, call_id: str, result: dict) -> None:
        # A stopped reply no longer waits; its late result is dropped.
        entry = self.calls.get(call_id)
        if entry is not None and not entry[1].done():
            entry[1].set_result(result)

    def set_control(self, control: str) -> None:
        self.control = control
        if control == "agent":
            self._agent.set()
        else:
            self._agent.clear()
        self.runtime.event("browser_control", control=control)

    def closed(self) -> None:
        """The person closed the panel: fail waiting actions, end this reply's use."""
        if (attempt := self._attempt()) is not None:
            self.closed_for = {attempt}
        for _, future in self.calls.values():
            if not future.done():
                future.set_result(CLOSED)
        if self.control != "agent":
            self.set_control("agent")


class Runtime:
    """`browser` is true when the app hosts the browser panel (the real app);
    `tasks` lets its replies manage scheduled tasks (the real app)."""

    def __init__(
        self,
        store: Store,
        emit: Callable,
        streamer=stream_reply,
        browser=False,
        tasks=False,
        agent: Agent | None = None,
    ):
        self.store, self.emit, self.streamer = store, emit, streamer
        self.catalog = ModelCatalog()
        self.models: list[dict] = []
        self.active: dict | None = None
        self.task: asyncio.Task | None = None
        self.stopping = False
        self.starting = False
        self.waiting: dict[str, tuple] = {}
        self.browser = DesktopBrowser(self) if browser else None
        # Scheduled tasks live beside the chats, in their own file, because
        # background runs write to it while this process owns the chats.
        self._schedules: ScheduleStore | None = None
        self.tasks, self.agent = tasks, agent
        self.background = "unavailable"
        self.watcher: asyncio.Task | None = None
        self.start_runner = start_runner

    def event(self, event: str, **payload) -> None:
        self.emit({"v": 1, "event": event, **payload})

    @property
    def schedules(self) -> ScheduleStore:
        if self._schedules is None:
            self._schedules = ScheduleStore(self.store.root)
        return self._schedules

    def tasks_changed(self) -> None:
        """After a change here: match the agent to the tasks, tell the window."""
        self.background = ensure_agent(self.schedules, self.agent)
        self.event("schedules")

    async def watch_schedules(self, interval: float = 2.0) -> None:
        """Tell the window when a background run or the CLI changes tasks."""
        seen = self.schedules.data_version()
        while True:
            await asyncio.sleep(interval)
            try:
                version = self.schedules.data_version()
            except sqlite3.Error:
                continue
            if version != seen:
                seen = version
                self.event("schedules")

    def schedule_list(self) -> dict:
        return {"tasks": self.schedules.tasks(), "background": self.background}

    async def close(self) -> None:
        if self.watcher is not None:
            self.watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.watcher
        if self._schedules is not None:
            self._schedules.close()

    async def refresh(self) -> dict:
        try:
            self.models = await self.catalog.refresh()
            return {"models": self.models, "connection_error": None}
        except ValueError as e:
            self.models = []
            return {"models": [], "connection_error": str(e)}

    def snapshot(self) -> dict:
        return {
            **self.store.snapshot(),
            "active": self.active,
            "models": self.models,
            "browser": (
                {"control": self.browser.control} if self.browser is not None else None
            ),
        }

    def suggested_model(self) -> str | None:
        installed = {m["name"] for m in self.models if m["available"]}
        last = self.store.snapshot()["preferences"].get("last_model")
        return next((m for m in (last, DEFAULT_MODEL) if m in installed), None)

    async def send(self, chat_id: str, request_id: str, retry: bool = False) -> dict:
        if self.active or self.starting:
            raise ValueError(
                "Another reply is still running. Stop it or wait for it to finish."
            )
        self.starting = True
        try:
            # Capture selection before awaiting metadata: changes affect only the next reply.
            chat = self.store.chat(chat_id)
            self.models = await self.catalog.refresh()
            selected, setting, thinking, web, notice = prepare_model(
                self.models,
                chat["model"],
                chat["thinking"].get(chat["model"]),
            )
            if (
                not retry
                and not selected["vision"]
                and any(
                    a["message_id"] is None and a["kind"] == "image"
                    for a in chat["attachments"]
                )
            ):
                raise ValueError(
                    "This model accepts text only. Remove draft images or choose an image-capable model."
                )
            if notice:
                self.event("notice", chat_id=chat_id, message=notice)
            # Save only a reset choice; the default stays "on" as models change.
            if setting != chat["thinking"].get(chat["model"]):
                self.store.update_chat(
                    chat_id, thinking={**chat["thinking"], chat["model"]: setting}
                )
            attempt = self.store.begin(
                chat_id,
                selected["name"],
                thinking,
                request_id,
                retry,
                web=web,
                mode=chat["mode"],
            )
            self.active = {
                "chat_id": chat_id,
                "attempt_id": attempt["id"],
                "request_id": request_id,
            }
            self.stopping = False
            self.task = asyncio.create_task(
                self._generate(attempt, selected, thinking, setting)
            )
            return {
                "attempt": attempt,
                "chat": self.store.chat(chat_id),
                "active": self.active,
                "models": self.models,
            }
        finally:
            self.starting = False

    async def _generate(
        self, attempt: dict, model: dict, thinking, setting=None
    ) -> None:
        route = {
            "chat_id": attempt["chat_id"],
            "attempt_id": attempt["id"],
            "request_id": attempt["request_id"],
        }
        content, actual, status, error, unsaved = "", None, "complete", None, False
        revision = attempt["revision"]
        self.event(
            "attempt",
            **route,
            content="",
            revision=revision,
            actual_model=None,
            status="running",
            phase="loading",
        )
        sources, searches = [], []
        reasoning, reasoning_truncated = "", False
        activity, saved_activity, mode = [], "[]", attempt["mode"]
        iterator = None
        try:
            history, prompt = inference_messages(
                self.store, attempt["chat_id"], attempt["message_id"], model["vision"]
            )
            iterator = self.streamer(
                model["name"],
                thinking,
                history,
                prompt,
                web=attempt["web"],
                session_id=attempt["chat_id"],
                mode=attempt["mode"],
                interaction=DesktopInteraction(self, route),
                browser=self.browser,
                vision=bool(model.get("vision")),
                tasks=(
                    TaskBook(
                        store=self.schedules,
                        model=model["name"],
                        thinking=setting,
                        origin="chat",
                        changed=self.tasks_changed,
                    )
                    if self.tasks
                    else None
                ),
            )
            async for chunk in iterator:
                if self.stopping or self.active != route:
                    break
                content, actual = chunk["text"], chunk.get("actual_model") or actual
                reasoning = chunk.get("reasoning", reasoning)
                reasoning_truncated = chunk.get(
                    "reasoning_truncated", reasoning_truncated
                )
                sources, searches = chunk.get("sources", []), chunk.get("searches", [])
                activity, mode = chunk.get("activity", activity), chunk.get(
                    "mode", mode
                )
                encoded = json.dumps(activity)
                revision += 1
                try:
                    self.store.checkpoint(
                        attempt["id"],
                        content,
                        actual,
                        sources=sources,
                        searches=searches,
                        revision=revision,
                        reasoning=reasoning,
                        reasoning_truncated=reasoning_truncated,
                        # Code and plans can be long: write them only when they change.
                        activity=activity if encoded != saved_activity else None,
                    )
                    saved_activity = encoded
                except (sqlite3.Error, OSError):
                    unsaved = True
                    raise
                self.event(
                    "attempt",
                    **route,
                    content=content,
                    actual_model=actual,
                    status="running",
                    phase=chunk["phase"],
                    sources=sources,
                    searches=searches,
                    revision=revision,
                    reasoning=reasoning,
                    reasoning_truncated=reasoning_truncated,
                    activity=activity,
                    mode=mode,
                )
            if self.stopping:
                status = "stopped"
            elif not content:
                status, error = (
                    "failed",
                    "The model finished without an answer. You can retry.",
                )
        except asyncio.CancelledError:
            status = "stopped"
        except (sqlite3.Error, OSError):
            status, unsaved, error = (
                "interrupted",
                True,
                "Could not save this reply. Copy the unsaved text, free disk space, then reconnect.",
            )
        except Exception as exc:
            status, error = "failed", reply_error(exc)
        finally:
            if iterator is not None:
                try:
                    await iterator.aclose()
                except Exception:
                    pass
            try:
                unfinished = {"running", "waiting"}
                if any(s["status"] in unfinished for s in [*searches, *activity]):
                    revision += 1
                    searches, activity = [
                        [
                            (
                                {
                                    **s,
                                    "status": (
                                        status
                                        if status != "complete"
                                        else "interrupted"
                                    ),
                                }
                                if s["status"] in unfinished
                                else s
                            )
                            for s in records
                        ]
                        for records in (searches, activity)
                    ]
                    self.store.checkpoint(
                        attempt["id"],
                        content,
                        actual,
                        sources=sources,
                        searches=searches,
                        revision=revision,
                        activity=activity,
                    )
                self.store.finish(attempt["id"], status, error)
                if attempt["mode"] == "plan" and mode == "normal":
                    # An approved plan ends plan mode for the chat's next replies.
                    self.store.update_chat(attempt["chat_id"], mode="normal")
                    self.event("chat_mode", **route, mode="normal")
            except (sqlite3.Error, OSError):
                unsaved, status, error = (
                    True,
                    "interrupted",
                    "Could not save this reply. Copy its text before reconnecting.",
                )
            self.event(
                "attempt",
                **route,
                content=content,
                actual_model=actual,
                status=status,
                error=error,
                unsaved=unsaved,
                sources=sources,
                searches=searches,
                revision=revision,
                reasoning=reasoning,
                reasoning_truncated=reasoning_truncated,
                activity=activity,
                mode=mode,
            )
            # This release happens only after the stream context has closed.
            self.active = None
            self.stopping = False
            self.event("idle", **route)

    async def wait_for_person(self, route: dict, kind: str, request):
        """Park the reply until the window answers `request` with `respond`."""
        future = asyncio.get_running_loop().create_future()
        self.waiting[request.id] = (route["attempt_id"], kind, request, future)
        try:
            return await future
        finally:
            self.waiting.pop(request.id, None)

    def respond(self, p: dict) -> None:
        entry = self.waiting.get(p["interaction_id"])
        if (
            entry is None
            or not self.active
            or self.active["chat_id"] != p["chat_id"]
            or entry[0] != p["attempt_id"]
            or entry[1] != p["kind"]
            or entry[3].done()
        ):
            raise ValueError("This reply is no longer waiting for that answer.")
        _, kind, request, future = entry
        if kind in ("browser", "schedule"):
            if p.get("decision") not in ("approve", "dismiss"):
                raise ValueError(
                    "Allow or decline the browser action."
                    if kind == "browser"
                    else "Save or decline the scheduled task."
                )
            future.set_result(p["decision"] == "approve")
            return
        if kind == "question":
            response = AskUserResponse(
                cancelled=p.get("cancelled", False),
                answers=(
                    ()
                    if p.get("cancelled")
                    else tuple(
                        AskUserAnswer(
                            header=answer["header"],
                            selected=tuple(answer.get("selected", ())),
                            custom_answer=(
                                _plain(answer["custom_answer"]).strip()
                                if answer.get("custom_answer") is not None
                                else None
                            ),
                        )
                        for answer in p.get("answers", [])
                    )
                ),
            )
            try:
                check_response(request, response)
            except ValueError:
                raise ValueError(
                    "Choose an option or type an answer for each question."
                ) from None
        else:
            feedback = _plain(p.get("feedback") or "").strip() or None
            if p.get("decision") is None or (
                p["decision"] == "revise" and feedback is None
            ):
                raise ValueError("Describe the changes you want to the plan.")
            response = PlanReview(decision=p["decision"], feedback=feedback)
        future.set_result(response)

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.stopping = True
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                # A task cancelled before its first instruction never runs its finally.
                if self.active:
                    route = self.active
                    status, unsaved, error = "stopped", False, None
                    try:
                        self.store.finish(route["attempt_id"], status)
                    except (OSError, sqlite3.Error):
                        status, unsaved, error = (
                            "interrupted",
                            True,
                            "Could not save the stopped reply. Reconnect after checking disk space.",
                        )
                    self.event(
                        "attempt",
                        **route,
                        content="",
                        status=status,
                        unsaved=unsaved,
                        error=error,
                    )
                    self.active = None
                    self.stopping = False
                    self.event("idle", **route)

    async def command(self, method: str, p: dict, request_id: str):
        if method.endswith("_schedule") or method in {
            "schedules",
            "schedule",
            "schedule_run",
            "open_run",
        }:
            return await self.schedule_command(method, p)
        if method == "hello":
            return {"protocol": 1, **self.snapshot()}
        if method == "refresh":
            return await self.refresh()
        if method == "snapshot":
            return self.snapshot()
        if method == "chat":
            return self.store.chat(p["chat_id"])
        if method == "create_chat":
            return self.store.create_chat(self.suggested_model(), p.get("folder_id"))
        if method == "update_chat":
            fields = p["fields"]
            if "model" in fields:
                if not any(
                    m["name"] == fields["model"] and m["available"] for m in self.models
                ):
                    raise ValueError("Choose an available local model.")
                self.store.set_preference("last_model", fields["model"])
            return self.store.update_chat(p["chat_id"], **fields)
        if method == "draft":
            self.store.save_draft(p["chat_id"], p["text"], p["revision"])
        elif method == "preference":
            self.store.set_preference(p["key"], p["value"])
        elif method == "create_folder":
            return self.store.create_folder(p["name"])
        elif method == "rename_folder":
            self.store.rename_folder(p["folder_id"], p["name"])
        elif method == "delete_folder":
            self.store.delete_folder(p["folder_id"])
        elif method == "delete_chat":
            if self.active and self.active["chat_id"] == p["chat_id"]:
                raise ValueError("Stop the active reply before deleting this chat.")
            self.store.delete_chat(p["chat_id"])
        elif method in {"import_image", "import_attachment"}:
            try:
                data = base64.b64decode(p["data"], validate=True)
            except ValueError:
                raise ValueError("The image could not be imported.") from None
            ensure_space(self.store.root, len(data) + 50 * 1024**2)
            if method == "import_image" or p["name"].lower().endswith(
                (".png", ".jpg", ".jpeg", ".webp")
            ):
                return self.store.import_image(p["chat_id"], p["name"], data)
            extracted = await extract_in_worker(p["name"], data)
            return self.store.import_attachment(
                p["chat_id"], p["name"], data, extracted=extracted
            )
        elif method == "attachment":
            return self.store._one(
                "SELECT * FROM attachments WHERE id=? AND chat_id=?",
                (p["attachment_id"], p["chat_id"]),
            )
        elif method == "image":
            data = self.store.image_bytes(p["chat_id"], p["attachment_id"])
            item = self.store._one(
                "SELECT media_type FROM attachments WHERE id=?", (p["attachment_id"],)
            )
            return f"data:{item['media_type']};base64,{base64.b64encode(data).decode()}"
        elif method in {"remove_image", "remove_attachment"}:
            self.store.remove_image(p["chat_id"], p["attachment_id"])
        elif method == "send":
            return await self.send(
                p["chat_id"], p.get("request_id", request_id), p.get("retry", False)
            )
        elif method == "stop":
            await self.stop()
        elif method == "respond":
            self.respond(p)
        elif method in {"browser_control", "browser_result", "browser_closed"}:
            if self.browser is None:
                raise ValueError("This app has no browser.")
            if method == "browser_control":
                self.browser.set_control(p["control"])
                return {"control": self.browser.control}
            if method == "browser_result":
                self.browser.result(p["call_id"], p["result"])
            else:
                self.browser.closed()
        else:
            raise ValueError("Unknown command.")
        return None

    async def schedule_command(self, method: str, p: dict):
        """The window's scheduled-task commands; runs happen in the background."""
        tasks = self.schedules
        if self.watcher is None:
            # The window asks for the task list when it opens.
            self.watcher = asyncio.create_task(self.watch_schedules())
            # Keep an existing agent current, e.g. after an update.
            self.background = ensure_agent(tasks, self.agent)
        if method == "schedules":
            return self.schedule_list()
        if method == "schedule":
            return tasks.task(p["task_id"])
        if method == "schedule_run":
            return tasks.run(p["run_id"])
        if method == "open_run":
            run = tasks.run(p["run_id"])
            if run["status"] == "running":
                raise ValueError("Wait for this run to finish.")
            task = tasks.task(run["task_id"])
            chat = self.store.import_run(
                task["name"],
                run["prompt"],
                run,
                model=run["actual_model"] or run["model"] or task["model"],
            )
            return chat
        if method in {"create_schedule", "update_schedule"}:
            fields = dict(p.get("fields", p))
            fields.pop("task_id", None)
            if "schedule" in fields:
                fields["schedule"] = Schedule.model_validate(fields["schedule"])
            if fields.get("model") is not None and not any(
                m["name"] == fields["model"] and m["available"] for m in self.models
            ):
                raise ValueError("Choose an available local model.")
            if method == "create_schedule":
                enabled = fields.pop("enabled", True)
                result = tasks.create(origin="app", **fields)
                if not enabled:
                    result = tasks.update(result["id"], enabled=False)
            else:
                result = tasks.update(p["task_id"], **fields)
        elif method == "delete_schedule":
            tasks.delete(p["task_id"])
            result = None
        elif method == "run_schedule":
            result = tasks.request_run(p["task_id"])
            try:
                self.start_runner()
            except OSError:
                raise ValueError(
                    "The background runner could not start. The run will start "
                    "within a minute if background runs are on."
                ) from None
        else:
            raise ValueError("Unknown command.")
        self.tasks_changed()
        return result
