"""Version 1 of Vikram's standalone Python extension API.

This module deliberately uses only the standard library. It is copied into
private extension environments; importing it does not import the chat engine.
Extension code has normal host user privileges and is not a security sandbox.
"""

import inspect
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

API_VERSION = 1
MAX_OUTPUT = 64_000
MAX_FRAME = 1024 * 1024
NAME = re.compile(r"(?!.*__)[a-z][a-z0-9_]{0,47}\Z")


def validate_value(value: Any, schema: dict, depth: int = 0) -> None:
    """Validate the SDK's bounded JSON Schema subset without a worker dependency."""
    if depth > 20 or not isinstance(schema, dict):
        raise ValueError("Invalid extension argument schema.")
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in {int, float},
        "boolean": type(value) is bool,
        "null": value is None,
        None: True,
    }
    if not any(matches.get(item, False) for item in kinds):
        raise ValueError("An extension argument has the wrong type.")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("An extension argument is not an allowed value.")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            raise ValueError("A required extension argument is missing.")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError("An unknown extension argument was supplied.")
        for key, item in value.items():
            if key in properties:
                validate_value(item, properties[key], depth + 1)
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", 1000) or len(value) < schema.get(
            "minItems", 0
        ):
            raise ValueError("An extension array has an invalid length.")
        for item in value:
            validate_value(item, schema.get("items", {}), depth + 1)
    if isinstance(value, str) and (
        len(value) > schema.get("maxLength", MAX_OUTPUT)
        or len(value) < schema.get("minLength", 0)
    ):
        raise ValueError("An extension string has an invalid length.")
    if type(value) in {int, float} and (
        value < schema.get("minimum", float("-inf"))
        or value > schema.get("maximum", float("inf"))
    ):
        raise ValueError("An extension number is outside the allowed range.")


def check_schema(schema: dict, depth: int = 0) -> None:
    allowed = {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "description",
        "title",
        "default",
    }
    if not isinstance(schema, dict) or depth > 20 or set(schema) - allowed:
        raise ValueError("The extension schema uses unsupported constraints.")
    if (
        "additionalProperties" in schema
        and type(schema["additionalProperties"]) is not bool
    ):
        raise ValueError("additionalProperties must be true or false.")
    if schema.get("type", "object") not in {
        "object",
        "array",
        "string",
        "integer",
        "number",
        "boolean",
        "null",
    }:
        raise ValueError("Unsupported extension schema type.")
    for child in schema.get("properties", {}).values():
        check_schema(child, depth + 1)
    if "items" in schema:
        check_schema(schema["items"], depth + 1)
    if len(json.dumps(schema)) > 32_000:
        raise ValueError("Extension schema is too large.")


def normalized_result(value: Any) -> dict:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, str):
        value = {"output": value}
    if value is None:
        value = {"output": ""}
    if not isinstance(value, dict):
        value = {"output": json.dumps(value, ensure_ascii=False)}
    output = value.get("output", "")
    if not isinstance(output, str):
        output = json.dumps(output, ensure_ascii=False)
    ui = value.get("ui", [])
    if not isinstance(ui, list) or len(ui) > 20:
        raise ValueError("Invalid extension UI declarations.")
    allowed = {
        "text": {"type", "text", "title"},
        "notice": {"type", "text", "level"},
        "table": {"type", "columns", "rows", "title"},
        "progress": {"type", "label", "value", "total"},
    }
    for item in ui:
        if (
            not isinstance(item, dict)
            or item.get("type") not in allowed
            or set(item) - allowed[item["type"]]
        ):
            raise ValueError("Unsupported extension UI declaration.")
        if item["type"] in {"text", "notice"} and not isinstance(item.get("text"), str):
            raise ValueError("Extension text UI requires a text string.")
        if item["type"] == "table":
            columns, rows = item.get("columns"), item.get("rows")
            if (
                not isinstance(columns, list)
                or len(columns) > 20
                or not all(isinstance(column, str) for column in columns)
                or not isinstance(rows, list)
                or len(rows) > 100
                or not all(
                    isinstance(row, list)
                    and len(row) == len(columns)
                    and all(
                        isinstance(cell, (str, int, float, bool, type(None)))
                        for cell in row
                    )
                    for row in rows
                )
            ):
                raise ValueError("Invalid extension table UI.")
        if item["type"] == "progress" and (
            type(item.get("value")) not in {int, float}
            or type(item.get("total")) not in {int, float}
            or item["total"] <= 0
            or not 0 <= item["value"] <= item["total"]
        ):
            raise ValueError("Invalid extension progress UI.")
    if len(json.dumps(ui)) > MAX_OUTPUT:
        raise ValueError("Extension UI output is too large.")
    error = value.get("error")
    return {
        "ok": bool(value.get("ok", error is None)),
        "output": output[:MAX_OUTPUT],
        "truncated": len(output) > MAX_OUTPUT,
        "error": str(error)[:1000] if error else None,
        "ui": ui,
    }


