import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from test_toolkit import FakeSandbox, fake_vm

from vikram.agent import build_agent
from vikram.evals import cli
from vikram.evals.cases import CASES, completed_tools
from vikram.evals.history import RunConfig, save_run
from vikram.evals.runner import _new_run, evaluate_many
from vikram.sandbox import CodeRun


def setUpModule():
    # Replies get a fake sandbox here; test_toolkit covers the real VM.
    unittest.enterModuleContext(fake_vm())


# Saved history depends on these: the original eight cases stay one series.
STARTER_SUITE = "4bd8114f23132b9132ebe827efef4727e904e3d1dbb2ed4d79f28eef1e4f6a22"
STARTER_PROTOCOL = "54d651d1de0aaf42ed689511058fb4b73baea6ec9eda4f17bdf44d69f56f86f0"


def tool_call(name, arguments, call_id):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


class JsonModel:
    """A scripted non-streaming OpenAI-compatible endpoint, as evaluations use."""

    def __init__(self, *steps):
        self.steps, self.bodies = list(steps), []

    async def __call__(self, request):
        body = json.loads(request.content)
        self.bodies.append(body)
        step = self.steps.pop(0) if self.steps else "Done"
        message = (
            {"role": "assistant", "content": step} if isinstance(step, str) else step
        )
        return httpx.Response(
            200,
            json={
                "id": "r",
                "object": "chat.completion",
                "created": 0,
                "model": "local:1",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": (
                            "tool_calls" if message.get("tool_calls") else "stop"
                        ),
                    }
                ],
            },
        )


class SuiteIdentityTests(unittest.TestCase):
    def test_starter_cases_keep_their_suite_and_protocol_identity(self):
        starter = [case for case in CASES if not case.uses_tools]
        self.assertEqual(len(starter), 8)
        run = _new_run(RunConfig(model="m"), starter, "", "").record
        self.assertEqual(run.suite_hash, STARTER_SUITE)
        self.assertEqual(run.protocol_hash, STARTER_PROTOCOL)
        everything = _new_run(RunConfig(model="m", tools=True), CASES, "", "").record
        self.assertNotEqual(everything.suite_hash, STARTER_SUITE)
        self.assertNotEqual(everything.protocol_hash, STARTER_PROTOCOL)
        self.assertNotEqual(everything.agent_hash, run.agent_hash)

    def test_completed_tools_name_only_finished_work(self):
        activity = [
            {"kind": "question", "status": "declined"},
            {"kind": "plan", "status": "approved"},
            {"kind": "code", "status": "failed"},
        ]
        self.assertEqual(completed_tools(activity), ["submit_plan"])
        activity.append({"kind": "code", "status": "complete"})
        self.assertEqual(completed_tools(activity), ["run_code", "submit_plan"])


class ToolCaseTests(unittest.IsolatedAsyncioTestCase):
    async def evaluate(self, case_id, *steps, output="ok\n"):
        cases = [case for case in CASES if case.id == case_id]
        config = RunConfig(model="local:1", samples=1, ks=[1], tools=True, sandbox=True)
        model = JsonModel(*steps)
        self.sandboxes = []

        def sandbox():
            self.sandboxes.append(FakeSandbox(CodeRun(0, output, "")))
            return self.sandboxes[-1]

        async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as client:
            runs = await evaluate_many(
                [config],
                cases,
                lambda c: build_agent(
                    c.model,
                    web="off",
                    toolkit=True,
                    sandbox=c.sandbox,
                    http_client=client,
                ),
                None,
                lambda message: None,
                lambda run: None,
                sandbox=sandbox,
            )
        return runs[0], model

    async def test_question_case_passes_only_when_the_user_was_asked(self):
        question = {
            "questions": [
                {
                    "header": "Scale",
                    "question": "Which scale is it in?",
                    "options": [{"label": "Celsius"}, {"label": "Fahrenheit"}],
                }
            ]
        }
        run, model = await self.evaluate(
            "clarifying-question",
            tool_call("ask_user_question", question, "q1"),
            "-3.9",
        )
        self.assertEqual(run.cases[0].passed, 1)
        self.assertIn("Fahrenheit; convert", json.dumps(model.bodies[1]["messages"]))
        self.assertFalse(
            any(t["function"]["name"] == "web_search" for t in model.bodies[0]["tools"])
        )
        run, _ = await self.evaluate("clarifying-question", "-3.9")
        self.assertEqual((run.cases[0].passed, run.cases[0].failed), (0, 1))

    async def test_plan_case_needs_an_approved_plan_then_a_sandbox_run(self):
        run, model = await self.evaluate(
            "plan-approval",
            tool_call("submit_plan", {"plan": "1. Count with code."}, "p1"),
            tool_call(
                "run_code",
                {"code": "n = 100000\nn // 7 + n // 11 - 2 * (n // 77)"},
                "c1",
            ),
            "20779",
            output="20779\n",
        )
        self.assertEqual(run.cases[0].passed, 1)
        self.assertIn("Plan mode is on", model.bodies[0]["messages"][0]["content"])
        self.assertIn("20779", json.dumps(model.bodies[2]["messages"]))
        # Each attempt gets its own VM, deleted when the attempt ends.
        self.assertEqual(len(self.sandboxes), 1)
        self.assertTrue(self.sandboxes[0].closed)
        run, _ = await self.evaluate("plan-approval", "20779")
        self.assertEqual(run.cases[0].failed, 1)

    async def test_saved_records_hold_metrics_not_prompts_code_or_answers(self):
        run, _ = await self.evaluate(
            "sandbox-computation",
            tool_call(
                "run_code",
                {
                    "code": "sum(n for n in range(2, 10000) if all(n % d for d in range(2, int(n ** 0.5) + 1)))"
                },
                "c1",
            ),
            "5736396",
            output="5736396\n",
        )
        self.assertEqual(run.cases[0].passed, 1)
        self.assertTrue(run.config.tools)
        self.assertTrue(run.config.sandbox)
        with tempfile.TemporaryDirectory() as root:
            saved = save_run(run, Path(root)).read_text()
        for private in ("prime", "range(2, 10000)", "5736396", "Reply with only"):
            self.assertNotIn(private, saved)
        self.assertIn("smolmachines", saved)


