"""Disposable parser entry point. Never starts Ollama, MCP or a chat library."""

import base64
import json
import logging
import resource
import sys

from vikram.documents import extract_document


def main() -> None:
    logging.disable(logging.CRITICAL)
    resource.setrlimit(resource.RLIMIT_CPU, (12, 12))
    try:
        resource.setrlimit(resource.RLIMIT_DATA, (512 * 1024**2, 512 * 1024**2))
    except (ValueError, OSError):
        pass
    try:
        raw = sys.stdin.buffer.read(15 * 1024**2)
        args = json.loads(raw)
        data = base64.b64decode(args["data"], validate=True)
        result = extract_document(args["name"], data)
    except ValueError as exc:
        result = {"error": str(exc)[:400]}
    except Exception:
        result = {"error": "This document could not be read."}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
