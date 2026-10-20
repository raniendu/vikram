import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from vikram import toolkit
from vikram.context import activity_note, context_messages
from vikram.conversation import stream_reply
from vikram.interaction import (
    AskUserAnswer,
    AskUserResponse,
    PlanReview,
    ScriptedInteraction,
)
from vikram.toolkit import repair_questions


def sse(payload):
    return "data: " + json.dumps(payload) + "\n\n"


def tool_call(name, arguments, call_id="c1"):
    return {
        "tool_calls": [
            {
                "index": 0,
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ]
    }


class Model:
    """A scripted OpenAI-compatible stream; each step sees the request body."""

    def __init__(self, *steps):
        self.steps, self.bodies = list(steps), []

    def offered(self, index):
        return [t["function"]["name"] for t in self.bodies[index].get("tools", [])]

    async def __call__(self, request):
        body = json.loads(request.content)
        self.bodies.append(body)
        step = self.steps.pop(0) if self.steps else {"content": "Done"}
        delta = step(body) if callable(step) else step
        if isinstance(delta, str):
            delta = {"content": delta}
        finish = "tool_calls" if "tool_calls" in delta else "stop"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=sse(
                {
                    "id": "r",
                    "model": "local:1",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
            )
            + "data: [DONE]\n\n",
        )


async def reply(model, *, mode="normal", interaction=None, prompt="Go"):
    return [
        event
        async for event in stream_reply(
            "local:1",
            False,
            [],
            [prompt],
            web="auto",
            session_id="a" * 32,
            transport=httpx.MockTransport(model),
            mode=mode,
            interaction=interaction,
        )
    ]


def tool_results(body):
    return [m["content"] for m in body["messages"] if m["role"] == "tool"]


class Person:
    def __init__(self, answers=(), reviews=()):
        self.answers, self.reviews = list(answers), list(reviews)
        self.requests, self.plans = [], []

    async def ask(self, request):
        self.requests.append(request)
        return self.answers.pop(0)(request)

    async def review_plan(self, plan):
        self.plans.append(plan)
        return self.reviews.pop(0)


class SandboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_code_runs_in_monty_and_reports_bounded_results(self):
        model = Model(
            tool_call("run_code", {"code": "x = sum(range(10))\nprint('sum')\nx"}),
            tool_call("run_code", {"code": "print('y' * 20000)"}, "c2"),
            "The sum is 45.",
        )
        events = await reply(model)
        self.assertIn("run_code", model.offered(0))
        self.assertNotIn("submit_plan", model.offered(0))
        first, second = events[-1]["activity"]
        self.assertEqual(first["status"], "complete")
        self.assertEqual(first["output"], "sum\n45")
        self.assertEqual(second["status"], "complete")
        self.assertIn("truncated", second["output"])
        self.assertLess(len(second["output"]), toolkit.MAX_OUTPUT + 100)
        returned = tool_results(model.bodies[2])[-1]
        self.assertIn("truncated", returned)
        self.assertLess(len(returned), toolkit.MAX_OUTPUT + 200)
        self.assertEqual(events[-1]["text"], "The sum is 45.")
        self.assertIn("sandbox", [e["phase"] for e in events])

    async def test_sandbox_has_no_host_access_and_failures_never_end_the_reply(self):
        attempts = [
            "open('/etc/passwd').read()",
            "import os\nos.environ['HOME']",
            "import subprocess",
            "def broken(:",
            "raise ValueError('still failing')",
        ]
        model = Model(
            *(
                tool_call("run_code", {"code": code}, f"c{i}")
                for i, code in enumerate(attempts)
            ),
            "I could not compute it in the sandbox.",
        )
        events = await reply(model)
        records = events[-1]["activity"]
        self.assertEqual([r["status"] for r in records], ["failed"] * 5)
        self.assertIn("PermissionError", records[0]["error"])
        self.assertIn("not supported", records[1]["error"])
        self.assertIn("ModuleNotFoundError", records[2]["error"])
        self.assertEqual(events[-1]["text"], "I could not compute it in the sandbox.")
        # The fourth consecutive failure is reported as a finished failure.
        self.assertIn("ValueError", tool_results(model.bodies[-1])[-1])

    async def test_code_run_budget_is_enforced_per_reply(self):
        model = Model(
            *(
                tool_call("run_code", {"code": f"{i} + 1"}, f"c{i}")
                for i in range(toolkit.LIMITS["run_code"] + 1)
            ),
            "Answer",
        )
        events = await reply(model)
        self.assertEqual(len(events[-1]["activity"]), toolkit.LIMITS["run_code"])
        self.assertIn("limit", tool_results(model.bodies[-1])[-1])

    async def test_final_request_offers_no_tools(self):
        def step(body):
            # A model keeps calling tools for as long as any are offered.
            if body.get("tools"):
                return tool_call("run_code", {"code": "1"}, f"c{len(model.bodies)}")
            return "Done"

        model = Model(*([step] * toolkit.REQUEST_LIMIT))
        with patch.dict(toolkit.LIMITS, {"run_code": 100}):
            events = await reply(model)
        self.assertEqual(len(model.bodies), toolkit.REQUEST_LIMIT)
        self.assertFalse(model.bodies[-1].get("tools"))
        self.assertEqual(events[-1]["text"], "Done")


class QuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_repaired_questions_reach_the_person_and_answers_reach_the_model(
        self,
    ):
        person = Person(
            answers=[
                lambda request: AskUserResponse(
                    answers=(
                        AskUserAnswer(
                            header=request.questions[0].header,
                            selected=("Fahrenheit",),
                        ),
                    )
                )
            ]
        )
        model = Model(
            # A typical small-model call: string options and a missing question.
            tool_call(
                "ask_user_question",
                {
                    "questions": [
                        {
                            "header": "Which temperature scale is it in?",
                            "options": ["Celsius", "Fahrenheit"],
                            "required": True,
                        }
                    ]
                },
            ),
            "It is -3.9 °C.",
        )
        events = await reply(model, interaction=person)
        request = person.requests[0]
        self.assertLessEqual(len(request.questions[0].header), 25)
        self.assertTrue(request.questions[0].header.endswith("…"))
        self.assertEqual(
            [o.label for o in request.questions[0].options], ["Celsius", "Fahrenheit"]
        )
        record = events[-1]["activity"][0]
        self.assertEqual(record["kind"], "question")
        self.assertEqual(record["status"], "answered")
        self.assertEqual(list(record["answers"].values()), [["Fahrenheit"]])
        self.assertIn("Fahrenheit", tool_results(model.bodies[1])[0])
        self.assertIn("asking", [e["phase"] for e in events])
        self.assertIn("ask_user_question", model.offered(0))

    async def test_unattended_and_timed_out_questions_continue_the_reply(self):
        question = tool_call(
            "ask_user_question",
            {
                "questions": [
                    {
                        "header": "Format",
                        "question": "Which format?",
                        "options": [{"label": "CSV"}, {"label": "JSON"}],
                    }
                ]
            },
        )
        model = Model(question, "I chose CSV.")
        events = await reply(model)
        self.assertEqual(events[-1]["activity"][0]["status"], "declined")
        self.assertIn("declined", tool_results(model.bodies[1])[0])

        class Silent:
            async def ask(self, request):
                await asyncio.Event().wait()

        model = Model(question, "I chose CSV.")
        with patch.object(toolkit, "INTERACTION_TIMEOUT", 0.01):
            events = await reply(model, interaction=Silent())
        self.assertEqual(events[-1]["activity"][0]["status"], "timed_out")
        self.assertEqual(events[-1]["text"], "I chose CSV.")

    async def test_scripted_person_prefers_matching_options_then_typed_answer(self):
        from pydantic_ai_harness.ask_user import AskUserRequest, Question

        request = AskUserRequest(
            questions=(
                Question(
                    header="Scale",
                    question="Which scale?",
                    options=({"label": "Celsius"}, {"label": "Fahrenheit"}),
                ),
            )
        )
        picked = await ScriptedInteraction(pattern="fahr").ask(request)
        self.assertEqual(picked.answers[0].selected, ("Fahrenheit",))
        typed = await ScriptedInteraction("It is Fahrenheit").ask(request)
        self.assertEqual(typed.answers[0].custom_answer, "It is Fahrenheit")
        self.assertTrue((await ScriptedInteraction().ask(request)).cancelled)

    def test_repair_keeps_valid_arguments_and_leaves_unrepairable_ones(self):
        valid = {
            "questions": [
                {
                    "header": "Unit",
                    "question": "Which unit?",
                    "options": [
                        {"label": "km", "description": "Metric"},
                        {"label": "mi"},
                    ],
                    "multi_select": False,
                }
            ]
        }
        self.assertEqual(repair_questions(valid), valid)
        self.assertEqual(repair_questions({"questions": "nope"}), {"questions": "nope"})
        repaired = repair_questions(
            {
                "questions": {
                    "question": "Pick\ncolours",
                    "choices": [{"text": "Red"}, "Blue"],
                    "multiSelect": True,
                }
            }
        )
        question = repaired["questions"][0]
        self.assertEqual(question["header"], "Pick colours")
        self.assertTrue(question["multi_select"])
        self.assertEqual([o["label"] for o in question["options"]], ["Red", "Blue"])


class PlanModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_mode_hides_the_sandbox_until_the_person_approves(self):
        person = Person(
            reviews=[
                PlanReview(decision="revise", feedback="Also double-check it."),
                PlanReview(decision="approve"),
            ]
        )
        model = Model(
            # Calling the hidden sandbox in plan mode must not execute anything.
            tool_call("run_code", {"code": "print('should not run')"}),
            tool_call("submit_plan", {"plan": "1. Compute."}, "p1"),
            tool_call("submit_plan", {"plan": "1. Compute.\n2. Verify."}, "p2"),
            tool_call("run_code", {"code": "6 * 7"}, "c2"),
            "The answer is 42.",
        )
        events = await reply(model, mode="plan", interaction=person)
        self.assertNotIn("run_code", model.offered(0))
        self.assertIn("submit_plan", model.offered(0))
        self.assertIn("Unknown tool", json.dumps(model.bodies[1]["messages"]))
        self.assertIn("Also double-check it.", tool_results(model.bodies[2])[-1])
        self.assertEqual(person.plans, ["1. Compute.", "1. Compute.\n2. Verify."])
        self.assertIn("run_code", model.offered(3))
        self.assertNotIn("submit_plan", model.offered(3))
        self.assertIn("Plan mode is on", model.bodies[0]["messages"][0]["content"])
        self.assertNotIn("Plan mode is on", model.bodies[3]["messages"][0]["content"])
        kinds = [(r["kind"], r["status"]) for r in events[-1]["activity"]]
        self.assertEqual(
            kinds,
            [
                ("plan", "changes_requested"),
                ("plan", "approved"),
                ("code", "complete"),
            ],
        )
        self.assertEqual(events[-1]["mode"], "normal")
        self.assertIn("reviewing", [e["phase"] for e in events])

    async def test_unapproved_plan_stays_in_plan_mode(self):
        model = Model(
            tool_call("submit_plan", {"plan": "1. Compute."}, "p1"),
            "Here is the plan. Shall I go ahead?",
        )
        events = await reply(model, mode="plan")
        self.assertEqual(events[-1]["mode"], "plan")
        self.assertEqual(events[-1]["activity"][0]["status"], "not_approved")
        self.assertIn("not approved", tool_results(model.bodies[1])[0])

    async def test_models_without_tools_get_text_plan_guidance(self):
        bodies = []

        async def handler(request):
            bodies.append(json.loads(request.content))
            return await Model("1. Compute.")(request)

        events = [
            e
            async for e in stream_reply(
                "local:1",
                False,
                [],
                ["Go"],
                web="off",
                transport=httpx.MockTransport(handler),
                mode="plan",
            )
        ]
        self.assertFalse(bodies[0].get("tools"))
        self.assertIn("numbered plan", bodies[0]["messages"][0]["content"])
        self.assertEqual(events[-1]["mode"], "plan")


class FollowUpContextTests(unittest.TestCase):
    def test_completed_turn_activity_reaches_follow_up_context(self):
        activity = [
            {
                "kind": "question",
                "status": "answered",
                "questions": [{"header": "Unit", "question": "Which unit?"}],
                "answers": {"Unit": ["Miles"]},
            },
            {
                "kind": "plan",
                "status": "approved",
                "plan": "1. Convert.",
                "feedback": None,
            },
            {
                "kind": "code",
                "status": "complete",
                "code": "10 / 1.609",
                "output": "6.21",
            },
            {"kind": "code", "status": "failed", "code": "oops(", "output": ""},
        ]
        note = activity_note(activity)
        self.assertIn("Which unit?", note)
        self.assertIn("Miles", note)
        self.assertIn("approved by the user", note)
        self.assertIn("10 / 1.609", note)
        self.assertNotIn("oops(", note)
        turns = [
            {"prompt": ["Convert"], "answer": "6.21 mi", "activity": activity},
            {"prompt": ["Again"], "answer": None, "activity": activity},
        ]
        messages = context_messages(turns)
        self.assertIn("Miles", json.dumps(messages[0].parts[0].content))
        self.assertEqual(messages[-1].parts[0].content, ["Again"])
        self.assertIsNone(activity_note([]))


if __name__ == "__main__":
    unittest.main()