class EvalCliTests(unittest.TestCase):
    def test_benchmarks_think_by_default_and_sweep_only_when_asked(self):
        from vikram.evals.thinking import select_levels
        from vikram.ollama import ThinkingControls

        controls = ThinkingControls(values=[False, True, "medium"], default=True)
        self.assertEqual(select_levels(controls, ["on"]), [True])
        self.assertEqual(select_levels(controls, ["all"]), [False, True, "medium"])
        self.assertEqual(select_levels(controls, ["off"]), [False])
        runs = []

        async def benchmark(configs, *args):
            runs.extend(configs)
            return 0

        errors = io.StringIO()
        with (
            patch.object(cli, "discover_thinking", return_value=controls),
            patch.object(cli, "discover_tools", return_value=True),
            patch.object(cli, "_benchmark", side_effect=benchmark),
            contextlib.redirect_stderr(errors),
        ):
            status = cli.main(["run", "--model", "local:1", "--case", "arithmetic"])
        self.assertEqual(status, 0, errors.getvalue())
        self.assertEqual([config.thinking for config in runs], [True])
        self.assertEqual([config.sandbox for config in runs], [True])
        self.assertIn("new VM per attempt", errors.getvalue())

    def test_sandbox_cases_need_a_host_that_runs_the_vm(self):
        reason = "The Python sandbox needs an Apple silicon Mac."
        for case, expected in (("plan-approval", 1), ("clarifying-question", 0)):
            runs, errors = [], io.StringIO()

            async def benchmark(configs, *args):
                runs.extend(configs)
                return 0

            with (
                patch("vikram.sandbox.unavailable_reason", return_value=reason),
                patch.object(cli, "discover_thinking", return_value=None),
                patch.object(cli, "discover_tools", return_value=True),
                patch.object(cli, "_benchmark", side_effect=benchmark),
                contextlib.redirect_stderr(errors),
            ):
                status = cli.main(
                    [
                        "run",
                        "--case",
                        case,
                        "--thinking",
                        "default",
                        "--samples",
                        "1",
                        "--k",
                        "1",
                    ]
                )
            self.assertEqual(status, expected, errors.getvalue())
            self.assertIn(reason, errors.getvalue())
        # Without a VM, the agent keeps questions and plan mode only.
        self.assertEqual([(c.tools, c.sandbox) for c in runs], [(True, False)])

    def test_tool_cases_require_a_tool_capable_model(self):
        errors = io.StringIO()
        with (
            patch.object(cli, "discover_thinking", return_value=None),
            patch.object(cli, "discover_tools", return_value=False),
            contextlib.redirect_stderr(errors),
        ):
            status = cli.main(
                [
                    "run",
                    "--model",
                    "local:1",
                    "--case",
                    "sandbox-computation",
                    "--thinking",
                    "default",
                    "--samples",
                    "1",
                    "--k",
                    "1",
                ]
            )
        self.assertEqual(status, 1)
        self.assertIn("does not advertise tool calling", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
