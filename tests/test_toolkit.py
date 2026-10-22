import asyncio
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from vikram import sandbox as vm
from vikram import toolkit
from vikram.context import activity_note, context_messages
from vikram.conversation import stream_reply
from vikram.interaction import (
    AskUserAnswer,
    AskUserResponse,
    PlanReview,
    ScriptedInteraction,
)
from vikram.models import parse_model, prepare_model
from vikram.sandbox import CodeRun, SandboxUnavailable
from vikram.toolkit import repair_questions

# Captured before any test module patches the host VM out.
REAL_VM, REAL_REASON = vm.VmSandbox, vm.unavailable_reason


class FakeSandbox:
    """Stands in for the VM: returns scripted results and never runs the code."""

    def __init__(self, *results):
        self.results, self.codes, self.closed = list(results), [], False

    async def run(self, code, /):
        self.codes.append(code)
        result = self.results.pop(0) if self.results else CodeRun(0, "ok\n", "")
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return await result()
        return result

    async def close(self):
        self.closed = True


@contextlib.contextmanager
def fake_vm():
    """Give every reply a `FakeSandbox`, whatever this host can run."""
    with (
        patch("vikram.sandbox.unavailable_reason", return_value=None),
        patch("vikram.sandbox.VmSandbox", FakeSandbox),
    ):
        yield


def setUpModule():
    unittest.enterModuleContext(fake_vm())


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


async def reply(model, *, mode="normal", interaction=None, prompt="Go", sandbox=None):
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
            sandbox=sandbox,
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

    async def review_plan(self, request):
        self.plans.append(request.plan)
        return self.reviews.pop(0)


def system_prompt(body):
    return body["messages"][0]["content"]


class SandboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_code_runs_in_the_reply_vm_with_bounded_results(self):
        sandbox = FakeSandbox(
            CodeRun(0, "sum\n45\n", ""), CodeRun(0, "y" * 20000 + "\n", "")
        )
        model = Model(
            tool_call("run_code", {"code": "x = sum(range(10))\nprint('sum')\nx"}),
            tool_call("run_code", {"code": "print('y' * 20000)"}, "c2"),
            "The sum is 45.",
        )
        events = await reply(model, sandbox=lambda: sandbox)
        self.assertIn("run_code", model.offered(0))
        self.assertNotIn("submit_plan", model.offered(0))
        self.assertIn("isolated Linux VM", system_prompt(model.bodies[0]))
        self.assertEqual(sandbox.codes[0], "x = sum(range(10))\nprint('sum')\nx")
        first, second = events[-1]["activity"]
        self.assertEqual(first["status"], "complete")
        self.assertEqual(first["output"], "sum\n45")
        self.assertEqual(tool_results(model.bodies[1])[-1], "sum\n45")
        self.assertEqual(second["status"], "complete")
        self.assertIn("truncated", second["output"])
        self.assertLess(len(second["output"]), toolkit.MAX_OUTPUT + 100)
        returned = tool_results(model.bodies[2])[-1]
        self.assertIn("Print less", returned)
        self.assertLess(len(returned), toolkit.MAX_OUTPUT + 200)
        self.assertEqual(events[-1]["text"], "The sum is 45.")
        self.assertIn("sandbox", [e["phase"] for e in events])
        # The reply deletes its VM when it ends.
        self.assertTrue(sandbox.closed)

    async def test_failures_timeouts_and_a_broken_vm_never_end_the_reply(self):
        sandbox = FakeSandbox(
            CodeRun(
                1, "partial\n", "Traceback (most recent call last):\nValueError: bad"
            ),
            CodeRun(None, "", "Stopped after 15 seconds.", True),
            SandboxUnavailable("The Python sandbox could not start (SmolError)."),
            RuntimeError("zq-internal-7731"),
        )
        model = Model(
            *(tool_call("run_code", {"code": f"step({i})"}, f"c{i}") for i in range(4)),
            "I could not compute it in the sandbox.",
        )
        events = await reply(model, sandbox=lambda: sandbox)
        records = events[-1]["activity"]
        self.assertEqual([r["status"] for r in records], ["failed"] * 4)
        self.assertEqual(records[0]["output"], "partial")
        self.assertIn("ValueError: bad", records[0]["error"])
        self.assertEqual(records[1]["error"], "Stopped after 15 seconds.")
        self.assertIn("could not start", records[2]["error"])
        self.assertEqual(
            records[3]["error"], "The Python sandbox failed (RuntimeError)."
        )
        results = tool_results(model.bodies[-1])
        self.assertIn("ValueError: bad", results[0])
        self.assertIn("Answer without running code", results[2])
        self.assertNotIn("zq-internal-7731", json.dumps(model.bodies))
        self.assertEqual(events[-1]["text"], "I could not compute it in the sandbox.")

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

    async def test_without_a_vm_the_sandbox_is_not_offered(self):
        for mode in ("normal", "plan"):
            model = Model("Hello.")
            await reply(model, mode=mode, sandbox=lambda: None)
            self.assertNotIn("run_code", model.offered(0))
            self.assertIn("ask_user_question", model.offered(0))
            self.assertNotIn("run_code", system_prompt(model.bodies[0]))
        self.assertIn("submit_plan", model.offered(0))

    async def test_stopping_a_reply_deletes_its_vm(self):
        started = asyncio.Event()

        async def hang():
            started.set()
            await asyncio.Event().wait()

        sandbox = FakeSandbox(hang)
        stream = stream_reply(
            "local:1",
            False,
            [],
            ["Go"],
            web="auto",
            transport=httpx.MockTransport(
                Model(tool_call("run_code", {"code": "while True: pass"}))
            ),
            sandbox=lambda: sandbox,
        )
        async for event in stream:
            if started.is_set():
                break
        await stream.aclose()
        self.assertTrue(sandbox.closed)

    async def test_host_vm_only_where_this_host_can_run_one(self):
        self.assertIsInstance(vm.host_sandbox(), FakeSandbox)
        reason = "The Python sandbox needs an Apple silicon Mac."
        with patch("vikram.sandbox.unavailable_reason", return_value=reason):
            self.assertIsNone(vm.host_sandbox())
            prepared = prepare_model(
                [
                    parse_model(
                        {
                            "name": "local:1",
                            "size": 1,
                            "digest": "d",
                            "details": {"format": "gguf"},
                        },
                        {
                            "capabilities": ["completion", "tools"],
                            "details": {"format": "gguf"},
                        },
                    )
                ],
                "local:1",
                None,
            )
        self.assertEqual(prepared.web, "auto")
        self.assertIn(reason, prepared.notice)
        self.assertIn(
            "Questions, plan mode, and web search still work.", prepared.notice
        )


