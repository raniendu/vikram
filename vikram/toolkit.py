"""Local tools shared by every interface: a code sandbox, questions, and plan mode.

The sandbox is Pydantic AI Harness `CodeMode` with no tools inside it, so `run_code`
executes model-written Python in Monty: no filesystem, network, environment, clock,
or third-party packages. Questions use Harness `AskUser`; plan approval uses core
deferred-tool approval. Interfaces only supply the person (see `interaction.py`).
"""

import asyncio
import copy
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Annotated, Any, Literal

from pydantic import Field
from pydantic_ai import (
    DeferredToolRequests,
    DeferredToolResults,
    ModelRetry,
    RunContext,
    Tool,
    ToolDenied,
    ToolFailed,
    ToolReturn,
)
from pydantic_ai.capabilities import (
    AbstractCapability,
    CapabilityOrdering,
    HandleDeferredToolCalls,
)
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai_harness.ask_user import AskUser
from pydantic_ai_harness.code_mode import CodeMode

from vikram.interaction import (
    AskUserRequest,
    AskUserResponse,
    Interaction,
    PlanRequest,
    PlanReview,
    Unattended,
    check_response,
)

Mode = Literal["normal", "plan"]
RUN_CODE, ASK_USER, SUBMIT_PLAN = "run_code", "ask_user_question", "submit_plan"
# Model requests per reply, including the final answer, which never offers tools.
REQUEST_LIMIT = 10
LIMITS = {RUN_CODE: 6, ASK_USER: 3, SUBMIT_PLAN: 3}
SANDBOX_LIMITS = {"max_duration_secs": 15, "max_memory": 128 * 1024 * 1024}
SANDBOX_RETRIES = 3
INTERACTION_TIMEOUT = 15 * 60
MAX_CODE = 20_000
MAX_OUTPUT = 8_000
MAX_PLAN = 6_000

TOOLKIT_PROMPT = (
    "Local tools:\n"
    "- run_code runs Python in an isolated sandbox: no internet, files, "
    "environment, clock, or third-party packages, and only part of the standard "
    "library. Use it for exact arithmetic, counting, dates, and data processing "
    "instead of estimating, then state the result. Its results come from your "
    "code, not from the user.\n"
    "- ask_user_question shows the user multiple-choice questions and waits for "
    "the answers. If a request is ambiguous and the choice changes the answer "
    "(for example an unstated unit, scale, format, or scope), call it before "
    "answering. Never guess, and never list choices for the user in your reply "
    "text. Do not ask about details you can infer. If the user declines, choose "
    "reasonably and say which choice you made."
)
PLAN_PROMPT = (
    "Plan mode is on: the user wants to approve a plan before any work is done. "
    "For any request that needs calculation, code, research, or several steps, "
    "do not answer it yet. run_code is refused until the user approves your plan. "
    "If the request is ambiguous, call ask_user_question first. Then call "
    "submit_plan with a short numbered plan. "
    "If the user approves, plan mode ends: carry out the plan in this same reply "
    "and report the results. If the user requests changes, revise the plan and "
    "call submit_plan again. If the plan is not approved, end your reply by "
    "presenting the plan and asking how to proceed. Only greetings and questions "
    "about the plan itself may be answered without submit_plan."
)
TEXT_PLAN_PROMPT = (
    "Plan mode is on: the user wants to approve a plan before any work is done. "
    "Do not answer requests that need calculation, research, or several steps. "
    "Reply with a short numbered plan and ask the user to approve or change it."
)

APPROVED = (
    "The user approved the plan. Plan mode is off: carry out the plan now, using "
    "run_code where it helps, and report the results."
)
PLAN_FIRST = (
    "Plan mode is on, so code cannot run yet. Call submit_plan with your numbered "
    "plan and wait for the user's approval; then run the code."
)
NOT_APPROVED = (
    "The user has not approved the plan yet. Do not carry it out. End your reply "
    "by presenting the plan and asking how to proceed."
)
ASK_USER_DESCRIPTION = (
    "Ask the user to choose before you answer, and wait for the answers. Call this "
    "instead of writing a question in your reply whenever the request is ambiguous "
    "and the choice changes the answer. Give each question a short unique header "
    "and two to six concrete options. The result maps each header to the chosen "
    "labels or the user's own words, or says the user declined."
)
LIMIT_REACHED = {
    RUN_CODE: "The code run limit for this reply was reached. Answer with the results you have.",
    ASK_USER: "The question limit for this reply was reached. Make a reasonable choice and say which one you made.",
    SUBMIT_PLAN: "The plan submission limit for this reply was reached. End your reply with your latest plan.",
}


def bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n… [truncated: {len(text):,} characters in total]"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def display_result(value: Any) -> str:
    """What the sandbox produced, as text for people: printed output, then result."""
    if isinstance(value, dict) and value and set(value) <= {"output", "result"}:
        lines = [str(value["output"]).rstrip("\n")] if value.get("output") else []
        if "result" in value:
            result = value["result"]
            lines.append(result if isinstance(result, str) else _json(result))
        return "\n".join(lines)
    if value is None or value == {}:
        return ""
    return value if isinstance(value, str) else _json(value)


@dataclass
class Reply:
    """One reply's mode, person, and visible tool activity: the agent's `deps`.

    Activity records are plain JSON so interfaces can show, store, and replay them.
    """

    mode: Mode = "normal"
    interaction: Interaction = field(default_factory=Unattended)
    notify: Callable[[str], None] = lambda phase: None
    activity: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def snapshot(self) -> list[dict]:
        return copy.deepcopy(self.activity)

    def _spend(self, tool: str) -> bool:
        used = self.counts.get(tool, 0)
        if used >= LIMITS[tool]:
            return False
        self.counts[tool] = used + 1
        return True

    def _record(self, **fields: Any) -> dict:
        record = dict(fields)
        self.activity.append(record)
        return record

    async def ask(self, request: AskUserRequest) -> AskUserResponse:
        """The `AskUser` answerer: record the question, then wait for the person."""
        if not self._spend(ASK_USER):
            raise ToolFailed(LIMIT_REACHED[ASK_USER])
        record = self._record(
            kind="question",
            id=request.id,
            status="waiting",
            questions=[q.model_dump(mode="json") for q in request.questions],
            answers=None,
        )
        self.notify("asking")
        try:
            async with asyncio.timeout(INTERACTION_TIMEOUT):
                response = await self.interaction.ask(request)
            check_response(request, response)
        except TimeoutError:
            record["status"], response = "timed_out", AskUserResponse(cancelled=True)
        except ValueError:
            # An adapter answer that does not fit the question must not end the reply.
            record["status"], response = "declined", AskUserResponse(cancelled=True)
        except asyncio.CancelledError:
            record["status"] = "stopped"
            raise
        else:
            record["status"] = "declined" if response.cancelled else "answered"
            record["answers"] = {
                answer.header: (
                    [answer.custom_answer]
                    if answer.custom_answer is not None
                    else list(answer.selected)
                )
                for answer in response.answers
            } or None
        self.notify("loading")
        return response

    async def review(self, call_id: str, plan: str) -> PlanReview | None:
        """Show a submitted plan and wait for a decision; `None` past the limit."""
        if not self._spend(SUBMIT_PLAN):
            return None
        record = self._record(
            kind="plan", id=call_id, status="waiting", plan=plan, feedback=None
        )
        self.notify("reviewing")
        try:
            async with asyncio.timeout(INTERACTION_TIMEOUT):
                review = await self.interaction.review_plan(
                    PlanRequest(id=call_id, plan=plan)
                )
        except TimeoutError:
            review = PlanReview(decision="dismiss")
            record["status"] = "timed_out"
        except asyncio.CancelledError:
            record["status"] = "stopped"
            raise
        else:
            record["status"] = {
                "approve": "approved",
                "revise": "changes_requested",
                "dismiss": "not_approved",
            }[review.decision]
            record["feedback"] = review.feedback
        self.notify("loading")
        return review

    async def run_code(self, ctx: RunContext, call_id: str, code: str, handler) -> Any:
        """Execute one `run_code` call, keeping its record and the result bounded."""
        if not self._spend(RUN_CODE):
            raise ToolFailed(LIMIT_REACHED[RUN_CODE])
        record = self._record(
            kind="code",
            id=call_id,
            status="running",
            code=bounded(code, MAX_CODE),
            output="",
            error=None,
        )
        self.notify("sandbox")
        try:
            result = await handler()
        except ModelRetry as error:
            record["status"], record["error"] = "failed", bounded(
                error.message, MAX_OUTPUT
            )
            self.notify("loading")
            if ctx.retry >= SANDBOX_RETRIES:
                # Out of retries: report a finished failure instead of ending the reply.
                raise ToolFailed(record["error"]) from None
            raise
        except ToolFailed as error:
            record["status"], record["error"] = "failed", bounded(
                error.message, MAX_OUTPUT
            )
            self.notify("loading")
            raise
        except asyncio.CancelledError:
            record["status"] = "stopped"
            raise
        value = result.return_value if isinstance(result, ToolReturn) else result
        record["status"], record["output"] = "complete", bounded(
            display_result(value), MAX_OUTPUT
        )
        self.notify("loading")
        if len(_json(value)) > MAX_OUTPUT:
            # Large output would crowd out the conversation in a local context window.
            value = bounded(display_result(value), MAX_OUTPUT) + (
                "\nPrint less, or summarize in code, if more detail is needed."
            )
        if isinstance(result, ToolReturn):
            return ToolReturn(return_value=value, metadata=result.metadata)
        return value


