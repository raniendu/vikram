"""Minimal one-shot and interactive conversations with Vikram."""

import argparse
import asyncio
import os
import shlex
import sys
from collections.abc import Sequence

from pydantic_ai import Agent, AgentRunResult
from pydantic_ai.exceptions import AgentRunError, ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelMessage

from vikram.agent import DEFAULT_MODEL, build_agent


async def _reply(
    agent: Agent[None, str], prompt: str, history: list[ModelMessage]
) -> AgentRunResult[str]:
    async with agent:
        return await agent.run(prompt, message_history=history)


def _chat(model: str, prompt: str | None) -> None:
    agent = build_agent(model=model)
    history: list[ModelMessage] = []
    # Read terminal input outside the event loop so one Ctrl-C interrupts it.
    with asyncio.Runner() as runner:
        while True:
            message = prompt
            if message is None:
                try:
                    message = input("You: ").strip()
                except EOFError:
                    return
                if message == "/exit":
                    return
                if not message:
                    continue
            result = runner.run(_reply(agent, message, history))
            history = result.all_messages()
            sys.stdout.write(f"{result.output}\n")
            if prompt is not None:
                return


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local agent; return a shell exit status."""
    parser = argparse.ArgumentParser(
        description="Chat with Vikram using local Ollama. Type /exit to end a chat."
    )
    parser.add_argument(
        "prompt", nargs="?", help="one prompt; omit for interactive chat"
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("OLLAMA_MODEL", "").strip() or DEFAULT_MODEL,
        help="local Ollama model (default: OLLAMA_MODEL or %(default)s)",
    )
    args = parser.parse_args(argv)
    if args.prompt is not None and not args.prompt.strip():
        parser.error("prompt must not be empty")
    args.model = args.model.strip()
    if not args.model:
        parser.error("model must not be empty")

    try:
        _chat(args.model, args.prompt)
    except KeyboardInterrupt:
        sys.stderr.write("\n")
        return 130
    except ModelHTTPError as exc:
        if exc.status_code == 404:
            sys.stderr.write(
                f"Model {args.model!r} is unavailable. "
                f"Run `ollama pull {shlex.quote(args.model)}`.\n"
            )
        else:
            sys.stderr.write(f"Ollama returned HTTP {exc.status_code}.\n")
        return 1
    except ModelAPIError:
        sys.stderr.write(
            "Could not reach local Ollama. Start it with `ollama serve`.\n"
        )
        return 1
    except AgentRunError:
        sys.stderr.write("Ollama could not complete the response. Please try again.\n")
        return 1
    return 0
