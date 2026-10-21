"""Single generation slot, captured request settings, cancellation and recovery."""

import asyncio
import base64
import json
import sqlite3
from collections.abc import Callable

from vikram.agent import DEFAULT_MODEL
from vikram.documents import ensure_space, extract_in_worker
from vikram.errors import reply_error
from vikram.interaction import (
    AskUserAnswer,
    AskUserResponse,
    PlanReview,
    check_response,
)
from vikram.models import prepare_model

from .history import inference_messages
from .inference import stream_reply
from .models import ModelCatalog
from .store import Store


def _plain(text: str) -> str:
    # Typed answers reach the model and the terminal: keep only newlines as controls.
    return "".join(c for c in text.replace("\t", " ") if c == "\n" or c.isprintable())


class DesktopInteraction:
    """Questions and plan reviews for one attempt, answered with `respond`."""

    def __init__(self, runtime: "Runtime", route: dict):
        self.runtime, self.route = runtime, route

    async def ask(self, request, /):
        return await self.runtime.wait_for_person(self.route, "question", request)

    async def review_plan(self, request, /):
        return await self.runtime.wait_for_person(self.route, "plan", request)


class Runtime:
    def __init__(self, store: Store, emit: Callable, streamer=stream_reply):
        self.store, self.emit, self.streamer = store, emit, streamer
        self.catalog = ModelCatalog()
        self.models: list[dict] = []
        self.active: dict | None = None
        self.task: asyncio.Task | None = None
        self.stopping = False
        self.starting = False
        self.waiting: dict[str, tuple] = {}

    def event(self, event: str, **payload) -> None:
        self.emit({"v": 1, "event": event, **payload})

    async def refresh(self) -> dict:
        try:
            self.models = await self.catalog.refresh()
            return {"models": self.models, "connection_error": None}
        except ValueError as e:
            self.models = []
            return {"models": [], "connection_error": str(e)}

    def snapshot(self) -> dict:
        return {**self.store.snapshot(), "active": self.active, "models": self.models}

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
            self.task = asyncio.create_task(self._generate(attempt, selected, thinking))
            return {
                "attempt": attempt,
                "chat": self.store.chat(chat_id),
                "active": self.active,
                "models": self.models,
            }
        finally:
            self.starting = False

    async def _generate(self, attempt: dict, model: dict, thinking) -> None:
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
        else:
            raise ValueError("Unknown command.")
        return None
