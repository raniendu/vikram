import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vikram.coding import CodingSession
from vikram.settings import Settings


class Person:
    def __init__(self, approved=False):
        self.approved = approved
        self.requests = []

    async def approve(self, request):
        self.requests.append(request)
        return self.approved


class CodingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.settings = Settings(self.root / "user", self.workspace)
        self.person = Person()
        self.session = CodingSession(
            self.workspace, settings=self.settings, interaction=self.person
        )
        self.addAsyncCleanup(self.session.close)

    async def test_untrusted_and_plan_mutations_are_denied(self):
        result = await self.session.dispatch("write", {"path": "a", "content": "a"})
        self.assertFalse(result.ok)
        self.settings.trust()
        result = await self.session.dispatch(
            "write", {"path": "a", "content": "a"}, mode="plan"
        )
        self.assertFalse(result.ok)
        result = await self.session.dispatch("bash", {"command": "pwd"}, mode="plan")
        self.assertFalse(result.ok)
        self.assertFalse((self.workspace / "a").exists())

    async def test_exact_edit_conflicts_and_call_ids_never_replay(self):
        self.settings.trust()
        target = self.workspace / "a.txt"
        target.write_text("one\none\n")
        result = await self.session.dispatch(
            "edit", {"path": "a.txt", "old_text": "one", "new_text": "two"}
        )
        self.assertFalse(result.ok)
        target.write_text("one\n")
        args = {"path": "a.txt", "old_text": "one", "new_text": "two"}
        first = await self.session.dispatch("edit", args, call_id="same")
        second = await self.session.dispatch("edit", args, call_id="same")
        self.assertTrue(first.ok)
        self.assertEqual(first, second)
        self.assertIn("+two", first.diff)
        self.assertEqual(target.read_text(), "two\n")
        reused = await self.session.dispatch(
            "write", {"path": "a.txt", "content": "changed"}, call_id="same"
        )
        self.assertFalse(reused.ok)
        self.assertEqual(target.read_text(), "two\n")

    async def test_symlink_escape_and_credentials_require_approval(self):
        self.settings.trust()
        private = self.root / "private.txt"
        private.write_text("private")
        (self.workspace / "link").symlink_to(private)
        result = await self.session.dispatch("read", {"path": "link"})
        self.assertFalse(result.ok)
        self.assertEqual(len(self.person.requests), 1)
        (self.workspace / ".env").write_text("SECRET=private")
        result = await self.session.dispatch("read", {"path": ".env"})
        self.assertFalse(result.ok)
        (self.workspace / ".env").unlink()
        (self.workspace / "config").write_text("SECRET=private")
        (self.workspace / ".env").symlink_to("config")
        result = await self.session.dispatch("read", {"path": ".env"})
        self.assertFalse(result.ok)
        self.person.approved = True
        result = await self.session.dispatch("read", {"path": "../private.txt"})
        self.assertTrue(result.ok)
        self.assertIn("private", result.output)

    async def test_inspection_tools_are_bounded_and_do_not_follow_symlinks(self):
        self.settings.trust()
        (self.workspace / "a.py").write_text("needle\nother\nneedle\n")
        (self.workspace / "escape").symlink_to(self.root, target_is_directory=True)
        found = await self.session.dispatch("find", {"pattern": "*.py"})
        self.assertEqual(found.paths, ["a.py"])
        matches = await self.session.dispatch(
            "grep", {"pattern": "needle", "max_results": 1}
        )
        self.assertTrue(matches.truncated)
        self.assertIn("a.py:1:needle", matches.output)

    async def test_shell_policy_user_consent_and_credential_environment(self):
        self.settings.trust()
        result = await self.session.dispatch("bash", {"command": "git push"})
        self.assertFalse(result.ok)
        self.assertTrue(self.person.requests)
        result = await self.session.dispatch("bash", {"command": "pwd"})
        self.assertTrue(result.ok)
        with patch.dict(
            os.environ, {"OPENROUTER_API_KEY": "hidden", "AWS_TOKEN": "hidden"}
        ):
            result = await self.session.dispatch(
                "bash",
                {
                    "command": f'{sys.executable} -c \'import os; print(os.getenv("OPENROUTER_API_KEY")); print(os.getenv("AWS_TOKEN"))\''
                },
                user_initiated=True,
            )
        self.assertTrue(result.ok)
        self.assertNotIn("hidden", result.output)
        self.assertIn("None", result.output)

    async def test_timeout_and_cancellation_kill_process_groups(self):
        self.settings.trust()
        result = await self.session.dispatch(
            "bash", {"command": "sleep 30", "timeout": 0.1}, user_initiated=True
        )
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error)
        pid_file = self.workspace / "pid"
        task = asyncio.create_task(
            self.session.dispatch(
                "bash",
                {"command": "sleep 30 & echo $! > pid; wait", "timeout": 30},
                user_initiated=True,
            )
        )
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(pid_file.exists())
        child = int(pid_file.read_text())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        for _ in range(100):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("The cancelled shell left a child running")

    async def test_parallel_duplicate_calls_share_one_approval_and_effect(self):
        self.settings.trust()
        entered, release = asyncio.Event(), asyncio.Event()

        async def approve(request):
            self.person.requests.append(request)
            entered.set()
            await release.wait()
            return True

        self.person.approve = approve
        args = {"path": "../outside", "content": "once"}
        first = asyncio.create_task(
            self.session.dispatch("write", args, call_id="once")
        )
        await entered.wait()
        duplicate = asyncio.create_task(
            self.session.dispatch("write", args, call_id="once")
        )
        release.set()
        results = await asyncio.gather(first, duplicate)
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.person.requests), 1)
        self.assertEqual((self.root / "outside").read_text(), "once")

    async def test_close_cancels_pending_approval_and_never_replays_it(self):
        self.settings.trust()
        entered = asyncio.Event()

        async def approve(request):
            entered.set()
            await asyncio.Event().wait()

        self.person.approve = approve
        args = {"path": "../outside", "content": "never"}
        task = asyncio.create_task(self.session.dispatch("write", args, call_id="stop"))
        await entered.wait()
        with self.assertRaises(RuntimeError):
            self.session.begin_reply()
        await self.session.close()
        with self.assertRaises(asyncio.CancelledError):
            await task
        result = await self.session.dispatch("write", args, call_id="stop")
        self.assertFalse(result.ok)
        self.assertIn("cancelled", result.error)
        self.assertFalse((self.root / "outside").exists())

    async def test_approval_rechecks_trust_and_does_not_follow_replaced_parent(self):
        self.settings.trust()
        directory = self.root / "outside"
        directory.mkdir()
        victim = self.root / "victim"
        victim.mkdir()

        async def approve(request):
            directory.rmdir()
            directory.symlink_to(victim, target_is_directory=True)
            return True

        self.person.approve = approve
        result = await self.session.dispatch(
            "write", {"path": "../outside/a", "content": "never"}
        )
        self.assertFalse(result.ok)
        self.assertFalse((victim / "a").exists())

        async def revoke(request):
            self.settings.untrust()
            return True

        self.person.approve = revoke
        result = await self.session.dispatch(
            "write", {"path": "../nope", "content": "never"}
        )
        self.assertFalse(result.ok)
        self.assertFalse((self.root / "nope").exists())

    async def test_shared_budget_schema_and_overwrite_guards(self):
        self.settings.trust()
        created = await self.session.dispatch(
            "write", {"path": "nested/new/file.txt", "content": "new"}
        )
        self.assertTrue(created.ok)
        self.assertEqual((self.workspace / "nested/new/file.txt").read_text(), "new")
        (self.workspace / "a").write_text("original")
        result = await self.session.dispatch("write", {"path": "a", "content": "no"})
        self.assertFalse(result.ok)
        self.assertEqual((self.workspace / "a").read_text(), "original")
        result = await self.session.dispatch(
            "write", {"path": "a", "content": "no", "user_initiated": True}
        )
        self.assertFalse(result.ok)
        bounded = CodingSession(self.workspace, settings=self.settings, max_calls=1)
        self.addAsyncCleanup(bounded.close)
        result = await bounded.dispatch("read", {"path": "a"}, call_id="one")
        self.assertTrue(result.ok)
        replay = await bounded.dispatch("read", {"path": "a"}, call_id="one")
        self.assertEqual(result, replay)
        exhausted = await bounded.dispatch("read", {"path": "a"}, call_id="two")
        self.assertFalse(exhausted.ok)
        self.assertIn("budget", exhausted.error)
        bounded.begin_reply()
        renewed = await bounded.dispatch("read", {"path": "a"}, call_id="two")
        self.assertTrue(renewed.ok)

    async def test_shell_globs_cannot_silently_read_credentials(self):
        self.settings.trust()
        (self.workspace / "credentials.json").write_text("secret")
        for command in ("cat *", "rg --hidden .", "python -c 'print(1)'", "rm a"):
            with self.subTest(command=command):
                result = await self.session.dispatch("bash", {"command": command})
                self.assertFalse(result.ok)
                self.assertNotIn("secret", result.output)


if __name__ == "__main__":
    unittest.main()


class SpecialFileTests(unittest.TestCase):
    def test_fifo_reads_and_writes_fail_promptly(self):
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "pipe"
            os.mkfifo(path)
            code = """
import sys
from pathlib import Path
from vikram.coding import _read_text, _write_text
for operation in (lambda: _read_text(Path(sys.argv[1])), lambda: _write_text(Path(sys.argv[1]), 'new', 'old')):
    try:
        operation()
    except ValueError:
        pass
    else:
        raise AssertionError('special file was accepted')
"""
            result = subprocess.run(
                [sys.executable, "-c", code, str(path)], capture_output=True, timeout=5
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
