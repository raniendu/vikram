"""Model Context Protocol (MCP) server support for Vikram agents.

Agents declare MCP servers in ``agent.toml`` under ``[[mcp_servers]]``. Each
entry is parsed into an :class:`MCPServerSpec` (carried on the agent spec) and
realized into a Pydantic AI MCP server toolset by :func:`build_mcp_server`.

Pydantic AI treats MCP servers as toolsets and manages their lifecycle
automatically for each ``agent.run``/``agent.iter`` call, so attaching servers
is just a matter of passing them to ``Agent(toolsets=...)`` in
``vikram.agent.build_agent``.

Secrets must never be written inline. String fields (``command``, ``args``,
``url``, ``cwd``, and the values of ``env``/``headers``) may reference
environment variables with ``${VAR}`` syntax; references are expanded against
the process environment when the agent is built, and a missing variable is a
hard error.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai.mcp import (
    MCPServer,
    MCPServerSSE,
    MCPServerStdio,
    MCPServerStreamableHTTP,
)

MCPTransport = Literal["stdio", "http", "sse"]

# Matches ${NAME} references for environment-variable expansion.
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MCPConfigError(RuntimeError):
    """Raised when an MCP server spec is invalid or references a missing env var."""


class MCPServerSpec(BaseModel):
    """Declarative configuration for one Model Context Protocol server.

    Lives in ``agent.toml`` under ``[[mcp_servers]]``. Transport-specific
    fields are validated at build time by :func:`build_mcp_server` rather than
    at parse time so that loading a spec never requires the referenced servers'
    secrets to be present.
    """

    name: str
    transport: MCPTransport = "stdio"

    # stdio transport: a local subprocess speaking MCP over stdio.
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None

    # http / sse transport: a remote MCP server reached over HTTP.
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    # Shared knobs.
    tool_prefix: str | None = None
    timeout: float = 5.0
    read_timeout: float | None = None


def _expand(value: str, environ: Mapping[str, str], *, where: str) -> str:
    """Expand ``${VAR}`` references in ``value`` from ``environ``.

    Raises :class:`MCPConfigError` naming ``where`` if a referenced variable is
    not defined, so misconfiguration fails loudly at agent-build time.
    """

    def replace(match: re.Match[str]) -> str:
        var = match.group(1)
        if var not in environ:
            raise MCPConfigError(
                f"{where} references undefined environment variable "
                f"${{{var}}}. Set it in the environment or .env."
            )
        return environ[var]

    return _ENV_REF.sub(replace, value)


def _expand_mapping(
    mapping: Mapping[str, str], environ: Mapping[str, str], *, where: str
) -> dict[str, str]:
    return {
        key: _expand(value, environ, where=f"{where}[{key!r}]")
        for key, value in mapping.items()
    }


def build_mcp_server(
    spec: MCPServerSpec, environ: Mapping[str, str] | None = None
) -> MCPServer:
    """Realize one :class:`MCPServerSpec` into a Pydantic AI MCP server toolset.

    ``environ`` defaults to ``os.environ`` and is used to expand ``${VAR}``
    references. The server's ``id`` is set to the spec name so its tools and
    any errors are attributable to a stable, human-chosen identifier.
    """
    environ = os.environ if environ is None else environ
    where = f"MCP server {spec.name!r}"

    if spec.transport == "stdio":
        if not spec.command:
            raise MCPConfigError(f"{where} uses stdio transport but has no 'command'.")
        env = _expand_mapping(spec.env, environ, where=f"{where} env")
        kwargs: dict[str, object] = {
            "command": _expand(spec.command, environ, where=f"{where} command"),
            "args": [_expand(arg, environ, where=f"{where} args") for arg in spec.args],
            "env": env or None,
            "tool_prefix": spec.tool_prefix,
            "timeout": spec.timeout,
            "id": spec.name,
        }
        if spec.cwd:
            kwargs["cwd"] = _expand(spec.cwd, environ, where=f"{where} cwd")
        if spec.read_timeout is not None:
            kwargs["read_timeout"] = spec.read_timeout
        return MCPServerStdio(**kwargs)  # type: ignore[arg-type]

    if spec.transport in ("http", "sse"):
        if not spec.url:
            raise MCPConfigError(
                f"{where} uses {spec.transport} transport but has no 'url'."
            )
        headers = _expand_mapping(spec.headers, environ, where=f"{where} headers")
        kwargs = {
            "url": _expand(spec.url, environ, where=f"{where} url"),
            "headers": headers or None,
            "tool_prefix": spec.tool_prefix,
            "timeout": spec.timeout,
            "id": spec.name,
        }
        if spec.read_timeout is not None:
            kwargs["read_timeout"] = spec.read_timeout
        cls = MCPServerStreamableHTTP if spec.transport == "http" else MCPServerSSE
        return cls(**kwargs)  # type: ignore[arg-type]

    raise MCPConfigError(f"{where} has unknown transport {spec.transport!r}.")


def build_mcp_servers(
    specs: list[MCPServerSpec], environ: Mapping[str, str] | None = None
) -> list[MCPServer]:
    """Build every configured MCP server, rejecting duplicate names."""
    servers: list[MCPServer] = []
    seen: set[str] = set()
    for spec in specs:
        if spec.name in seen:
            raise MCPConfigError(
                f"Duplicate MCP server name {spec.name!r}; names must be unique."
            )
        seen.add(spec.name)
        servers.append(build_mcp_server(spec, environ))
    return servers
