from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections.abc import Sequence
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import structlog
from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from vikram import __version__
from vikram.agent import build_agent
from vikram.dbos_gateway import EventDispatcher, launch_dbos, shutdown_dbos
from vikram.gateway import InboundMessage, ThreadStore
from vikram.logging import configure_logging, get_logger, thread_hash
from vikram.observability import (
    get_tracer,
    init_observability,
    record_span_exception,
    set_span_attributes,
)
from vikram.settings import VikramSettings, resolve_model_selection
from vikram.spec import AgentSurfaceError, ensure_surface_allowed
from vikram.specstore import load_agent
from vikram.telegram import TelegramAdapter
from vikram.telegram_config import TelegramConfig, load_telegram_config

logger = get_logger(__name__)

REQUEST_ID_HEADER = "x-request-id"


class ChatRequest(BaseModel):
    prompt: str = Field(min_length=1)
    agent: str | None = None


class ChatResponse(BaseModel):
    agent: str
    output: str


class ThreadMessageRequest(BaseModel):
    prompt: str = Field(min_length=1)
    agent: str | None = None


class EnqueueResponse(BaseModel):
    workflow_id: str
    thread_id: str
    status: str


_settings: VikramSettings | None = None
_agents: dict[str, Any] = {}
_store: ThreadStore | None = None
_dispatcher: EventDispatcher | None = None
_telegram_config: TelegramConfig | None = None
_telegram_adapters: dict[str, TelegramAdapter] = {}


def _get_settings() -> VikramSettings:
    global _settings
    if _settings is None:
        _settings = VikramSettings()
    return _settings


def _load_http_spec(name: str):
    settings = _get_settings()
    spec = load_agent(name, settings)
    ensure_surface_allowed(spec, "http")
    return spec


def _get_agent(name: str) -> Any:
    if name not in _agents:
        settings = _get_settings()
        spec = _load_http_spec(name)
        _agents[name] = build_agent(spec=spec, settings=settings, surface="http")
    return _agents[name]


def _get_store() -> ThreadStore:
    global _store
    if _store is None:
        _store = ThreadStore(_get_settings().vikram_db_path)
    return _store


def _get_dispatcher() -> EventDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = EventDispatcher()
    return _dispatcher


def _get_telegram_config() -> TelegramConfig:
    global _telegram_config
    if _telegram_config is None:
        settings = _get_settings()
        _telegram_config = load_telegram_config(
            settings.spec_root,
            default_agent=settings.default_agent,
        )
    return _telegram_config


def _get_telegram_adapter(bot_name: str) -> TelegramAdapter:
    if bot_name not in _telegram_adapters:
        _telegram_adapters[bot_name] = TelegramAdapter(
            settings=_get_settings(),
            bot=_get_telegram_config().get_bot(bot_name),
            store=_get_store(),
            enqueue_message=_get_dispatcher().enqueue_message,
        )
    return _telegram_adapters[bot_name]


# Set by run() in GUI mode. stdout is the desktop shell's handshake channel,
# so every log line must go to stderr or it corrupts the port announcement.
_log_stream: Any | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = _get_settings()
    configure_logging(settings.log_level, stream=_log_stream)
    init_observability(settings)
    model_provider, model = resolve_model_selection(settings)
    logger.info(
        "api_starting",
        default_agent=settings.default_agent,
        model_provider=model_provider,
        model=model,
        db_path=str(settings.vikram_db_path),
    )
    launch_dbos(settings)
    _get_agent(settings.default_agent)
    try:
        yield
    finally:
        logger.info("api_stopping")
        _agents.clear()
        global _store, _dispatcher, _telegram_config
        _store = None
        _dispatcher = None
        _telegram_config = None
        _telegram_adapters.clear()
        shutdown_dbos()


app = FastAPI(title="vikram", lifespan=lifespan)


