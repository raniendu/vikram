"""Versioned JSON-lines input validation for the private Rust/Python channel."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    v: Literal[1]
    id: str = Field(min_length=1, max_length=80)
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Chat(Params):
    chat_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class NewChat(Params):
    folder_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class ChatFields(Params):
    title: str | None = Field(default=None, max_length=160)
    folder_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    model: str | None = Field(default=None, max_length=256)
    thinking: dict[str, StrictBool | StrictStr | None] | None = None
    mode: Literal["normal", "plan"] | None = None


class UpdateChat(Chat):
    fields: ChatFields


class Draft(Chat):
    text: str = Field(max_length=1_000_000)
    revision: StrictInt = Field(ge=0)


class Preference(Params):
    key: Literal["appearance", "selected_chat", "sidebar"]
    value: StrictStr | StrictBool | None


class Name(Params):
    name: str = Field(min_length=1, max_length=80)


class Folder(Params):
    folder_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class RenameFolder(Folder, Name):
    pass


class ImportImage(Chat):
    name: str = Field(max_length=180)
    data: str = Field(max_length=14_000_000)


class Image(Chat):
    attachment_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class Send(Chat):
    retry: bool = False
    request_id: str = Field(min_length=1, max_length=80)


class Answer(Params):
    header: str = Field(min_length=1, max_length=25)
    selected: list[Annotated[str, Field(max_length=50)]] = Field(
        default_factory=list, max_length=6
    )
    custom_answer: str | None = Field(default=None, max_length=2000)


class Respond(Chat):
    """A person's answer to the active reply's question, plan, browser action,
    or scheduled task to save.

    For a browser action or a task, "approve" allows it and "dismiss" declines it.
    """

    attempt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    interaction_id: str = Field(min_length=1, max_length=120)
    kind: Literal["question", "plan", "browser", "schedule"]
    answers: list[Answer] = Field(default_factory=list, max_length=10)
    cancelled: bool = False
    decision: Literal["approve", "revise", "dismiss"] | None = None
    feedback: str | None = Field(default=None, max_length=2000)


class BrowserResult(Params):
    """The app's answer to one browser action; the toolkit validates `result`."""

    call_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    result: dict[str, Any]


class BrowserControl(Params):
    """The person takes the browser over, or hands it back to Vikram."""

    control: Literal["person", "agent"]


TASK_ID = Field(pattern=r"^[0-9a-f]{32}$")


class ScheduleTime(Params):
    """Validated again by `vikram.schedules.Schedule`."""

    model_config = ConfigDict(extra="forbid", strict=False)
    repeat: Literal["once", "daily", "weekly", "monthly", "interval"]
    time: str | None = Field(default=None, max_length=5)
    date: str | None = Field(default=None, max_length=10)
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] | None = Field(
        default=None, max_length=7
    )
    day_of_month: StrictInt | None = Field(default=None, ge=1, le=31)
    every_minutes: StrictInt | None = Field(default=None, ge=15, le=7 * 24 * 60)


class TaskFields(Params):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    prompt: str | None = Field(default=None, min_length=1, max_length=4000)
    schedule: ScheduleTime | None = None
    model: str | None = Field(default=None, max_length=256)
    thinking: StrictBool | StrictStr | None = None
    enabled: StrictBool | None = None


class NewTask(Params):
    fields: TaskFields


class Task(Params):
    task_id: str = TASK_ID


class UpdateTask(Task):
    fields: TaskFields


class Run(Params):
    run_id: str = TASK_ID


PARAMS = {
    "hello": Params,
    "snapshot": Params,
    "refresh": Params,
    "stop": Params,
    "chat": Chat,
    "create_chat": NewChat,
    "update_chat": UpdateChat,
    "draft": Draft,
    "preference": Preference,
    "create_folder": Name,
    "rename_folder": RenameFolder,
    "delete_folder": Folder,
    "delete_chat": Chat,
    "import_image": ImportImage,
    "import_attachment": ImportImage,
    "attachment": Image,
    "remove_attachment": Image,
    "image": Image,
    "remove_image": Image,
    "send": Send,
    "respond": Respond,
    "browser_control": BrowserControl,
    "schedules": Params,
    "schedule": Task,
    "schedule_run": Run,
    "create_schedule": NewTask,
    "update_schedule": UpdateTask,
    "delete_schedule": Task,
    "run_schedule": Task,
    "open_run": Run,
    "update_status": Params,
    "check_update": Params,
    # From the app itself, never the window: results and the panel closing.
    "browser_result": BrowserResult,
    "browser_closed": Params,
}


def validate_command(raw: str) -> Command:
    return validate_parameters(Command.model_validate_json(raw))


def validate_parameters(cmd: Command) -> Command:
    if cmd.method not in PARAMS:
        raise ValueError("Unknown command.")
    cmd.params = (
        PARAMS[cmd.method].model_validate(cmd.params).model_dump(exclude_unset=True)
    )
    if cmd.method == "update_chat":
        fields = cmd.params["fields"]
        if any(
            fields.get(key) is None
            for key in ("title", "model", "thinking", "mode")
            if key in fields
        ):
            raise ValueError("Invalid chat settings.")
    if cmd.method in {"create_schedule", "update_schedule"}:
        fields = cmd.params["fields"]
        if cmd.method == "create_schedule" and not {
            "name",
            "prompt",
            "schedule",
            "model",
        } <= set(fields):
            raise ValueError(
                "A scheduled task needs a name, prompt, schedule, and model."
            )
        if not fields or any(
            fields.get(key) is None
            for key in ("name", "prompt", "schedule", "enabled")
            if key in fields
        ):
            raise ValueError("Invalid scheduled task.")
        if "schedule" in fields:
            fields["schedule"] = {
                key: value
                for key, value in fields["schedule"].items()
                if value is not None
            }
    if cmd.method == "preference":
        key, value = cmd.params["key"], cmd.params["value"]
        if key == "appearance" and value not in ("system", "light", "dark"):
            raise ValueError("Choose system, light, or dark appearance.")
        if key == "sidebar" and not isinstance(value, bool):
            raise ValueError("Invalid sidebar preference.")
    return cmd
