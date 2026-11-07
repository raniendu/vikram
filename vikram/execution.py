"""Shared model-facing adapters for host tools, integrations and VM composition."""

import asyncio
import json
from dataclasses import replace

from pydantic_ai import Tool
from pydantic_ai.toolsets import FunctionToolset

from vikram.composition import CompositionState, approval_wait, compose_tools
from vikram.interaction import Approver, argument_preview


class ObservedApproval:
    def __init__(self, reply, record):
        self.reply, self.record = reply, record

    async def approve(self, request):
        details = request.details or argument_preview(self.record.get("arguments", {}))
        request = replace(request, details=details)
        self.record.update(
            status="waiting",
            approval={
                "id": request.id,
                "summary": request.summary,
                "reason": request.reason,
                "url": request.url,
                "details": details,
            },
        )
        self.reply.notify("approving")
        try:
            with approval_wait():
                if isinstance(self.reply.interaction, Approver):
                    return await self.reply.interaction.approve(request)
                return False
        finally:
            self.record["status"] = "running"


def result_value(result):
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if isinstance(result, dict):
        return result
    return {"ok": True, "output": str(result)}


async def tool_call(reply, name, args, call_id, *, request_index=0):
    """All direct and composed calls share one auditable execution route."""
    if reply.counts.get("host", 0) >= 100:
        return {"ok": False, "error": "The reply's tool-call budget is exhausted."}
    reply.counts["host"] = reply.counts.get("host", 0) + 1
    kind = (
        "mcp"
        if name.startswith("mcp__")
        else "extension" if name.startswith("ext__") else "coding"
    )
    record = {
        "kind": kind,
        "id": call_id,
        "tool": name,
        "status": "running",
        "arguments": args,
    }
    reply.activity.append(record)
    reply.notify("coding")
    approval = ObservedApproval(reply, record)
    try:
        if kind == "coding":
            guidance, must_review = scoped_instructions(
                reply, name, args, request_index
            )
            if must_review:
                record.update(
                    status="declined",
                    output=guidance,
                    error="Review these workspace instructions before attempting the change in a new model request.",
                )
                return {"ok": False, "output": guidance, "error": record["error"]}
            previous = reply.coding.on_output

            async def output(text):
                record["output"] = (record.get("output", "") + text)[-16_000:]
                reply.notify("coding")

            if name == "bash":
                reply.coding.on_output = output
            try:
                result = await reply.coding.dispatch(
                    name, args, interaction=approval, mode=reply.mode, call_id=call_id
                )
            finally:
                if name == "bash":
                    reply.coding.on_output = previous
        elif kind == "mcp":
            manager = reply.coding.mcp
            if name == "mcp__read_resource":
                result = await manager.read_resource(
                    **args, call_id=call_id, interaction=approval, mode=reply.mode
                )
            else:
                result = await manager.call_tool(
                    name, args, call_id=call_id, interaction=approval, mode=reply.mode
                )
        else:
            manager = reply.coding.extensions
            # A manager has one approval adapter; serialize it across indirect calls.
            async with reply.integration_lock:
                manager.interaction, manager.mode = approval, reply.mode
                result = await manager.call_tool(name, args, call_id=call_id)
        value = result_value(result)
        if kind == "coding" and guidance:
            value["instructions"] = guidance
        record.update(
            status="complete" if value.get("ok") else "failed",
            output=str(value.get("output") or "")[:16_000],
            error=value.get("error"),
            diff=value.get("diff"),
        )
        if value.get("ui"):
            record["ui"] = value["ui"]
        return value
    except asyncio.CancelledError:
        record["status"] = "stopped"
        raise
    except Exception:
        record.update(
            status="failed",
            error="The tool failed; completed effects will not be retried automatically.",
        )
        return {"ok": False, "error": record["error"]}
    finally:
        reply.notify("coding")


def _tool(reply, name, description, schema, *, sequential=False):
    async def invoke(ctx, **arguments):
        return await tool_call(
            reply, name, arguments, ctx.tool_call_id, request_index=ctx.usage.requests
        )

    return Tool.from_schema(
        invoke,
        name=name,
        description=description,
        json_schema=schema,
        takes_ctx=True,
        sequential=sequential,
    )


def _definition(definition):
    return definition.model_dump() if hasattr(definition, "model_dump") else definition


def scoped_instructions(reply, name, arguments, request_index):
    """A new subtree's rules reach the model before any mutation can proceed."""
    from pathlib import Path

    from vikram.coding import TOOL_SPECS
    from vikram.resources import ProjectResources

    if not reply.coding.settings.is_trusted():
        return "", False
    resources = ProjectResources(reply.coding.settings)
    target = Path(arguments.get("path") or arguments.get("cwd") or ".")
    if not target.is_absolute():
        target = reply.coding.workspace / target
    target = target.resolve()
    if not target.is_relative_to(reply.coding.workspace):
        return "", False
    full, root = resources.instructions(target), resources.instructions()
    extra = full[len(root) :].strip() if full.startswith(root) else full
    if not extra:
        return "", False
    if not hasattr(reply, "instruction_scopes"):
        reply.instruction_scopes = {}
    scope = str(target if target.is_dir() else target.parent)
    key = (scope, extra)
    if key not in reply.instruction_scopes:
        if len(reply.instructions) + len(extra) > 64_000:
            return (
                "Workspace instruction limit reached. Narrow the task before changing this subtree.",
                True,
            )
        reply.instruction_scopes[key] = request_index
        reply.instructions += f"\n\nInstructions applying only within {scope}:\n{extra}"
    return extra, bool(
        TOOL_SPECS[name].mutation and reply.instruction_scopes[key] >= request_index
    )


