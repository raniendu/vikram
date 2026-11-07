"""Private stdio entry point. Never writes conversation content to diagnostics."""

import asyncio
import fcntl
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

from pydantic import ValidationError

from .protocol import Command, validate_parameters
from .runtime import Runtime
from .store import Store
from .updates import Updater

MAX_LINE = 15 * 1024 * 1024
MAX_PENDING = 8
MAX_IMPORTS = 4
BACKGROUND_METHODS = {"send", "refresh", "import_attachment", "import_image"}
QUICK_WORKBENCH = {"queue", "queue_edit", "queue_remove", "tree"}


def emit(payload: dict) -> None:
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
    )
    sys.stdout.flush()


async def serve(root: Path, *, browser: bool = True) -> None:
    from vikram.schedules import installed_agent

    store = Store(root)
    # The app hosts the browser panel that this runtime drives, and its replies
    # may schedule tasks, which the installed app runs in the background.
    runtime = Runtime(
        store,
        emit,
        browser=browser,
        tasks=True,
        agent=installed_agent(root),
        updater=Updater(emit),
    )
    pending: set[asyncio.Task] = set()
    imports: set[asyncio.Task] = set()

    def decode(raw: bytes) -> Command | None:
        identifier = None
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                candidate = payload.get("id")
                if (
                    isinstance(candidate, str)
                    and 0 < len(candidate) <= 80
                    and candidate.isprintable()
                ):
                    identifier = candidate
            return validate_parameters(Command.model_validate(payload))
        except (
            ValidationError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
        ):
            error = "Invalid desktop command. Restart or update Vikram."
        except ValueError:
            error = "Unsupported protocol or command."
        emit({"v": 1, "id": identifier, "error": error})
        return None

    async def respond(cmd: Command):
        try:
            result = await runtime.command(cmd.method, cmd.params, cmd.id)
            emit({"v": 1, "id": cmd.id, "result": result})
        except asyncio.CancelledError:
            emit({"v": 1, "id": cmd.id, "error": "The desktop action was cancelled."})
            raise
        except (ValidationError, UnicodeError, KeyError, TypeError):
            emit(
                {
                    "v": 1,
                    "id": cmd.id,
                    "error": "Invalid desktop command. Restart or update Vikram.",
                }
            )
        except ValueError as e:
            emit(
                {
                    "v": 1,
                    "id": cmd.id,
                    "error": str(e),
                }
            )
        except (OSError, sqlite3.Error):
            emit(
                {
                    "v": 1,
                    "id": cmd.id,
                    "error": "Could not access local chat storage. Keep a copy of unsaved text and check disk space.",
                }
            )
        except Exception:
            emit(
                {
                    "v": 1,
                    "id": cmd.id,
                    "error": "The desktop service could not complete this action. Reconnect and try again.",
                }
            )

    try:
        while raw := await asyncio.to_thread(sys.stdin.buffer.readline, MAX_LINE + 1):
            if len(raw) > MAX_LINE or not raw.endswith(b"\n"):
                break
            cmd = decode(raw)
            if cmd is None:
                continue
            importing = cmd.method in {"import_attachment", "import_image"}
            background = cmd.method in BACKGROUND_METHODS or (
                cmd.method == "workbench"
                and cmd.params["action"] not in QUICK_WORKBENCH
            )
            if background:
                if len(pending) >= MAX_PENDING or (
                    importing and len(imports) >= MAX_IMPORTS
                ):
                    emit(
                        {
                            "v": 1,
                            "id": cmd.id,
                            "error": "Wait for the current desktop actions to finish.",
                        }
                    )
                    continue
                task = asyncio.create_task(respond(cmd))
                pending.add(task)
                if importing:
                    imports.add(task)
                task.add_done_callback(pending.discard)
                task.add_done_callback(imports.discard)
                # Establish Runtime/Workbench mutation guards before reading the
                # next command; quick draft/control commands retain wire order.
                await asyncio.sleep(0)
            else:
                await respond(cmd)
    finally:
        tasks = tuple(pending)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.stop()
        await runtime.close()
        store.close()


def main() -> None:
    if sys.argv[1:] == ["--extract-document"]:
        from vikram.document_worker import main as extract_main

        extract_main()
        return
    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    # This override is for isolated developer tests; the renderer cannot set it.
    root = Path(
        os.environ.get(
            "VIKRAM_CHAT_DATA_DIR",
            Path.home() / "Library/Application Support/dev.raniendu.vikram",
        )
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "runtime.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            emit(
                {
                    "v": 1,
                    "event": "fatal",
                    "message": "This chat library is already open in another Vikram process.",
                }
            )
            return
        try:
            asyncio.run(serve(root))
        except (ValueError, OSError, sqlite3.Error):
            emit(
                {
                    "v": 1,
                    "event": "fatal",
                    "message": "The chat library could not be opened. Check disk space and app version.",
                }
            )
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
