"""Versioned JSON-lines input validation for the private Rust/Python channel."""

from typing import Any, Literal

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
            for key in ("title", "model", "thinking")
            if key in fields
        ):
            raise ValueError("Invalid chat settings.")
    if cmd.method == "preference":
        key, value = cmd.params["key"], cmd.params["value"]
        if key == "appearance" and value not in ("system", "light", "dark"):
            raise ValueError("Choose system, light, or dark appearance.")
        if key == "sidebar" and not isinstance(value, bool):
            raise ValueError("Invalid sidebar preference.")
    return cmd