def _one_line(value: str, limit: int) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def repair_questions(args: dict) -> dict:
    """Coerce common local-model deviations into the `ask_user_question` schema.

    Small models often send options as strings, omit `question` or `header`, or
    exceed length limits. Strict validation still runs on the repaired arguments;
    anything that cannot be repaired is returned unchanged for a normal retry.
    """
    questions = args.get("questions", args.get("question"))
    if isinstance(questions, dict):
        questions = [questions]
    if not isinstance(questions, list):
        return args
    repaired, headers = [], set()
    for item in questions[:10]:
        if not isinstance(item, dict):
            return args
        text = item.get("question") or item.get("text") or item.get("header")
        header = item.get("header") or item.get("title") or text
        options = item.get("options") or item.get("choices")
        if not (isinstance(text, str) and isinstance(header, str)):
            return args
        if not isinstance(options, list):
            return args
        header = base = _one_line(header, 25)
        suffix = 2
        while header.casefold() in headers:
            header, suffix = f"{base[:21].rstrip()} {suffix}", suffix + 1
        headers.add(header.casefold())
        choices = []
        for option in options[:6]:
            if isinstance(option, dict):
                label = next(
                    (
                        option[key]
                        for key in ("label", "text", "value", "name", "title")
                        if isinstance(option.get(key), str)
                    ),
                    None,
                )
                description = option.get("description")
            else:
                label, description = option, None
            if not isinstance(label, str):
                return args
            choice = {"label": _one_line(label, 50)}
            if isinstance(description, str) and description.strip():
                choice["description"] = description.strip()[:200]
            choices.append(choice)
        multi = next(
            (
                item[key]
                for key in ("multi_select", "multiSelect", "multiple")
                if isinstance(item.get(key), bool)
            ),
            False,
        )
        repaired.append(
            {
                "header": header,
                "question": text.strip()[:500],
                "options": choices,
                "multi_select": multi,
            }
        )
    return {"questions": repaired}


def _reply(ctx: RunContext) -> Reply | None:
    return ctx.deps if isinstance(ctx.deps, Reply) else None


def mode_of(ctx: RunContext) -> Mode:
    reply = _reply(ctx)
    return reply.mode if reply else "normal"


async def submit_plan(
    ctx: RunContext[Reply],
    plan: Annotated[str, Field(min_length=1, max_length=MAX_PLAN)],
) -> str:
    """Present your numbered plan to the user for approval, in plan mode only.

    Call it once the plan is ready. On approval, plan mode ends and you carry out
    the plan in this reply. A request for changes includes the user's feedback.
    """
    reply = _reply(ctx)
    if reply is not None:
        reply.mode = "normal"
    return APPROVED


