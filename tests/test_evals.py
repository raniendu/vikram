import asyncio
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
from vikram.evals.site import ScriptedSite, completed_browser
from vikram.sandbox import CodeRun

CASES_BY_ID = {case.id: case for case in CASES}


def setUpModule():
    # Replies get a fake sandbox here; test_toolkit covers the real VM.
    unittest.enterModuleContext(fake_vm())


# Saved history depends on these: the original eight cases stay one series.
STARTER_SUITE = "4bd8114f23132b9132ebe827efef4727e904e3d1dbb2ed4d79f28eef1e4f6a22"
STARTER_PROTOCOL = "54d651d1de0aaf42ed689511058fb4b73baea6ec9eda4f17bdf44d69f56f86f0"
# The three local-tool cases as they were before the browser case was added.
TOOL_CASES = ("sandbox-computation", "clarifying-question", "plan-approval")
TOOL_SUITE = "551cd06b07b55d93294cbb814c63b7f05819aefc5dd9849a14c2e392ef5e99e8"
TOOL_PROTOCOL = "4908b19ba4618d7ebaa29d81b69fad191e2c99b89884103e57a63d701045cdaf"
TOOL_AGENT = {
    True: "26dbd28f401421f82b309497953c08cc4af973e5fa839a9760f200d88ecd8061",
    False: "9694d982b5558f477b9efa1ba4479bd0fa289da9b1252ec3587ede320ce10961",
}


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

    def test_tool_case_series_keep_their_identity_without_the_browser_case(self):
        cases = [case for case in CASES if case.id in TOOL_CASES]
        for sandbox in (True, False):
            config = RunConfig(model="m", tools=True, sandbox=sandbox)
            run = _new_run(config, cases, "", "").record
            self.assertEqual(run.suite_hash, TOOL_SUITE)
            self.assertEqual(run.protocol_hash, TOOL_PROTOCOL)
            self.assertEqual(run.agent_hash, TOOL_AGENT[sandbox])
            browsing = _new_run(config, [*cases, CASES_BY_ID["browser-lookup"]], "", "")
            self.assertNotEqual(browsing.record.protocol_hash, TOOL_PROTOCOL)
            self.assertNotEqual(browsing.record.agent_hash, TOOL_AGENT[sandbox])

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

    async def test_browser_case_needs_the_answer_found_by_browsing(self):
        run, model = await self.evaluate(
            "browser-lookup",
            tool_call("browser_open", {"url": "https://kettles.example/"}, "b1"),
            tool_call("browser_click", {"element": 2}, "b2"),
            "$48",
        )
        self.assertEqual(run.cases[0].passed, 1)
        self.assertIn(
            "browser_click", [t["function"]["name"] for t in model.bodies[0]["tools"]]
        )
        self.assertIn("Price: $48", json.dumps(model.bodies[2]["messages"]))
        # A right answer without using the browser does not pass.
        run, _ = await self.evaluate("browser-lookup", "$48")
        self.assertEqual((run.cases[0].passed, run.cases[0].failed), (0, 1))

    def test_offline_site_follows_links_and_never_leaves_its_pages(self):
        site = ScriptedSite()
        opened = asyncio.run(
            site.act({"action": "open", "url": "https://kettles.example"})
        )
        self.assertEqual(opened["page"]["elements"][1]["name"], "Copper Kettle")
        clicked = asyncio.run(site.act({"action": "click", "element": 2}))
        self.assertIn("Price: $48", clicked["page"]["text"])
        self.assertFalse(asyncio.run(site.act({"action": "click", "element": 9}))["ok"])
        back = asyncio.run(site.act({"action": "back"}))
        self.assertEqual(back["page"]["title"], "Kettle Corner")
        other = asyncio.run(site.act({"action": "open", "url": "https://example.com/"}))
        self.assertFalse(other["ok"])
        self.assertEqual(
            completed_browser([{"kind": "browser", "status": "complete"}]), ["browser"]
        )
        self.assertEqual(
            completed_browser([{"kind": "browser", "status": "failed"}]), []
        )

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
