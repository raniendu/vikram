"""Local tools shared by every interface: a code sandbox, questions, and plan mode.

`run_code` executes model-written Python in a microVM for each reply (see
`sandbox.py`). Questions use Pydantic AI Harness `AskUser`; plan approval uses
core deferred-tool approval. Interfaces only supply the person (`interaction.py`).
"""

import asyncio
import copy
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Annotated, Any, Literal

from pydantic import Field
from pydantic_ai import (
    DeferredToolRequests,
    DeferredToolResults,
    RunContext,
    Tool,
    ToolDenied,
    ToolFailed,
)
from pydantic_ai.capabilities import AbstractCapability, HandleDeferredToolCalls
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai_harness.ask_user import AskUser

from vikram.interaction import (
    AskUserRequest,
    AskUserResponse,
    Interaction,
    PlanRequest,
    PlanReview,
    Unattended,
    check_response,
)
from vikram.sandbox import RUN_TIMEOUT, Sandbox, SandboxUnavailable

Mode = Literal["normal", "plan"]
RUN_CODE, ASK_USER, SUBMIT_PLAN = "run_code", "ask_user_question", "submit_plan"
# Model requests per reply, including the final answer, which never offers tools.
REQUEST_LIMIT = 10
LIMITS = {RUN_CODE: 6, ASK_USER: 3, SUBMIT_PLAN: 3}
INTERACTION_TIMEOUT = 15 * 60
MAX_CODE = 20_000
MAX_OUTPUT = 8_000
MAX_PLAN = 6_000

SANDBOX_PROMPT = (
    "- run_code runs Python 3.13 in an isolated Linux VM with no internet and no "
    "access to the user's files; only the standard library is installed. Use it "
    "for exact arithmetic, counting, dates, and data processing instead of "
    "estimating, then state the result. Each call is a fresh Python process, so "
    "define everything the code needs. Its results come from your code, not from "
    "the user.\n"
)
QUESTIONS_PROMPT = (
    "- ask_user_question shows the user multiple-choice questions and waits for "
    "the answers. If a request is ambiguous and the choice changes the answer "
    "(for example an unstated unit, scale, format, or scope), call it before "
    "answering. Never guess, and never list choices for the user in your reply "
    "text. Do not ask about details you can infer. If the user declines, choose "
    "reasonably and say which choice you made."
)
TEXT_PLAN_PROMPT = (
    "Plan mode is on: the user wants to approve a plan before any work is done. "
    "Do not answer requests that need calculation, research, or several steps. "
    "Reply with a short numbered plan and ask the user to approve or change it."
)
RUN_CODE_DESCRIPTION = (
    "Run Python 3.13 code in an isolated Linux VM and return what it printed. "
    "Each call starts a fresh Python process, so define everything the code needs; "
    "files written under /workspace stay for the rest of this reply. A final bare "
    "expression is printed, as in the Python REPL. The VM has no internet access, "
    "none of the user's files, and only the standard library. Code is stopped "
    f"after {RUN_TIMEOUT} seconds."
)
UNAVAILABLE = "The Python sandbox is unavailable."
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


def toolkit_prompt(sandbox: bool) -> str:
    return "Local tools:\n" + (SANDBOX_PROMPT if sandbox else "") + QUESTIONS_PROMPT


def plan_prompt(sandbox: bool) -> str:
    return (
        "Plan mode is on: the user wants to approve a plan before any work is done. "
        "For any request that needs calculation, code, research, or several steps, "
        "do not answer it yet. "
        + ("run_code is refused until the user approves your plan. " if sandbox else "")
        + "If the request is ambiguous, call ask_user_question first. Then call "
        "submit_plan with a short numbered plan. "
        "If the user approves, plan mode ends: carry out the plan in this same reply "
        "and report the results. If the user requests changes, revise the plan and "
        "call submit_plan again. If the plan is not approved, end your reply by "
        "presenting the plan and asking how to proceed. Only greetings and questions "
        "about the plan itself may be answered without submit_plan."
    )


def bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n… [truncated: {len(text):,} characters in total]"


@dataclass
class Reply:
    """One reply's mode, person, sandbox, and visible tool activity: the `deps`.

    Activity records are plain JSON so interfaces can show, store, and replay them.
    """

    mode: Mode = "normal"
    interaction: Interaction = field(default_factory=Unattended)
    notify: Callable[[str], None] = lambda phase: None
    sandbox: Sandbox | None = None
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

    async def run_code(self, call_id: str, code: str) -> str:
        """Run one snippet in the reply's VM, keeping its record and result bounded.

        Errors, timeouts, and a VM that cannot start are finished failures: the
        model reads them and can fix the code within the reply's run limit, so a
        bad snippet never ends the reply.
        """
        if self.sandbox is None:
            raise ToolFailed(f"{UNAVAILABLE} Answer without running code.")
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
            run = await self.sandbox.run(code)
        except asyncio.CancelledError:
            record["status"] = "stopped"
            raise
        except Exception as error:
            reason = (
                str(error)
                if isinstance(error, SandboxUnavailable)
                else f"The Python sandbox failed ({type(error).__name__})."
            )
            record["status"], record["error"] = "failed", reason
            self.notify("loading")
            raise ToolFailed(f"{reason} Answer without running code.") from None
        self.notify("loading")
        printed, errors = run.output.rstrip("\n"), run.error.strip()
        text = "\n".join(part for part in (printed, errors) if part)
        if run.exit_code != 0:
            record["status"] = "failed"
            record["output"] = bounded(printed, MAX_OUTPUT)
            record["error"] = bounded(errors, MAX_OUTPUT)
            raise ToolFailed(bounded(text, MAX_OUTPUT))
        record["status"], record["output"] = "complete", bounded(text, MAX_OUTPUT)
        if len(text) > MAX_OUTPUT:
            # Large output would crowd out the conversation in a local context window.
            return record["output"] + (
                "\nPrint less, or summarize in code, if more detail is needed."
            )
        return text or "(no output)"


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


async def run_code(
    ctx: RunContext[Reply],
    code: Annotated[
        str,
        Field(
            min_length=1, max_length=MAX_CODE, description="Python source code to run."
        ),
    ],
) -> str:
    reply = _reply(ctx) or Reply()
    if reply.mode == "plan":
        # Refused, not hidden: small models call run_code in plan mode anyway,
        # and an unknown tool spends retries until the reply fails.
        raise ToolFailed(PLAN_FIRST)
    return await reply.run_code(ctx.tool_call_id or "", code)


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
    return (
        "The user approved the plan. Plan mode is off: carry out the plan now"
        + (", using run_code where it helps," if reply and reply.sandbox else "")
        + " and report the results."
    )


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
    """Offers each request's tools: normal mode hides `submit_plan`, and the final
    request offers no tools so the model must answer."""

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


@dataclass
class ReplyPolicy(AbstractCapability[Reply]):
    """Tool availability and question-argument repair for every request."""

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


def toolkit_tools(sandbox: bool) -> list[Tool]:
    tools = [Tool(submit_plan, requires_approval=True)]
    if sandbox:
        tools.append(Tool(run_code, description=RUN_CODE_DESCRIPTION, sequential=True))
    return tools


def toolkit_capabilities() -> list:
    return [
        lambda ctx: (
            Questions(answerer=reply.ask)
            if (reply := _reply(ctx)) is not None
            else None
        ),
        HandleDeferredToolCalls(handler=_review_plans),
        ReplyPolicy(),
    ]
