"""Explicit local sandbox status/reset operations; never model-facing tools."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from vikram.desktop.store import Store
from vikram.sandbox_session import load_state, reset_sandbox, state_path
from vikram.settings import application_root


def sandbox_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="vikram sandbox")
    parser.add_argument("action", choices=("status", "reset"))
    parser.add_argument("--session", required=True, help="conversation ID")
    parser.add_argument("--session-dir", help="chat library, as for vikram")
    parser.add_argument(
        "--yes", action="store_true", help="confirm deletion of sandbox files"
    )
    args = parser.parse_args(argv)
    if args.action == "reset" and not args.yes:
        parser.error("reset deletes this sandbox's files; pass --yes to confirm")
    root = Path(
        args.session_dir or os.environ.get("VIKRAM_CHAT_DATA_DIR") or application_root()
    )
    try:
        store = Store(root)
        try:
            with store.mutation(args.session):
                store._require_idle(args.session)
                path = state_path(store.root, args.session)
                if args.action == "reset":
                    asyncio.run(reset_sandbox(path))
                    print(
                        "Sandbox reset. Its files were deleted; the conversation was kept."
                    )
                else:
                    saved = load_state(path)
                    if saved is None:
                        print(
                            "No persistent sandbox has been created for this conversation."
                        )
                    else:
                        print(
                            "Sandbox saved and stopped at its last checkpoint."
                            if saved.saved
                            else "Sandbox state is uncertain; inspect its files before continuing."
                        )
        finally:
            store.close()
    except Exception:
        sys.stderr.write(
            "The sandbox operation could not finish. Check its conversation ID, active controller, VM availability, or private metadata. Any saved reference was retained.\n"
        )
        return 1
    return 0
