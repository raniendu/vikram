"""Private versioned JSONL extension worker. No terminal UI, logs or telemetry."""

import asyncio
import importlib
import importlib.metadata
import inspect
import json
import os
import re
import sys
from pathlib import Path

from vikram.extension_sdk import (
    API_VERSION,
    MAX_FRAME,
    Context,
    Extension,
    validate_value,
)


def load_extension(entry: str, package: str | None = None) -> Extension:
    if package:
        location = Path(package).resolve(strict=True)
        sys.path.insert(0, str(location))
    module, separator, attribute = entry.partition(":")
    if (
        not separator
        or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module)
        or not re.fullmatch(r"[A-Za-z_]\w*", attribute)
    ):
        raise ValueError("Invalid extension entry point.")
    register = getattr(importlib.import_module(module), attribute)
    if isinstance(register, Extension):
        return register
    extension = Extension()
    result = register(extension)
    if inspect.isawaitable(result):
        raise ValueError("Extension registration must be synchronous.")
    return extension


def discover() -> list[dict]:
    result = []
    for distribution in importlib.metadata.distributions():
        for entry in distribution.entry_points:
            if entry.group == "vikram.extensions":
                result.append(
                    {
                        "name": entry.name,
                        "entry": entry.value,
                        "distribution": distribution.metadata["Name"],
                        "version": distribution.version,
                    }
                )
    return result


async def serve(output) -> None:
    extension, context = None, Context()
    while True:
        raw = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_FRAME + 1)
        if not raw:
            break
        if len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
            break
        request_id = None
        try:
            request = json.loads(raw)
            if (
                not isinstance(request, dict)
                or request.get("v") != API_VERSION
                or not isinstance(request.get("id"), str)
            ):
                raise ValueError("Invalid extension request.")
            request_id = request["id"]
            method = request.get("method")
            arguments = request.get("params", {})
            if not isinstance(arguments, dict):
                raise ValueError("Invalid extension parameters.")
            if method == "discover":
                result = discover()
            elif method == "initialize":
                if extension is not None:
                    raise ValueError("Extension already initialized.")
                extension = load_extension(arguments["entry"], arguments.get("package"))
                supplied = arguments.get("settings", {})
                if not isinstance(supplied, dict):
                    raise ValueError("Invalid extension settings.")
                context.settings = {
                    name: supplied.get(name, spec["default"])
                    for name, spec in extension.settings.items()
                }
                for name, value in context.settings.items():
                    validate_value(value, extension.settings[name]["schema"])
                workspace = arguments.get("workspace")
                context.workspace = Path(workspace) if workspace else None
                result = extension.describe()
            elif method in {"tool", "command"} and extension is not None:
                result = await extension.invoke(
                    method, arguments["name"], arguments.get("arguments", {}), context
                )
            elif method == "hook" and extension is not None:
                result = await extension.notify(
                    arguments["event"], arguments.get("payload", {}), context
                )
            elif method == "close":
                result = None
            else:
                raise ValueError("Unsupported extension request.")
            response = {"v": API_VERSION, "id": request_id, "result": result}
            encoded = json.dumps(response, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode()) > MAX_FRAME:
                raise ValueError("Extension response exceeds its limit.")
        except Exception as exc:
            # Exception bodies can contain prompts, paths, or credentials.
            encoded = json.dumps(
                {
                    "v": API_VERSION,
                    "id": request_id,
                    "error": "Extension operation failed (" + type(exc).__name__ + ").",
                }
            )
        output.write(encoded + "\n")
        output.flush()
        if (
            isinstance(locals().get("request"), dict)
            and request.get("method") == "close"
        ):
            break


def main() -> None:
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
    # Even native writes and extension child processes cannot corrupt framing.
    with open(os.devnull, "w") as quiet:
        os.dup2(quiet.fileno(), 1)
        os.dup2(quiet.fileno(), 2)
        sys.stdout = quiet
        sys.stderr = quiet
        try:
            asyncio.run(serve(protocol))
        finally:
            protocol.close()


if __name__ == "__main__":
    main()