@app.middleware("http")
async def observability_middleware(request: Request, call_next: Any) -> Response:
    """Give every request an id, a span, and one completion log line.

    The request id is bound into structlog's context variables, so every log
    emitted while handling the request — including from the gateway, Telegram,
    and tool layers — carries it without being threaded through by hand. It is
    echoed back in the response header so a caller can quote it in a bug report.
    """
    request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
    structlog.contextvars.bind_contextvars(request_id=request_id)
    start = time.perf_counter()
    with get_tracer().start_as_current_span(
        f"{request.method} {request.url.path}"
    ) as span:
        set_span_attributes(
            span,
            {
                "http.request.method": request.method,
                "url.path": request.url.path,
                "vikram.request_id": request_id,
            },
        )
        try:
            response = await call_next(request)
        except Exception as exc:
            record_span_exception(span, exc)
            logger.exception(
                "http_request_failed",
                http_method=request.method,
                path=request.url.path,
                duration_ms=_elapsed_ms(start),
                error_type=type(exc).__name__,
            )
            structlog.contextvars.unbind_contextvars("request_id")
            raise
        span.set_attribute("http.response.status_code", response.status_code)
        log = logger.info if response.status_code < 500 else logger.error
        log(
            "http_request_finished",
            http_method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=_elapsed_ms(start),
        )
    response.headers[REQUEST_ID_HEADER] = request_id
    structlog.contextvars.unbind_contextvars("request_id")
    return response


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe: the process is up and serving.

    Deliberately does no dependency work — ``vikram.local_webhook.check_health``
    and container liveness checks depend on this exact payload.
    """
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(response: Response) -> dict[str, Any]:
    """Readiness probe: report whether Vikram can actually serve a request.

    Unlike ``/healthz`` this exercises the real dependencies — model config,
    thread store, and the default agent — and answers 503 when one is broken,
    so a deploy fails loudly instead of accepting traffic it cannot serve.
    """
    settings = _get_settings()
    provider, model = resolve_model_selection(settings)
    checks: dict[str, str] = {
        "model_config": "ok" if provider and model else "unconfigured"
    }
    for name, probe in (
        ("thread_store", _get_store),
        ("default_agent", _default_agent),
    ):
        try:
            probe()
        except Exception as exc:
            logger.exception("readiness_check_failed", check=name)
            checks[name] = f"error: {type(exc).__name__}"
        else:
            checks[name] = "ok"

    ready = all(status == "ok" for status in checks.values())
    if not ready:
        response.status_code = 503
        logger.warning("readiness_degraded", checks=checks)
    return {
        "status": "ready" if ready else "degraded",
        "version": __version__,
        "environment": settings.environment,
        "default_agent": settings.default_agent,
        "checks": checks,
    }


def _default_agent() -> Any:
    return _get_agent(_get_settings().default_agent)


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    name = req.agent or _get_settings().default_agent
    try:
        agent = _get_agent(name)
    except AgentSurfaceError as exc:
        logger.warning("chat_rejected", agent=name, reason="surface_not_allowed")
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        logger.warning("chat_rejected", agent=name, reason="unknown_agent")
        raise HTTPException(status_code=404, detail=f"Unknown agent: {name}") from exc

    log = logger.bind(agent=name, prompt_length=len(req.prompt))
    log.info("chat_started")
    start = time.perf_counter()
    try:
        result = await agent.run(req.prompt, conversation_id=f"chat:{name}")
    except Exception:
        log.exception("chat_failed", duration_ms=_elapsed_ms(start))
        raise
    output = str(result.output)
    log.info(
        "chat_succeeded", duration_ms=_elapsed_ms(start), output_length=len(output)
    )
    return ChatResponse(agent=name, output=output)


@app.post(
    "/threads/{interface}/{external_thread_id}/messages",
    response_model=EnqueueResponse,
)
async def thread_message(
    interface: str,
    external_thread_id: str,
    req: ThreadMessageRequest,
) -> EnqueueResponse:
    agent_name = req.agent or _get_settings().default_agent
    try:
        _load_http_spec(agent_name)
    except AgentSurfaceError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"Unknown agent: {agent_name}"
        ) from exc
    logger.info(
        "thread_message_received",
        interface=interface,
        thread_hash=thread_hash(interface, external_thread_id),
        agent=req.agent,
        prompt_length=len(req.prompt),
    )
    enqueued = await _get_dispatcher().enqueue_message(
        InboundMessage(
            interface=interface,
            external_thread_id=external_thread_id,
            prompt=req.prompt,
            agent_name=req.agent,
            default_agent=None,
            metadata={},
        )
    )
    logger.info(
        "thread_message_enqueued",
        interface=interface,
        thread_hash=thread_hash(interface, external_thread_id),
        workflow_id=enqueued.workflow_id,
        status=enqueued.status,
    )
    return EnqueueResponse(
        workflow_id=enqueued.workflow_id,
        thread_id=f"{interface}:{external_thread_id}",
        status=enqueued.status,
    )


@app.get("/events/{workflow_id}")
async def event_status(workflow_id: str) -> dict[str, Any]:
    return await _get_dispatcher().get_event_status(workflow_id)


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, Any]:
    return await _handle_telegram_webhook(
        _get_telegram_config().default_bot_name,
        request,
        x_telegram_bot_api_secret_token,
    )


@app.post("/telegram/{bot_name}/webhook")
async def named_telegram_webhook(
    bot_name: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, Any]:
    return await _handle_telegram_webhook(
        bot_name,
        request,
        x_telegram_bot_api_secret_token,
    )


async def _handle_telegram_webhook(
    bot_name: str,
    request: Request,
    secret_token: str | None,
) -> dict[str, Any]:
    try:
        bot = _get_telegram_config().get_bot(bot_name)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"Unknown Telegram bot: {bot_name}"
        ) from exc
    if not bot.webhook_secret:
        logger.warning("telegram_webhook_unconfigured")
        raise HTTPException(
            status_code=503,
            detail=f"Telegram webhook secret is not configured for {bot_name}",
        )
    if secret_token != bot.webhook_secret:
        logger.warning("telegram_webhook_secret_rejected", telegram_bot=bot_name)
        raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret")
    update = await request.json()
    result = await _get_telegram_adapter(bot_name).handle_update(update)
    logger.info(
        "telegram_webhook_processed",
        telegram_bot=bot_name,
        update_id=update.get("update_id"),
        status=result.status,
        workflow_id=result.workflow_id,
    )
    return {
        "status": result.status,
        "workflow_id": result.workflow_id,
    }


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def mount_gui(target: FastAPI, *, origins: list[str] | None = None) -> None:
    """Attach the desktop app's router.

    Never called on a default ``vikram-api``. These endpoints reach the agent
    store and shell-capable sessions, and ``Dockerfile`` serves this app on
    ``0.0.0.0``, so mounting is opt-in and every route requires a bearer token.
    """
    from fastapi.middleware.cors import CORSMiddleware

    from vikram.api_gui import allowed_origins, router

    # Idempotent: a second mount would fail once the app has started serving,
    # and would double-register every route.
    if any(str(route.path).startswith("/v1") for route in target.routes):
        logger.debug("gui_router_already_mounted")
        return

    target.add_middleware(
        CORSMiddleware,
        allow_origins=origins if origins is not None else allowed_origins(),
        allow_methods=["*"],
        allow_headers=["authorization", "content-type"],
    )
    target.include_router(router)
    logger.info("gui_router_mounted", routes=len(router.routes))


def _build_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vikram-api", description="Run the Vikram HTTP API."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="0 picks a free port, reported on stdout as JSON.",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Mount the desktop app's router. Requires VIKRAM_GUI_TOKEN and a "
        "loopback host.",
    )
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=None,
        help="Exit when this process is gone. Used by the desktop shell so a "
        "crashed window cannot leave the API running unattended.",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> None:
    import socket

    import uvicorn

    from vikram.api_gui import GUI_TOKEN_ENV, gui_enabled

    args = _build_run_parser().parse_args(list(argv) if argv is not None else None)
    enable_gui = args.gui or gui_enabled()

    if enable_gui:
        # Before anything can emit a log line: structlog's default writes to
        # stdout, which is where the desktop shell reads the port from.
        global _log_stream
        _log_stream = sys.stderr
        configure_logging(args.log_level.upper(), stream=sys.stderr)

        if args.host not in LOOPBACK_HOSTS:
            raise SystemExit(
                f"Refusing to serve the GUI router on {args.host}. "
                "It reaches the agent store and shell tools; bind 127.0.0.1."
            )
        if not os.environ.get(GUI_TOKEN_ENV):
            raise SystemExit(
                f"Refusing to start: --gui requires {GUI_TOKEN_ENV} to be set."
            )
        mount_gui(app)

    # Bind before serving so the chosen port is known with no race: the desktop
    # shell reads it from this line rather than polling for the server.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    bound_port = sock.getsockname()[1]
    print(
        json.dumps(
            {
                "vikram_api_ready": True,
                "host": args.host,
                "port": bound_port,
                "pid": os.getpid(),
                "gui": enable_gui,
            }
        ),
        flush=True,
    )

    if args.parent_pid:
        _watch_parent(args.parent_pid)

    server = uvicorn.Server(
        uvicorn.Config(app, log_level=args.log_level, access_log=False)
    )
    server.run(sockets=[sock])


def _watch_parent(parent_pid: int, *, interval: float = 2.0) -> None:
    """Terminate when ``parent_pid`` disappears.

    Without this, a crashed desktop shell would leave a shell-capable HTTP
    server listening with nobody watching it.
    """
    import signal
    import threading

    def watch() -> None:
        while True:
            time.sleep(interval)
            try:
                os.kill(parent_pid, 0)
            except OSError:
                logger.warning("gui_parent_gone", parent_pid=parent_pid)
                os.kill(os.getpid(), signal.SIGTERM)
                return

    threading.Thread(target=watch, daemon=True, name="parent-watchdog").start()


if __name__ == "__main__":
    run()