async def _review_plans(
    ctx: RunContext[Reply], requests: DeferredToolRequests
) -> DeferredToolResults:
    """Resolve `submit_plan` approvals inline by asking the person."""
    reply = _reply(ctx) or Reply()
    results = DeferredToolResults()
    for call in requests.approvals:
        if call.tool_name != SUBMIT_PLAN:
            results.approvals[call.tool_call_id] = ToolDenied(
                "This tool is unavailable."
            )
            continue
        review = await reply.review(
            call.tool_call_id, str(call.args_as_dict().get("plan", ""))
        )
        if review is None:
            results.approvals[call.tool_call_id] = ToolDenied(
                LIMIT_REACHED[SUBMIT_PLAN]
            )
        elif review.decision == "approve":
            results.approvals[call.tool_call_id] = True
        elif review.decision == "revise":
            feedback = review.feedback or "No details were given."
            results.approvals[call.tool_call_id] = ToolDenied(
                "The user asked for changes before approving: "
                f"{feedback}\nRevise the plan and call submit_plan again."
            )
        else:
            results.approvals[call.tool_call_id] = ToolDenied(NOT_APPROVED)
    for call in requests.calls:
        results.calls[call.tool_call_id] = ToolFailed("This tool is unavailable.")
    return results


@dataclass(kw_only=True)
class Questions(AskUser):
    """Harness `AskUser`; its guidance lives in Vikram's own instructions."""

    def get_instructions(self):
        return None


def _available(ctx: RunContext, tool_def) -> bool:
    if ctx.usage.requests >= REQUEST_LIMIT - 1:
        return False
    if tool_def.name == SUBMIT_PLAN:
        return mode_of(ctx) == "plan"
    return True


@dataclass
class ReplyToolset(WrapperToolset[Reply]):
    """Offers each request's tools and runs `run_code` with bounded, recorded results.

    It wraps CodeMode's toolset, so it sees `run_code` and its raw `ModelRetry`
    before the retry budget is checked: an exhausted budget becomes a failed
    result the model can adapt to, instead of an error that ends the reply.
    """

    async def get_tools(self, ctx: RunContext[Reply]) -> dict:
        tools = {}
        for name, tool in (await super().get_tools(ctx)).items():
            if not _available(ctx, tool.tool_def):
                continue
            if name == ASK_USER:
                tool = replace(
                    tool,
                    tool_def=replace(tool.tool_def, description=ASK_USER_DESCRIPTION),
                )
            tools[name] = tool
        return tools

    async def call_tool(self, name, tool_args, ctx, tool) -> Any:
        call = super().call_tool
        reply = _reply(ctx)
        if name != RUN_CODE or reply is None:
            return await call(name, tool_args, ctx, tool)
        if reply.mode == "plan":
            # Refused, not hidden: small models call run_code in plan mode anyway,
            # and an unknown tool spends retries until the reply fails.
            raise ToolFailed(PLAN_FIRST)
        return await reply.run_code(
            ctx,
            ctx.tool_call_id or "",
            str(tool_args.get("code", "")),
            lambda: call(name, tool_args, ctx, tool),
        )


@dataclass
class ReplyPolicy(AbstractCapability[Reply]):
    """Tool availability and argument repair for every request of a reply.

    Plan mode refuses the sandbox until a plan is approved, normal mode hides
    `submit_plan`, and the final request offers no tools so the model must answer.
    It sits outside CodeMode so the sandbox follows the same rules.
    """

    def get_ordering(self) -> CapabilityOrdering:
        return CapabilityOrdering(position="outermost", wraps=[CodeMode])

    def get_wrapper_toolset(self, toolset):
        return ReplyToolset(toolset)

    async def before_tool_validate(self, ctx, *, call, tool_def, args):
        if call.tool_name != ASK_USER:
            return args
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                return args
        return repair_questions(args) if isinstance(args, dict) else args


def check_sandbox() -> int:
    """Run one snippet in Monty without a model, to check a packaged runtime."""
    from pydantic_monty import Monty

    try:
        with Monty() as pool, pool.checkout() as session:
            ready = session.feed_run("sum(range(10))") == 45
    except Exception as error:
        ready, reason = False, type(error).__name__
    else:
        reason = "unexpected result"
    if not ready:
        sys.stderr.write(f"The Python sandbox is unavailable ({reason}).\n")
        return 1
    sys.stdout.write("The Python sandbox is ready.\n")
    return 0


def toolkit_tools() -> list[Tool]:
    return [Tool(submit_plan, requires_approval=True)]


def toolkit_capabilities() -> list:
    return [
        lambda ctx: (
            Questions(answerer=reply.ask)
            if (reply := _reply(ctx)) is not None
            else None
        ),
        CodeMode(
            tools=[], max_retries=SANDBOX_RETRIES + 1, resource_limits=SANDBOX_LIMITS
        ),
        HandleDeferredToolCalls(handler=_review_plans),
        ReplyPolicy(),
    ]