@dataclass
class Context:
    workspace: Path | None = None
    settings: dict[str, Any] = field(default_factory=dict)


class Extension:
    """Register functions with signature ``function(arguments, context)``."""

    def __init__(self):
        self.tools: dict[str, dict] = {}
        self.commands: dict[str, dict] = {}
        self.hooks: dict[str, list[Callable]] = {}
        self.settings: dict[str, dict] = {}

    def _register(self, target, name, function, description, schema, read_only=False):
        if (
            not NAME.fullmatch(name)
            or name in target
            or len(target) >= 64
            or not callable(function)
        ):
            raise ValueError("Invalid or duplicate extension registration.")
        schema = schema or {"type": "object", "additionalProperties": False}
        check_schema(schema)
        target[name] = {
            "function": function,
            "name": name,
            "description": str(description)[:2000],
            "input_schema": schema,
            "read_only": bool(read_only),
        }
        return function

    def register_tool(
        self, name, function, *, description, schema=None, read_only=False
    ):
        return self._register(
            self.tools, name, function, description, schema, read_only
        )

    def register_command(self, name, function, *, description="", schema=None):
        return self._register(self.commands, name, function, description, schema)

    def register_hook(self, event: str, function: Callable) -> None:
        if not NAME.fullmatch(event) or len(self.hooks) >= 32 or not callable(function):
            raise ValueError("Invalid extension hook.")
        callbacks = self.hooks.setdefault(event, [])
        if len(callbacks) >= 8:
            raise ValueError("Too many extension hook callbacks.")
        callbacks.append(function)

    def register_setting(
        self,
        name: str,
        default: Any,
        *,
        description: str = "",
        schema: dict | None = None,
    ) -> None:
        if (
            not NAME.fullmatch(name)
            or name in self.settings
            or len(self.settings) >= 64
            or any(
                word in name
                for word in ("password", "secret", "token", "credential", "api_key")
            )
        ):
            raise ValueError("Invalid setting; credentials are not extension settings.")
        schema = schema or {
            "type": {
                str: "string",
                bool: "boolean",
                int: "integer",
                float: "number",
                list: "array",
                dict: "object",
                type(None): "null",
            }.get(type(default), "null")
        }
        check_schema(schema)
        validate_value(default, schema)
        self.settings[name] = {
            "default": default,
            "description": description[:2000],
            "schema": schema,
        }

    def describe(self) -> dict:
        def public(records):
            return [
                {key: value for key, value in item.items() if key != "function"}
                for item in records.values()
            ]

        return {
            "api_version": API_VERSION,
            "tools": public(self.tools),
            "commands": public(self.commands),
            "hooks": list(self.hooks),
            "settings": self.settings,
        }

    async def invoke(
        self, kind: str, name: str, arguments: dict, context: Context
    ) -> dict:
        target = self.tools if kind == "tool" else self.commands
        if name not in target or not isinstance(arguments, dict):
            raise ValueError("Unknown extension operation.")
        registration = target[name]
        validate_value(arguments, registration["input_schema"])
        result = registration["function"](arguments, context)
        if inspect.isawaitable(result):
            result = await result
        return normalized_result(result)

    async def notify(self, event: str, payload: dict, context: Context) -> list[dict]:
        result = []
        for callback in self.hooks.get(event, []):
            value = callback(payload, context)
            if inspect.isawaitable(value):
                value = await value
            result.append(normalized_result(value))
        return result
