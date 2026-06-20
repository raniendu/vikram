from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from pydantic_ai import Agent

from vikram.agent import build_agent
from vikram.dbos_gateway import EventDispatcher, launch_dbos, shutdown_dbos
from vikram.gateway import InboundMessage, ThreadStore
from vikram.logging import configure_logging, get_logger, thread_hash
from vikram.observability import init_observability
from vikram.settings import VikramSettings
from vikram.spec import AgentSurfaceError, ensure_surface_allowed, load_spec
from vikram.telegram import TelegramAdapter
from vikram.telegram_config import TelegramConfig, load_telegram_config

logger = get_logger(__name__)


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
_agents: dict[str, Agent[None, str]] = {}
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
    spec = load_spec(name, settings.spec_root)
    ensure_surface_allowed(spec, "http")
    return spec


def _get_agent(name: str) -> Agent[None, str]:
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = _get_settings()
    configure_logging(settings.log_level)
    init_observability(settings)
    logger.info(
        "api_starting",
        default_agent=settings.default_agent,
        model_provider=settings.model_provider,
        model=settings.model,
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


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    name = req.agent or _get_settings().default_agent
    try:
        agent = _get_agent(name)
    except AgentSurfaceError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {name}") from exc

    result = await agent.run(req.prompt, conversation_id=f"chat:{name}")
    return ChatResponse(agent=name, output=str(result.output))


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


def run() -> None:
    import uvicorn

    uvicorn.run("vikram.api:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    run()