async def execution_tools(reply, *, builtin_tools=()):
    """A mutable toolset: MCP search exposes matching schemas on the next request."""
    toolset = FunctionToolset()
    if reply.coding is None or reply.scheduled:
        return toolset
    from vikram.coding import TOOL_SPECS
    from vikram.integrations import start
    from vikram.toolkit import _available

    reply.coding.mode = reply.mode
    await start(reply.coding)
    reply.integration_lock = asyncio.Lock()
    for service in (reply.coding, reply.coding.mcp, reply.coding.extensions):
        service.begin_reply()
    reply.coding.extensions.mode = reply.mode
    composition = CompositionState()
    read_only = {}

    def register(item):
        item = _definition(item)
        name = item["name"]
        read_only[name] = item.get("read_only", False)
        if name not in toolset.tools:
            toolset.add_tool(
                _tool(
                    reply,
                    name,
                    item.get("description", ""),
                    item.get("input_schema", item.get("schema", {"type": "object"})),
                    sequential=not read_only[name],
                )
            )

    if reply.coding.settings.is_trusted():
        for name, spec in TOOL_SPECS.items():
            register(
                {
                    "name": name,
                    "description": spec.description,
                    "schema": spec.schema,
                    "read_only": not spec.mutation,
                }
            )
    for item in await reply.coding.mcp.list_tools(exposed_only=True):
        register(item)
    for item in await reply.coding.extensions.list_tools():
        register(item)

    async def search_tools(query: str, limit: int = 10) -> list[dict]:
        """Search connected MCP tools and make matching schemas available for subsequent calls."""
        matches = await reply.coding.mcp.search_tools(query[:1000], limit)
        for item in matches:
            register(item)
        return [item.model_dump() for item in matches]

    async def list_resources(server: str | None = None) -> dict:
        """List connected MCP resource URIs and parameterized URI templates."""
        manager = reply.coding.mcp
        return {
            "resources": await manager.list_resources(server),
            "templates": await manager.list_resource_templates(server),
        }

    if reply.coding.settings.get("mcp_servers"):
        toolset.add_tool(Tool(search_tools, name="mcp_search_tools", sequential=True))
        toolset.add_tool(
            Tool(list_resources, name="mcp_list_resources", sequential=True)
        )
        register(
            {
                "name": "mcp__read_resource",
                "description": "Read an MCP resource by server name and URI, under the server's permission policy.",
                "schema": {
                    "type": "object",
                    "properties": {
                        "server": {"type": "string"},
                        "uri": {"type": "string"},
                    },
                    "required": ["server", "uri"],
                    "additionalProperties": False,
                },
            }
        )

    async def compose(ctx, code: str) -> dict:
        """Compose available tools with Python in the isolated VM. Use await call_tool(name, **arguments); tools contains schemas. Supports asyncio.gather for reads. No host Python, network, mounts or recursive VM calls. Print or end with the desired result."""
        if reply.mode == "plan":
            return {
                "ok": False,
                "error": "Approve the plan before running Python composition.",
            }
        functions = {
            tool.name: tool
            for tool in [*builtin_tools, *toolset.tools.values()]
            if tool.name not in {"run_code", "compose_tools", "submit_plan"}
        }
        registry = []
        for name, tool in functions.items():
            definition = await tool.prepare_tool_def(ctx)
            if definition is not None and _available(ctx, definition):
                registry.append(
                    {
                        "name": name,
                        "description": definition.description,
                        "input_schema": definition.parameters_json_schema,
                        "read_only": read_only.get(name, False),
                    }
                )
        allowed = {item["name"] for item in registry}

        async def dispatch(name, arguments, identifier):
            if name not in allowed:
                return {"ok": False, "error": "This tool is unavailable."}
            tool = functions[name]
            context = replace(ctx, tool_name=name, tool_call_id=identifier)
            definition = await tool.prepare_tool_def(context)
            if definition is None or not _available(context, definition):
                return {
                    "ok": False,
                    "error": "This tool's budget is exhausted or it is unavailable.",
                }
            arguments = tool.function_schema.validator.validate_python(arguments)
            return result_value(await tool.function_schema.call(arguments, context))

        record = {
            "kind": "composition",
            "id": ctx.tool_call_id,
            "status": "running",
            "code": code,
        }
        reply.activity.append(record)
        reply.notify("coding")
        try:
            result = await compose_tools(
                code, dispatcher=dispatch, tools=registry, state=composition
            )
            record.update(
                status="complete" if result.get("ok") else "failed",
                output=result.get("output", ""),
                error=result.get("error"),
            )
            return result
        except asyncio.CancelledError:
            record["status"] = "stopped"
            raise
        finally:
            reply.notify("coding")

    if reply.sandbox is not None and toolset.tools:
        toolset.add_tool(
            Tool(compose, name="compose_tools", takes_ctx=True, sequential=True)
        )
    return toolset