class RunnerTests(unittest.TestCase):
    """The in-VM runner, run here on fixed test snippets (never model code)."""

    def run_snippet(self, code):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "snippet.py").write_text(code)
            return subprocess.run(
                [sys.executable, "-I", "-c", vm.RUNNER, "snippet.py"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
            )

    def test_a_final_expression_prints_like_the_repl(self):
        done = self.run_snippet("x = 6 * 7\nprint('hi')\nx")
        self.assertEqual((done.returncode, done.stdout), (0, "hi\n42\n"))
        self.assertEqual(self.run_snippet("'text'").stdout, "'text'\n")
        self.assertEqual(self.run_snippet("None").stdout, "")
        loop = self.run_snippet("for i in range(2):\n    print(i)")
        self.assertEqual(loop.stdout, "0\n1\n")

    def test_errors_show_the_snippet_traceback_only(self):
        syntax = self.run_snippet("def broken(:")
        self.assertEqual(syntax.returncode, 1)
        self.assertIn("SyntaxError", syntax.stderr)
        self.assertNotIn("Traceback", syntax.stderr)
        failed = self.run_snippet("print('before')\nraise ValueError('bad')")
        self.assertEqual((failed.returncode, failed.stdout), (1, "before\n"))
        self.assertIn('File "<code>", line 2', failed.stderr)
        self.assertIn("ValueError: bad", failed.stderr)
        self.assertNotIn("exec(compile", failed.stderr)

    def test_exit_keeps_python_semantics(self):
        self.assertEqual(
            self.run_snippet("print('done')\nraise SystemExit(0)").returncode, 0
        )
        self.assertEqual(self.run_snippet("import sys\nsys.exit(3)").returncode, 3)


def image_archive(root, *, tamper=False):
    """A tiny image archive in the bundled layout, with its manifest digest."""
    config, layer = b'{"architecture":"arm64"}', b"layer bytes"
    digest = lambda data: f"sha256:{hashlib.sha256(data).hexdigest()}"
    registry = json.dumps(
        {
            "config": {"digest": digest(config)},
            "layers": [{"digest": digest(layer)}],
        }
    ).encode()
    files = {
        "manifest.json": json.dumps(
            [
                {
                    "Config": digest(config).replace(":", "-"),
                    "Layers": [digest(layer).replace(":", "-")],
                }
            ]
        ).encode(),
        vm.REGISTRY_MANIFEST: registry,
        digest(config).replace(":", "-"): config,
        digest(layer).replace(":", "-"): b"other bytes" if tamper else layer,
    }
    path = Path(root) / ("tampered.tar" if tamper else "image.tar")
    with tarfile.open(path, "w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path, digest(registry)


class ImageAndCleanupTests(unittest.IsolatedAsyncioTestCase):
    def test_the_archive_must_match_the_pinned_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            good, pinned = image_archive(root)
            tampered, _ = image_archive(root, tamper=True)
            with patch.object(vm, "IMAGE_MANIFEST", pinned):
                self.assertTrue(vm.verify_archive(good))
                self.assertFalse(vm.verify_archive(tampered))
                self.assertFalse(vm.verify_archive(Path(root) / "missing.tar"))
            self.assertFalse(vm.verify_archive(good))

    async def test_vms_left_by_a_crashed_process_are_deleted(self):
        destroyed = []

        class Smol:
            async def destroy(self, ref):
                if ref.id == "pydantic-ai-stuck":
                    raise RuntimeError("smolvm is busy")
                destroyed.append(ref.id)

        dead = subprocess.run(
            [sys.executable, "-c", "import os; print(os.getpid())"],
            capture_output=True,
            text=True,
        )
        with tempfile.TemporaryDirectory() as root:
            markers = Path(root)
            for name, pid in (
                ("pydantic-ai-old", int(dead.stdout)),
                ("pydantic-ai-stuck", int(dead.stdout)),
                ("pydantic-ai-live", os.getpid()),
            ):
                (markers / f"{name}.json").write_text(json.dumps({"pid": pid}))
            (markers / "pydantic-ai-corrupt.json").write_text("{")
            with (
                patch.object(vm, "_markers", return_value=markers),
                patch.object(vm, "_swept", False),
                patch("pydantic_ai_harness.smol_sandbox.SmolSandbox", Smol),
            ):
                await vm._sweep()
                await vm._sweep()  # Once per process.
            self.assertEqual(destroyed, ["pydantic-ai-old"])
            # A failed deletion is retried by a later process.
            self.assertEqual(
                sorted(p.name for p in markers.iterdir()),
                ["pydantic-ai-live.json", "pydantic-ai-stuck.json"],
            )

    async def test_vms_get_small_disks_and_keep_the_disk_reserve(self):
        import smol

        created = []

        class Denied(Exception):
            code = "HYPERVISOR_DENIED"

        def create(config, target):
            created.append(config)
            raise Denied("/private/path detail")

        destroyed = []

        class Smol:
            async def destroy(self, ref):
                destroyed.append(ref.id)

        plenty = SimpleNamespace(free=10**13)
        tight = SimpleNamespace(free=21 * 1024**3)
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(vm, "_markers", return_value=Path(root)),
            patch.object(vm, "_swept", True),
            patch.object(smol.Machine, "create", side_effect=create),
            patch("pydantic_ai_harness.smol_sandbox.SmolSandbox", Smol),
        ):
            sandbox = REAL_VM()
            with patch.object(vm.shutil, "disk_usage", return_value=tight):
                with self.assertRaisesRegex(SandboxUnavailable, "20 GiB reserve"):
                    await sandbox.run("1")
            self.assertEqual(created, [])
            with patch.object(vm.shutil, "disk_usage", return_value=plenty):
                with self.assertRaises(SandboxUnavailable) as failed:
                    await sandbox.run("1")
            self.assertEqual(
                str(failed.exception),
                "The Python sandbox could not start (HYPERVISOR_DENIED).",
            )
            [config] = created
            self.assertEqual(
                (config.resources.storage_gb, config.resources.overlay_gb), (1, 1)
            )
            self.assertFalse(config.resources.network)
            self.assertEqual(
                (config.resources.cpus, config.resources.memory_mb), (1, 512)
            )
            # A broken VM fails fast, and closing still deletes it and its marker.
            with self.assertRaises(SandboxUnavailable):
                await sandbox.run("1")
            self.assertEqual(len(created), 1)
            self.assertEqual(len(list(Path(root).iterdir())), 1)
            await sandbox.close()
            self.assertEqual(len(destroyed), 1)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_a_frozen_app_keeps_one_guest_filesystem_cache(self):
        with tempfile.TemporaryDirectory() as root:
            rootfs = Path(root, "smol", "agent-rootfs.tar.zst")
            rootfs.parent.mkdir()
            stamps = []
            for content, launched in ((b"rootfs", 1), (b"rootfs", 2), (b"other", 3)):
                rootfs.write_bytes(content)
                os.utime(rootfs, (launched, launched))  # Unpacked at each launch.
                with (
                    patch.object(sys, "frozen", True, create=True),
                    patch.object(sys, "_MEIPASS", root, create=True),
                ):
                    vm._pin_guest_rootfs()
                stamps.append(rootfs.stat().st_mtime)
            self.assertEqual(stamps[0], stamps[1])
            self.assertNotEqual(stamps[0], stamps[2])
        vm._pin_guest_rootfs()  # Outside a frozen app, smolvm's own files stay as they are.

    def test_packaging_check_boots_a_vm_and_reports_failure(self):
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch(
                "vikram.sandbox.VmSandbox", lambda: FakeSandbox(CodeRun(0, "45\n", ""))
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(vm.check_sandbox(), 0)
        self.assertIn("ready", output.getvalue())
        broken = SandboxUnavailable("The Python sandbox could not start (SmolError).")
        with (
            patch("vikram.sandbox.VmSandbox", lambda: FakeSandbox(broken)),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(vm.check_sandbox(), 1)
        with (
            patch("vikram.sandbox.unavailable_reason", return_value="No VM here."),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(vm.check_sandbox(), 1)
        self.assertIn("could not start", errors.getvalue())
        self.assertIn("No VM here.", errors.getvalue())


@unittest.skipIf(REAL_REASON(), f"This host cannot run the sandbox VM: {REAL_REASON()}")
class MicroVmTests(unittest.IsolatedAsyncioTestCase):
    """Boots a real microVM from the pinned image (Apple silicon only)."""

    async def test_code_runs_isolated_in_a_microvm(self):
        sandbox = REAL_VM()
        os.environ["VIKRAM_HOST_ONLY"] = "secret"
        try:
            run = await sandbox.run(
                "import platform, sys\n"
                "print(platform.system(), sys.version_info[:2])\n"
                "sum(range(10))"
            )
            self.assertEqual((run.exit_code, run.output), (0, "Linux (3, 13)\n45\n"))
            checks = await sandbox.run(
                "import os, socket\n"
                f"print(os.path.exists({str(Path.home())!r}), os.getcwd())\n"
                "print('VIKRAM_HOST_ONLY' in os.environ)\n"
                "try:\n"
                "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
                "    print('connected')\n"
                "except OSError:\n"
                "    print('offline')\n"
                "import shutil\n"
                "print(shutil.disk_usage('/workspace').total < 1.2e9)\n"
                "open('kept.txt', 'w').write('7')"
            )
            self.assertEqual(
                checks.output, "False /workspace\nFalse\noffline\nTrue\n1\n"
            )
            kept = await sandbox.run("int(open('/workspace/kept.txt').read()) * 6")
            self.assertEqual(kept.output, "42\n")
            failed = await sandbox.run("1 / 0")
            self.assertEqual(failed.exit_code, 1)
            self.assertIn("ZeroDivisionError", failed.error)
            with patch.object(vm, "RUN_TIMEOUT", 2):
                stuck = await sandbox.run("while True: pass")
            self.assertTrue(stuck.timed_out)
            ref = sandbox._backend.ref
            self.assertTrue((vm._markers() / f"{ref.id}.json").is_file())
        finally:
            del os.environ["VIKRAM_HOST_ONLY"]
            await sandbox.close()
        self.assertFalse((vm._markers() / f"{ref.id}.json").exists())


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
        same = {"header": "A very long repeated header text", "options": ["x", "y"]}
        headers = [
            q["header"]
            for q in repair_questions({"questions": [same] * 4})["questions"]
        ]
        self.assertEqual(len({h.casefold() for h in headers}), 4)
        self.assertTrue(all(len(h) <= 25 for h in headers))
        self.assertTrue(question["multi_select"])
        self.assertEqual([o["label"] for o in question["options"]], ["Red", "Blue"])


class PlanModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_mode_refuses_the_sandbox_until_the_person_approves(self):
        person = Person(
            reviews=[
                PlanReview(decision="revise", feedback="Also double-check it."),
                PlanReview(decision="approve"),
            ]
        )
        model = Model(
            # Small models call the sandbox in plan mode: refuse without running it,
            # as often as it happens, without spending retries or ending the reply.
            tool_call("run_code", {"code": "print('should not run')"}),
            tool_call("run_code", {"code": "print('should not run')"}, "c0"),
            tool_call("run_code", {"code": "print('should not run')"}, "c1"),
            tool_call("submit_plan", {"plan": "1. Compute."}, "p1"),
            tool_call("submit_plan", {"plan": "1. Compute.\n2. Verify."}, "p2"),
            tool_call("run_code", {"code": "6 * 7"}, "c2"),
            "The answer is 42.",
        )
        sandbox = FakeSandbox(CodeRun(0, "42\n", ""))
        events = await reply(
            model, mode="plan", interaction=person, sandbox=lambda: sandbox
        )
        # Refused calls never reach the VM.
        self.assertEqual(sandbox.codes, ["6 * 7"])
        self.assertIn("submit_plan", model.offered(0))
        for index in (1, 2, 3):
            self.assertIn("code cannot run yet", tool_results(model.bodies[index])[-1])
        self.assertIn("Also double-check it.", tool_results(model.bodies[4])[-1])
        self.assertEqual(person.plans, ["1. Compute.", "1. Compute.\n2. Verify."])
        self.assertIn("run_code", model.offered(5))
        self.assertNotIn("submit_plan", model.offered(5))
        self.assertIn("Plan mode is on", model.bodies[0]["messages"][0]["content"])
        self.assertNotIn("Plan mode is on", model.bodies[5]["messages"][0]["content"])
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
