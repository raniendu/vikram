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


def emit(payload: dict) -> None:
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
    )
    sys.stdout.flush()


async def serve(root: Path) -> None:
    from vikram.schedules import installed_agent

    store = Store(root)
    # The app hosts the browser panel that this runtime drives, and its replies
    # may schedule tasks, which the installed app runs in the background.
    runtime = Runtime(
        store,
        emit,
        browser=True,
        tasks=True,
        agent=installed_agent(root),
        updater=Updater(emit),
    )
    imports = set()

    async def respond(raw):
        cmd = None
        try:
            cmd = Command.model_validate_json(raw.decode("utf-8"))
            cmd = validate_parameters(cmd)
            result = await runtime.command(cmd.method, cmd.params, cmd.id)
            emit({"v": 1, "id": cmd.id, "result": result})
        except (ValidationError, UnicodeError, KeyError, TypeError):
            emit(
                {
                    "v": 1,
                    "id": cmd.id if cmd else None,
                    "error": "Invalid desktop command. Restart or update Vikram.",
                }
            )
        except ValueError as e:
            emit(
                {
                    "v": 1,
                    "id": cmd.id if cmd else None,
                    "error": str(e) if cmd else "Unsupported protocol or command.",
                }
            )
        except (OSError, sqlite3.Error):
            emit(
                {
                    "v": 1,
                    "id": cmd.id if cmd else None,
                    "error": "Could not access local chat storage. Keep a copy of unsaved text and check disk space.",
                }
            )
        except Exception:
            emit(
                {
                    "v": 1,
                    "id": cmd.id if cmd else None,
                    "error": "The desktop service could not complete this action. Reconnect and try again.",
                }
            )

    try:
        while raw := await asyncio.to_thread(sys.stdin.buffer.readline, MAX_LINE + 1):
            if len(raw) > MAX_LINE or not raw.endswith(b"\n"):
                break
            try:
                method = json.loads(raw).get("method")
            except (ValueError, AttributeError):
                method = None
            if method in {"import_attachment", "import_image"}:
                if len(imports) >= 4:
                    emit(
                        {
                            "v": 1,
                            "id": json.loads(raw).get("id"),
                            "error": "Wait for the current file imports to finish.",
                        }
                    )
                    continue
                task = asyncio.create_task(respond(raw))
                imports.add(task)
                task.add_done_callback(imports.discard)
            else:
                await respond(raw)
    finally:
        for task in imports:
            task.cancel()
        await asyncio.gather(*imports, return_exceptions=True)
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
