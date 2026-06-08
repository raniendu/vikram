import pytest
from pydantic_ai.mcp import MCPServerSSE, MCPServerStdio, MCPServerStreamableHTTP

from vikram.mcp import (
    MCPConfigError,
    MCPServerSpec,
    build_mcp_server,
    build_mcp_servers,
)


def test_build_stdio_server_with_env_expansion():
    spec = MCPServerSpec(
        name="github",
        transport="stdio",
        command="${RUNNER}",
        args=["-y", "${PKG}"],
        env={"TOKEN": "${SECRET}"},
        cwd="${WORKDIR}",
        tool_prefix="gh",
    )
    environ = {
        "RUNNER": "npx",
        "PKG": "@modelcontextprotocol/server-github",
        "SECRET": "s3cr3t",
        "WORKDIR": "/srv/app",
    }

    server = build_mcp_server(spec, environ)

    assert isinstance(server, MCPServerStdio)
    assert server.command == "npx"
    assert server.args == ["-y", "@modelcontextprotocol/server-github"]
    assert server.env == {"TOKEN": "s3cr3t"}
    assert server.cwd == "/srv/app"
    assert server.tool_prefix == "gh"
    assert server.id == "github"


def test_stdio_without_env_inherits_parent_environment():
    server = build_mcp_server(
        MCPServerSpec(name="fetch", command="uvx", args=["mcp-server-fetch"]),
        environ={},
    )

    assert isinstance(server, MCPServerStdio)
    # No env configured -> None so the subprocess inherits the parent's env.
    assert server.env is None


def test_build_http_server_with_header_expansion():
    spec = MCPServerSpec(
        name="docs",
        transport="http",
        url="https://${HOST}/mcp",
        headers={"Authorization": "Bearer ${TOKEN}"},
        read_timeout=42.0,
    )

    server = build_mcp_server(spec, {"HOST": "mcp.example.com", "TOKEN": "abc"})

    assert isinstance(server, MCPServerStreamableHTTP)
    assert server.url == "https://mcp.example.com/mcp"
    assert server.headers == {"Authorization": "Bearer abc"}
    assert server.id == "docs"


def test_build_sse_server():
    server = build_mcp_server(
        MCPServerSpec(name="events", transport="sse", url="https://x/sse"), {}
    )

    assert isinstance(server, MCPServerSSE)
    assert server.url == "https://x/sse"
    assert server.id == "events"


def test_environ_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv("VIKRAM_TEST_MCP_TOKEN", "from-os-environ")
    server = build_mcp_server(
        MCPServerSpec(name="x", command="run", env={"T": "${VIKRAM_TEST_MCP_TOKEN}"})
    )
    assert server.env == {"T": "from-os-environ"}


@pytest.mark.parametrize(
    "spec",
    [
        MCPServerSpec(name="x", command="run", env={"K": "${MISSING}"}),
        MCPServerSpec(name="x", command="${MISSING}"),
        MCPServerSpec(name="x", command="run", args=["${MISSING}"]),
        MCPServerSpec(name="y", transport="http", url="https://${MISSING}/mcp"),
        MCPServerSpec(
            name="y",
            transport="http",
            url="https://x/mcp",
            headers={"A": "${MISSING}"},
        ),
    ],
)
def test_missing_env_var_raises(spec):
    with pytest.raises(MCPConfigError, match="MISSING"):
        build_mcp_server(spec, environ={})


def test_stdio_without_command_raises():
    with pytest.raises(MCPConfigError, match="stdio"):
        build_mcp_server(MCPServerSpec(name="x", transport="stdio"), {})


def test_http_without_url_raises():
    with pytest.raises(MCPConfigError, match="url"):
        build_mcp_server(MCPServerSpec(name="x", transport="http"), {})


def test_unknown_transport_raises():
    # transport is normally constrained by the Literal; bypass validation to
    # exercise the defensive branch in build_mcp_server.
    spec = MCPServerSpec.model_construct(name="x", transport="carrier-pigeon")
    with pytest.raises(MCPConfigError, match="unknown transport"):
        build_mcp_server(spec, {})


def test_build_mcp_servers_rejects_duplicate_names():
    specs = [
        MCPServerSpec(name="dup", command="a"),
        MCPServerSpec(name="dup", command="b"),
    ]
    with pytest.raises(MCPConfigError, match="Duplicate MCP server name"):
        build_mcp_servers(specs, {})


def test_build_mcp_servers_returns_all():
    servers = build_mcp_servers(
        [
            MCPServerSpec(name="a", command="a"),
            MCPServerSpec(name="b", transport="http", url="https://b/mcp"),
        ],
        {},
    )
    assert [s.id for s in servers] == ["a", "b"]
