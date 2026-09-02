"""HTTP surface for the desktop app.

**This router is never mounted by default.** ``vikram-api`` is a deployment
target that ``Dockerfile`` serves on ``0.0.0.0``, and these endpoints reach the
agent store and (in a later phase) shell-capable sessions. Mounting is opt-in
via ``--gui`` / ``VIKRAM_GUI_ENABLED``, and every route additionally requires a
bearer token that the desktop shell generates per launch.

Nothing here returns credential material. Provider rows carry
``has_credential``, never the key; MCP responses redact the fields that carry
expanded ``${VAR}`` secrets, mirroring what ``mcp.py`` already keeps out of
logs.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from vikram import introspect
from vikram.config import (
    config_path,
    delete_agent_model,
    load_config_raw,
    merge_write_config,
    migrate_v1,
    write_agent_model,
)
from vikram.hooks import HookSpec
from vikram.logging import get_logger
from vikram.mcp import MCPConfigError, MCPServerSpec, build_mcp_server
from vikram.providers import PROVIDER_IDS, PROVIDERS
from vikram.session import SessionError, SessionRegistry, sse_stream
from vikram.settings import VikramSettings
from vikram.spec import AgentSpecDraft
from vikram.specstore import (
    AgentNotFoundError,
    AgentReadOnlyError,
    AgentStoreError,
    create_agent,
    delete_agent,
    duplicate_agent,
    get_agent,
    list_agents,
    shared_root,
    update_agent,
    user_agents_root,
)

logger = get_logger(__name__)

GUI_ENABLED_ENV = "VIKRAM_GUI_ENABLED"
GUI_TOKEN_ENV = "VIKRAM_GUI_TOKEN"
GUI_ORIGINS_ENV = "VIKRAM_GUI_ALLOWED_ORIGINS"

# The Tauri v2 macOS webview serves from tauri://localhost, so every call it
# makes is cross-origin; 1420 is the Tauri dev-server default.
DEFAULT_ALLOWED_ORIGINS = ("tauri://localhost", "http://localhost:1420")

_settings: VikramSettings | None = None


def gui_enabled() -> bool:
    return os.environ.get(GUI_ENABLED_ENV, "").strip().lower() in {"1", "true", "yes"}


def allowed_origins() -> list[str]:
    raw = os.environ.get(GUI_ORIGINS_ENV, "").strip()
    if not raw:
        return list(DEFAULT_ALLOWED_ORIGINS)
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def generate_token() -> str:
    return secrets.token_hex(32)


def _settings_for_request() -> VikramSettings:
    global _settings
    if _settings is None:
        _settings = VikramSettings()
    return _settings


def require_token(authorization: str = Header(default="")) -> None:
    """Reject any request without the launch token.

    Compared with ``secrets.compare_digest`` so a wrong token cannot be
    recovered by timing the response.
    """
    expected = os.environ.get(GUI_TOKEN_ENV, "")
    if not expected:
        raise HTTPException(status_code=503, detail="GUI token is not configured.")
    scheme, _, presented = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token.")


router = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])


# --- request/response models ------------------------------------------


class AgentCreateRequest(BaseModel):
    id: str
    draft: AgentSpecDraft
    system_prompt: str = ""


class AgentUpdateRequest(BaseModel):
    draft: AgentSpecDraft
    system_prompt: str | None = None


class AgentDuplicateRequest(BaseModel):
    new_id: str
    name: str | None = None


class ValidateRequest(BaseModel):
    draft: AgentSpecDraft
    agent_id: str | None = None


class ProviderUpdateRequest(BaseModel):
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = Field(
        default=None,
        description="Omit to keep the stored key; never echoed back.",
    )


class AgentModelRequest(BaseModel):
    provider: str
    model: str


class DefaultProviderRequest(BaseModel):
    provider: str


class EnvCheckRequest(BaseModel):
    refs: list[str]


def _detail(detail: Any) -> dict[str, Any]:
    payload = asdict(detail)
    payload["path"] = str(detail.path)
    payload["draft"] = detail.draft.model_dump(mode="json") if detail.draft else None
    return payload


def _handled(exc: Exception) -> HTTPException:
    if isinstance(exc, AgentNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, AgentReadOnlyError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, AgentStoreError):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc


# --- agents ------------------------------------------------------------


@router.get("/agents")
def read_agents() -> dict[str, Any]:
    settings = _settings_for_request()
    return {"agents": [asdict(summary) for summary in list_agents(settings)]}


@router.get("/agents/{agent_id}")
def read_agent(agent_id: str) -> dict[str, Any]:
    try:
        return _detail(get_agent(agent_id, _settings_for_request()))
    except AgentStoreError as exc:
        raise _handled(exc) from exc


@router.post("/agents", status_code=201)
def post_agent(request: AgentCreateRequest) -> dict[str, Any]:
    settings = _settings_for_request()
    try:
        detail = create_agent(
            request.id,
            request.draft,
            settings=settings,
            system_prompt=request.system_prompt,
        )
    except AgentStoreError as exc:
        raise _handled(exc) from exc
    return _detail(detail)


@router.put("/agents/{agent_id}")
def put_agent(agent_id: str, request: AgentUpdateRequest) -> dict[str, Any]:
    try:
        detail = update_agent(
            agent_id,
            request.draft,
            settings=_settings_for_request(),
            system_prompt=request.system_prompt,
        )
    except AgentStoreError as exc:
        raise _handled(exc) from exc
    return _detail(detail)


@router.delete("/agents/{agent_id}", status_code=204)
def remove_agent(agent_id: str) -> None:
    try:
        delete_agent(agent_id, _settings_for_request())
    except AgentStoreError as exc:
        raise _handled(exc) from exc


@router.post("/agents/{agent_id}/duplicate", status_code=201)
def post_duplicate(agent_id: str, request: AgentDuplicateRequest) -> dict[str, Any]:
    try:
        detail = duplicate_agent(
            agent_id,
            request.new_id,
            settings=_settings_for_request(),
            name=request.name,
        )
    except AgentStoreError as exc:
        raise _handled(exc) from exc
    return _detail(detail)


@router.post("/agents/validate")
def post_validate(request: ValidateRequest) -> dict[str, Any]:
    settings = _settings_for_request()
    agent_id = request.agent_id or "draft"
    agent_dir = user_agents_root() / agent_id
    report = introspect.validate_draft(
        request.draft,
        settings=settings,
        agent_dir=agent_dir,
        shared_dir=shared_root(settings),
    )
    return asdict(report)


@router.get("/agents/{agent_id}/prompt-preview")
def read_prompt_preview(agent_id: str) -> dict[str, Any]:
    """The fully assembled system prompt -- what the model actually sees.

    Includes the generated skills and subagent blocks, which no other surface
    exposes.
    """
    settings = _settings_for_request()
    try:
        detail = get_agent(agent_id, settings)
    except AgentStoreError as exc:
        raise _handled(exc) from exc
    if detail.draft is None:
        raise HTTPException(status_code=422, detail=detail.summary.error)

    report = introspect.validate_draft(
        detail.draft,
        settings=settings,
        agent_dir=detail.path.parent,
        shared_dir=shared_root(settings),
    )
    if report.system_prompt is None:
        raise HTTPException(status_code=422, detail=[asdict(i) for i in report.issues])
    return {
        "system_prompt": report.system_prompt,
        "length": len(report.system_prompt),
        "tool_names": report.tool_names,
        "approval_tool_names": report.approval_tool_names,
        "model_config": report.model_config,
    }


@router.get("/agents/{agent_id}/raw")
def read_raw(agent_id: str) -> dict[str, str]:
    try:
        detail = get_agent(agent_id, _settings_for_request())
    except AgentStoreError as exc:
        raise _handled(exc) from exc
    return {"toml": detail.source_toml}


# --- registries and schemas -------------------------------------------


@router.get("/schema/agent-draft")
def read_agent_schema() -> dict[str, Any]:
    return AgentSpecDraft.model_json_schema()


@router.get("/schema/mcp-server")
def read_mcp_schema() -> dict[str, Any]:
    return MCPServerSpec.model_json_schema()


@router.get("/schema/hook")
def read_hook_schema() -> dict[str, Any]:
    return HookSpec.model_json_schema()


@router.get("/tools")
def read_tools() -> dict[str, Any]:
    return {"tools": [asdict(info) for info in introspect.tool_catalog()]}


@router.get("/providers")
def read_providers() -> dict[str, Any]:
    settings = _settings_for_request()
    return {
        "default_provider": getattr(settings, "config_default_provider", None),
        "providers": [asdict(info) for info in introspect.provider_catalog(settings)],
    }


@router.get("/skills")
def read_skills(agent_id: str | None = Query(default=None)) -> dict[str, Any]:
    catalog = introspect.skill_catalog(_settings_for_request(), agent_id=agent_id)
    return {origin: [asdict(s) for s in skills] for origin, skills in catalog.items()}


@router.post("/env/check")
def post_env_check(request: EnvCheckRequest) -> dict[str, list[str]]:
    """Report which environment variables are unset. Never returns values."""
    return {"missing": [ref for ref in request.refs if not os.environ.get(ref)]}


# --- MCP ---------------------------------------------------------------

_REDACTED = "<redacted>"


def _redact_mcp(spec: MCPServerSpec) -> dict[str, Any]:
    """Describe a server without echoing expanded ``${VAR}`` secrets.

    ``mcp.py`` deliberately keeps url/command/env out of its logs for this
    reason; an HTTP response is the same exposure.
    """
    return {
        "name": spec.name,
        "transport": spec.transport,
        "tool_prefix": spec.tool_prefix,
        "timeout": spec.timeout,
        "read_timeout": spec.read_timeout,
        "command": _REDACTED if spec.command else None,
        "args": _REDACTED if spec.args else None,
        "url": _REDACTED if spec.url else None,
        "env_keys": sorted(spec.env),
        "header_keys": sorted(spec.headers),
    }


@router.post("/mcp/validate")
def post_mcp_validate(spec: MCPServerSpec) -> dict[str, Any]:
    """Construct the toolset without starting it, surfacing config errors."""
    try:
        build_mcp_server(spec)
    except MCPConfigError as exc:
        return {"ok": False, "error": str(exc), "server": _redact_mcp(spec)}
    return {"ok": True, "error": None, "server": _redact_mcp(spec)}


# --- config ------------------------------------------------------------


@router.get("/config")
def read_config() -> dict[str, Any]:
    """Config as stored, with API keys reduced to a boolean.

    ``top_level_model`` is surfaced explicitly because it lands on
    ``settings.model`` and beats every spec pin and per-agent override -- the
    editor warns about it rather than writing one.
    """
    data = migrate_v1(load_config_raw(config_path()))
    providers = {}
    for provider_id, values in (data.get("providers") or {}).items():
        values = dict(values)
        providers[provider_id] = {
            "model": values.get("model"),
            "base_url": values.get("base_url"),
            "has_api_key": bool(values.get("api_key")),
        }
    return {
        "path": str(config_path()),
        "config_version": data.get("config_version"),
        "default_provider": data.get("default_provider"),
        "top_level_model": data.get("model"),
        "providers": providers,
        "agents": data.get("agents") or {},
    }


@router.put("/config/providers/{provider_id}")
def put_provider(provider_id: str, request: ProviderUpdateRequest) -> dict[str, Any]:
    if provider_id not in PROVIDERS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown provider '{provider_id}'. Known: {', '.join(PROVIDER_IDS)}.",
        )
    values = {
        key: value
        for key, value in request.model_dump(exclude_none=True).items()
        if value != ""
    }
    if values:
        merge_write_config({"providers": {provider_id: values}})
    return read_config()


@router.put("/config/default-provider")
def put_default_provider(request: DefaultProviderRequest) -> dict[str, Any]:
    if request.provider not in PROVIDERS:
        raise HTTPException(
            status_code=404, detail=f"Unknown provider '{request.provider}'."
        )
    merge_write_config({}, default_provider=request.provider)
    return read_config()


@router.put("/config/agents/{agent_id}")
def put_agent_model(agent_id: str, request: AgentModelRequest) -> dict[str, Any]:
    if request.provider not in PROVIDERS:
        raise HTTPException(
            status_code=404, detail=f"Unknown provider '{request.provider}'."
        )
    write_agent_model(agent_id, provider=request.provider, model=request.model)
    return read_config()


@router.delete("/config/agents/{agent_id}")
def remove_agent_model(agent_id: str) -> dict[str, Any]:
    """Clear a per-agent override so the agent follows its spec pin again."""
    delete_agent_model(agent_id)
    return read_config()


# --- doctor ------------------------------------------------------------


@router.get("/doctor")
def read_doctor(
    agent_id: str | None = Query(default=None),
    cwd: str | None = Query(default=None),
) -> dict[str, Any]:
    from vikram.doctor import collect_diagnostics

    diagnostics = collect_diagnostics(
        agent_name=agent_id, cwd=Path(cwd) if cwd else None
    )
    return {"diagnostics": [asdict(item) for item in diagnostics]}


# --- sessions ----------------------------------------------------------


_registry = SessionRegistry()


class SessionCreateRequest(BaseModel):
    agent_id: str
    workspace: str
    approve_all: bool = False


class PromptRequest(BaseModel):
    prompt: str = Field(min_length=1)


class ApprovalDecisionRequest(BaseModel):
    decision: Literal["allow", "deny", "allow_always"]
    tool_name: str | None = None


class SessionFlagsRequest(BaseModel):
    approve_all: bool


def _session_info(session: Any) -> dict[str, Any]:
    return {
        "session_id": session.id,
        "agent_id": session.agent_id,
        "workspace": str(session.workspace),
        "closed": session.closed,
        **(session.ready.get("payload") or {}),
    }


def _session_or_404(session_id: str) -> Any:
    try:
        return _registry.get(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No session {session_id}.")


@router.post("/sessions", status_code=201)
async def post_session(request: SessionCreateRequest) -> dict[str, Any]:
    """Open a session: spawns a worker, chdirs it, and warms its MCP servers."""
    try:
        session = await _registry.create(
            agent_id=request.agent_id, workspace=Path(request.workspace)
        )
    except SessionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if request.approve_all:
        await session.set_flags(approve_all=True)
    return _session_info(session)


@router.get("/sessions")
async def read_sessions() -> dict[str, Any]:
    return {"sessions": [_session_info(s) for s in _registry.list()]}


@router.get("/sessions/{session_id}")
async def read_session(session_id: str) -> dict[str, Any]:
    return _session_info(_session_or_404(session_id))


@router.patch("/sessions/{session_id}")
async def patch_session(
    session_id: str, request: SessionFlagsRequest
) -> dict[str, Any]:
    """Toggle approve-all live, without rebuilding the agent."""
    session = _session_or_404(session_id)
    await session.set_flags(approve_all=request.approve_all)
    return _session_info(session)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str) -> None:
    _session_or_404(session_id)
    await _registry.close(session_id)


@router.post("/sessions/{session_id}/messages", status_code=202)
async def post_message(session_id: str, request: PromptRequest) -> dict[str, str]:
    """Start a turn. Returns immediately; output arrives on the event stream."""
    session = _session_or_404(session_id)
    try:
        turn_id = await session.prompt(request.prompt)
    except SessionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"turn_id": turn_id}


@router.get("/sessions/{session_id}/events")
async def read_session_events(session_id: str) -> StreamingResponse:
    """One long-lived SSE stream per session.

    Session-scoped rather than turn-scoped so an approval request can outlive
    the HTTP call that triggered the turn.
    """
    session = _session_or_404(session_id)
    return StreamingResponse(
        sse_stream(session),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{session_id}/approvals/{approval_id}", status_code=204)
async def post_approval(
    session_id: str, approval_id: str, request: ApprovalDecisionRequest
) -> None:
    session = _session_or_404(session_id)
    try:
        await session.approve(
            approval_id, request.decision, tool_name=request.tool_name
        )
    except SessionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/cancel", status_code=204)
async def post_cancel(session_id: str) -> None:
    session = _session_or_404(session_id)
    try:
        await session.cancel()
    except SessionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def shutdown_sessions() -> None:
    """Reap every worker. Wired to the app's lifespan."""
    await _registry.close_all()


__all__ = [
    "DEFAULT_ALLOWED_ORIGINS",
    "GUI_ENABLED_ENV",
    "GUI_ORIGINS_ENV",
    "GUI_TOKEN_ENV",
    "allowed_origins",
    "shutdown_sessions",
    "generate_token",
    "gui_enabled",
    "require_token",
    "router",
]
