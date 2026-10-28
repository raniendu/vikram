import asyncio
import json
import os
import plistlib
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
from test_toolkit import Model, fake_vm, system_prompt, tool_call, tool_results

from vikram import schedules
from vikram.context import activity_note
from vikram.conversation import stream_reply
from vikram.interaction import Unattended
from vikram.schedules import (
    Schedule,
    ScheduleStore,
    TaskBook,
    describe,
    next_run,
    repair_schedule,
    schedule_from,
)

LA = ZoneInfo("America/Los_Angeles")


def setUpModule():
    unittest.enterModuleContext(fake_vm())
    # Every test computes times in one fixed zone, whatever this Mac uses.
    unittest.enterModuleContext(patch("vikram.schedules.local_zone", return_value=LA))


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=LA)


class Clock:
    def __init__(self, moment: datetime):
        self.now = moment.timestamp()

    def __call__(self) -> float:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta).total_seconds()


class ScheduleTests(unittest.TestCase):
    def test_next_runs_follow_local_time(self):
        weekdays = Schedule(repeat="weekly", days=("fri", "mon"), time="08:00")
        # Friday evening → Monday morning; the days are kept in week order.
        self.assertEqual(weekdays.days, ("mon", "fri"))
        self.assertEqual(
            next_run(weekdays, at("2026-10-30T09:00")), at("2026-11-02T08:00")
        )
        daily = Schedule(repeat="daily", time="08:00")
        self.assertEqual(
            next_run(daily, at("2026-10-27T07:59")), at("2026-10-27T08:00")
        )
        # Strictly after: a run at 08:00 schedules the next day's.
        self.assertEqual(
            next_run(daily, at("2026-10-27T08:00")), at("2026-10-28T08:00")
        )
        # Daylight saving time ends on 1 Nov 2026: 08:00 stays 08:00 local.
        after = next_run(daily, at("2026-10-31T09:00"))
        self.assertEqual((after.hour, after.utcoffset()), (8, timedelta(hours=-8)))
        monthly = Schedule(repeat="monthly", day_of_month=31, time="09:30")
        self.assertEqual(
            next_run(monthly, at("2026-11-01T00:00")), at("2026-11-30T09:30")
        )
        once = Schedule(repeat="once", date="2026-10-28", time="15:00")
        self.assertEqual(next_run(once, at("2026-10-27T10:00")), at("2026-10-28T15:00"))
        self.assertIsNone(next_run(once, at("2026-10-28T15:00")))
        every = Schedule(repeat="interval", every_minutes=90)
        anchor = at("2026-10-27T10:00")
        self.assertEqual(next_run(every, at("2026-10-27T09:00"), anchor), anchor)
        self.assertEqual(
            next_run(every, at("2026-10-27T13:01"), anchor), at("2026-10-27T14:30")
        )

    def test_descriptions(self):
        self.assertEqual(
            describe(
                Schedule(repeat="weekly", days=list(schedules.WEEKDAYS), time="08:00")
            ),
            "Weekdays at 08:00",
        )
        self.assertEqual(
            describe(Schedule(repeat="weekly", days=["mon", "wed"], time="18:15")),
            "Every Mon, Wed at 18:15",
        )
        self.assertEqual(
            describe(Schedule(repeat="once", date="2026-10-28", time="15:00")),
            "Once on Wed 28 Oct 2026 at 15:00",
        )
        self.assertEqual(
            describe(Schedule(repeat="interval", every_minutes=120)), "Every 2 hours"
        )
        self.assertEqual(
            describe(Schedule(repeat="monthly", day_of_month=1, time="09:00")),
            "Monthly on day 1 at 09:00",
        )

    def test_invalid_schedules_say_what_is_missing(self):
        with self.assertRaisesRegex(ValueError, "needs date"):
            schedule_from({"repeat": "once", "time": "08:00"})
        with self.assertRaisesRegex(ValueError, "every_minutes"):
            schedule_from({"repeat": "interval", "every_minutes": 5})
        with self.assertRaises(ValueError):
            schedule_from({"repeat": "daily", "time": "25:00"})

    def test_repair_of_typical_small_model_arguments(self):
        now = at("2026-10-27T16:00")
        cases = [
            (
                {"repeat": "weekdays", "time": "8am"},
                {"repeat": "weekly", "days": list(schedules.WEEKDAYS), "time": "08:00"},
            ),
            (
                {"repeat": "once", "time": "2026-10-28T15:00:00-07:00"},
                {"repeat": "once", "date": "2026-10-28", "time": "15:00"},
            ),
            (
                {"repeat": "one-time", "date": "tomorrow", "time": "3 pm"},
                {"repeat": "once", "date": "2026-10-28", "time": "15:00"},
            ),
            # A time alone: today if still ahead, else tomorrow.
            (
                {"repeat": "once", "time": "9:00"},
                {"repeat": "once", "date": "2026-10-28", "time": "09:00"},
            ),
            (
                {"schedule": {"frequency": "Every Day", "time": "07:30"}, "days": None},
                {"repeat": "daily", "time": "07:30"},
            ),
            (
                {"repeat": "daily", "days": "Monday and Wednesday", "time": "20.30"},
                {"repeat": "weekly", "days": ["mon", "wed"], "time": "20:30"},
            ),
            ({"repeat": "hourly"}, {"repeat": "interval", "every_minutes": 60}),
            (
                {"repeat": "interval", "every_hours": 2, "time": "08:00"},
                {"repeat": "interval", "every_minutes": 120},
            ),
            (
                {"repeat": "monthly", "day": "1st", "time": "9am"},
                {"repeat": "monthly", "day_of_month": 1, "time": "09:00"},
            ),
        ]
        for given, expected in cases:
            with self.subTest(given=given):
                repaired = repair_schedule(given, now=now)
                self.assertEqual(
                    {k: v for k, v in repaired.items() if v is not None}, expected
                )
                schedule_from(repaired)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.clock = Clock(at("2026-10-27T07:00"))
        self.store = ScheduleStore(self.root, clock=self.clock)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def due(self) -> str | None:
        path = self.root / "scheduler-next-due"
        return path.read_text().strip() if path.exists() else None

    def create(self, **schedule) -> dict:
        return self.store.create(
            name="Morning news",
            prompt="Summarize the news.",
            schedule=Schedule(**(schedule or {"repeat": "daily", "time": "08:00"})),
            model="local:1",
        )

    def test_the_due_file_follows_the_soonest_enabled_task(self):
        self.assertIsNone(self.due())
        task = self.create()
        self.assertEqual(task["state"], "scheduled")
        self.assertEqual(task["description"], "Every day at 08:00")
        self.assertEqual(self.due(), str(int(at("2026-10-27T08:00").timestamp())))
        self.assertEqual(
            oct(os.stat(self.root / "schedules.sqlite3").st_mode)[-3:], "600"
        )
        self.assertEqual(
            oct(os.stat(self.root / "scheduler-next-due").st_mode)[-3:], "600"
        )
        paused = self.store.update(task["id"], enabled=False)
        self.assertEqual(paused["state"], "paused")
        self.assertIsNone(self.due())
        self.assertFalse(self.store.active())
        self.store.request_run(task["id"])
        self.assertEqual(self.due(), "0")
        self.store.update(task["id"], enabled=True)
        self.store.delete(task["id"])
        self.assertIsNone(self.due())

    def test_past_times_and_limits_are_refused(self):
        with self.assertRaisesRegex(ValueError, "already passed"):
            self.create(repeat="once", date="2026-10-26", time="09:00")
        with self.assertRaisesRegex(ValueError, "name and a prompt"):
            self.store.create(
                name=" \x07 ",
                prompt="x",
                schedule=Schedule(repeat="daily", time="08:00"),
                model=None,
            )

    def test_claims_run_due_tasks_once_and_record_long_missed_ones(self):
        task = self.create()
        self.assertIsNone(self.store.claim())
        self.clock.advance(hours=1, minutes=5)
        claimed, run = self.store.claim()
        self.assertEqual(
            (claimed["id"], run["status"], run["trigger"]),
            (task["id"], "running", "catch_up"),
        )
        self.assertEqual(run["prompt"], "Summarize the news.")
        self.assertIsNone(self.store.claim())
        self.store.finish(run["id"], "complete")
        # Asleep for three days: the missed runs collapse into one record.
        self.clock.advance(days=3)
        self.assertIsNone(self.store.claim())
        runs = self.store.task(task["id"])["runs"]
        self.assertEqual([r["status"] for r in runs], ["missed", "complete"])
        self.assertGreater(self.store.task(task["id"])["next_run"], self.clock.now)
        # Late by less than twelve hours: it catches up, once.
        self.clock.now = self.store.task(task["id"])["next_run"] + 3 * 3600
        claimed, run = self.store.claim()
        self.assertEqual(run["trigger"], "catch_up")
        self.assertIsNone(self.store.claim())

    def test_one_time_tasks_are_done_after_running_and_run_now_still_works(self):
        task = self.create(repeat="once", date="2026-10-27", time="09:00")
        self.clock.advance(hours=2)
        _, run = self.store.claim()
        self.store.finish(run["id"], "complete")
        done = self.store.task(task["id"])
        self.assertEqual(done["state"], "done")
        self.assertIsNone(self.due())
        self.store.request_run(task["id"])
        _, again = self.store.claim()
        self.assertEqual((again["trigger"], again["scheduled_for"]), ("manual", None))

    def test_stale_runs_and_history_limit(self):
        task = self.create(repeat="interval", every_minutes=15)
        with patch.object(schedules, "KEEP_RUNS", 3):
            for _ in range(5):
                self.clock.advance(minutes=15)
                _, run = self.store.claim()
                self.store.checkpoint(
                    run["id"], content="Partial", activity=[{"kind": "code"}]
                )
                self.store.finish(run["id"], "complete")
            self.store.request_run(task["id"])
            _, run = self.store.claim()
        self.store.interrupt_stale()
        runs = self.store.task(task["id"])["runs"]
        self.assertEqual(len(runs), 3)
        self.assertEqual(runs[0]["status"], "interrupted")
        self.assertEqual(self.store.run(runs[1]["id"])["activity"], [{"kind": "code"}])
        self.assertEqual(runs[1]["preview"], "Partial")

    def test_find_by_id_prefix_or_name_and_changes_seen_by_other_processes(self):
        task = self.create()
        other = ScheduleStore(self.root, clock=self.clock)
        try:
            self.assertEqual(other.find(task["id"][:8])["id"], task["id"])
            self.assertEqual(other.find("“morning NEWS”")["id"], task["id"])
            self.assertEqual(other.find("news")["id"], task["id"])
            with self.assertRaisesRegex(ValueError, "No scheduled task"):
                other.find("weather")
            version = other.data_version()
            self.store.update(task["id"], name="Evening news")
            self.assertNotEqual(other.data_version(), version)
        finally:
            other.close()

    def test_an_edit_reschedules_from_now(self):
        task = self.create()
        changed = self.store.update(
            task["id"], schedule=Schedule(repeat="interval", every_minutes=30)
        )
        self.assertEqual(changed["next_run"], self.clock.now + 30 * 60)
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            self.store.update(task["id"], origin="x")


class FakeCatalog:
    def __init__(self, models=None):
        self.models = models

    async def refresh(self):
        if self.models is None:
            raise ValueError(
                "Cannot connect to local Ollama. Open Ollama, then choose Refresh."
            )
        return self.models


MODEL = {
    "name": "local:1",
    "available": True,
    "vision": False,
    "tools": True,
    "thinking": [],
    "thinking_default": None,
}


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock(at("2026-10-27T07:00"))
        self.store = ScheduleStore(Path(self.directory.name), clock=self.clock)
        self.alerts = []

    async def asyncTearDown(self):
        self.store.close()
        self.directory.cleanup()

    def task(self, **fields):
        return self.store.create(
            name=fields.get("name", "Call mom"),
            prompt="Remind me to call mom.",
            schedule=Schedule(repeat="once", date="2026-10-27", time="08:00"),
            model=fields.get("model", "local:1"),
        )

    async def test_a_due_task_runs_unattended_without_a_browser(self):
        self.task()
        seen = {}

        async def streamer(model, thinking, history, prompt, **options):
            seen.update(options, model=model, prompt=prompt, history=history)
            yield {"text": "Reminder", "phase": "answering", "activity": []}
            yield {
                "text": "Reminder: call mom.",
                "phase": "answering",
                "actual_model": "local:1",
            }

        self.clock.advance(hours=1, seconds=30)
        count = await schedules.run_due(
            self.store,
            streamer=streamer,
            catalog=FakeCatalog([MODEL]),
            alert=lambda *args: self.alerts.append(args),
        )
        self.assertEqual(count, 1)
        self.assertIsInstance(seen["interaction"], Unattended)
        self.assertIsNone(seen["browser"])
        self.assertNotIn("tasks", seen)
        self.assertEqual(
            (seen["prompt"], seen["history"]), (["Remind me to call mom."], [])
        )
        self.assertIn("“Call mom” (once on", seen["scheduled"])
        [task] = self.store.tasks()
        run = self.store.run(task["last_run"]["id"])
        self.assertEqual(
            (run["status"], run["content"]), ("complete", "Reminder: call mom.")
        )
        self.assertEqual(run["actual_model"], "local:1")
        self.assertEqual(task["state"], "done")
        # The notification names the task only, never the answer.
        self.assertEqual(self.alerts, [("Call mom", "complete")])

    async def test_failures_are_recorded_with_safe_messages(self):
        self.task(name="No model", model=None)
        self.task(name="Offline")
        self.clock.advance(hours=1, seconds=30)
        await schedules.run_due(
            self.store,
            catalog=FakeCatalog(None),
            alert=lambda *a: self.alerts.append(a),
        )
        runs = {
            t["name"]: self.store.run(t["last_run"]["id"]) for t in self.store.tasks()
        }
        self.assertEqual(
            runs["No model"]["error"], "Choose a model for this scheduled task."
        )
        self.assertIn("Ollama", runs["Offline"]["error"])
        self.assertEqual({a[1] for a in self.alerts}, {"failed"})

    async def test_real_reply_loop_gets_the_scheduled_run_instructions(self):
        self.task()
        model = Model("Reminder: call mom.")
        self.clock.advance(hours=1, seconds=30)
        await schedules.run_due(
            self.store,
            streamer=lambda *a, **o: stream_reply(
                *a, transport=httpx.MockTransport(model), **o
            ),
            catalog=FakeCatalog([MODEL]),
            alert=lambda *a: None,
        )
        prompt = system_prompt(model.bodies[0])
        self.assertIn("scheduled run of the task “Call mom”", prompt)
        self.assertNotIn("ask_user_question", prompt)
        offered = model.offered(0)
        self.assertNotIn("ask_user_question", offered)
        self.assertFalse(
            [
                name
                for name in offered
                if "schedule" in name or name.startswith("browser_")
            ]
        )

    async def test_a_stop_ends_the_run_as_stopped(self):
        self.task()

        async def slow(*args, **options):
            yield {"text": "Partial", "phase": "answering"}
            await asyncio.sleep(30)

        self.clock.advance(hours=1, seconds=30)
        work = asyncio.create_task(
            schedules.run_due(
                self.store,
                streamer=slow,
                catalog=FakeCatalog([MODEL]),
                alert=lambda *a: self.alerts.append(a),
            )
        )
        await asyncio.sleep(0.05)
        work.cancel()
        await work
        [task] = self.store.tasks()
        run = self.store.run(task["last_run"]["id"])
        self.assertEqual((run["status"], run["content"]), ("stopped", "Partial"))
        self.assertEqual(self.alerts, [])


class Launchctl:
    def __init__(self, loaded=False):
        self.loaded, self.calls = loaded, []

    def __call__(self, *args):
        self.calls.append(args[0])
        if args[0] == "print":
            return SimpleNamespace(returncode=0 if self.loaded else 113)
        self.loaded = args[0] == "bootstrap"
        return SimpleNamespace(returncode=0)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name) / "home with spaces"
        self.home.mkdir()
        self.root = self.home / "data"
        self.store = ScheduleStore(self.root)
        self.agent = schedules.agent_for(self.home, self.root)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_the_agent_follows_whether_any_task_is_enabled(self):
        run = Launchctl()
        self.assertEqual(schedules.ensure_agent(self.store, None, run), "unavailable")
        self.assertEqual(schedules.ensure_agent(self.store, self.agent, run), "off")
        self.assertFalse(self.agent.plist.exists())
        task = self.store.create(
            name="News",
            prompt="News.",
            schedule=Schedule(repeat="daily", time="08:00"),
            model="m",
        )
        self.assertEqual(schedules.ensure_agent(self.store, self.agent, run), "on")
        definition = plistlib.loads(self.agent.plist.read_bytes())
        self.assertEqual(definition["Label"], schedules.LABEL)
        self.assertEqual(definition["StartInterval"], 60)
        self.assertEqual(definition["StandardOutPath"], "/dev/null")
        arguments = definition["ProgramArguments"]
        self.assertEqual(arguments[:2], ["/bin/sh", "-c"])
        # Through `current`, so updates need no change to the agent.
        self.assertTrue(
            arguments[5].endswith(
                "dev.raniendu.vikram.install/current/Vikram.app/Contents/MacOS/vikram-sidecar"
            )
        )
        self.assertEqual(run.calls, ["print", "print", "bootstrap"])
        # Unchanged and loaded: nothing to do.
        self.assertEqual(schedules.ensure_agent(self.store, self.agent, run), "on")
        self.assertEqual(run.calls[-1], "print")
        self.store.update(task["id"], enabled=False)
        self.assertEqual(schedules.ensure_agent(self.store, self.agent, run), "off")
        self.assertEqual(run.calls[-1], "bootout")
        self.assertFalse(self.agent.plist.exists())

    def test_an_update_refreshes_an_existing_agent_only(self):
        run = Launchctl()
        schedules.refresh_installed_agent(self.home, run)
        self.assertFalse(self.agent.plist.exists())
        self.agent.plist.parent.mkdir(parents=True)
        self.agent.plist.write_bytes(b"old")
        schedules.refresh_installed_agent(self.home, run)
        definition = plistlib.loads(self.agent.plist.read_bytes())
        self.assertEqual(definition["Label"], schedules.LABEL)
        # A disposable home never touches this account's launchd.
        self.assertEqual(run.calls, [])

    def test_development_runtimes_install_no_agent(self):
        self.assertIsNone(schedules.installed_agent())

    def shell(self, due: str | None, install: bool = True) -> tuple[Path, Path]:
        """Run the agent's command as launchd would, with stand-ins."""
        bin_dir = self.home / "bin"
        bin_dir.mkdir(exist_ok=True)
        marker = self.home / "ran"
        runtime = bin_dir / "runtime"
        runtime.write_text(f'#!/bin/sh\necho "$@" > "{marker}"\n')
        runtime.chmod(0o755)
        launchctl = bin_dir / "launchctl"
        launchctl.write_text(f'#!/bin/sh\necho "$@" > "{self.home / "launchctl"}"\n')
        launchctl.chmod(0o755)
        install_root = self.home / "install"
        if install:
            install_root.mkdir(exist_ok=True)
        due_file = self.home / "due"
        due_file.unlink(missing_ok=True)
        if due is not None:
            due_file.write_text(due + "\n")
        plist = self.home / "agent.plist"
        plist.write_text("x")
        subprocess.run(
            [
                "/bin/sh",
                "-c",
                schedules.SCRIPT,
                "vikram-scheduler",
                str(due_file),
                str(runtime),
                str(install_root),
                str(plist),
                "test.label",
            ],
            check=True,
            timeout=10,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        )
        return marker, plist

    def test_the_shell_starts_the_runtime_only_when_due(self):
        import time

        for due, runs in (
            (None, False),
            ("junk", False),
            (str(int(time.time()) + 600), False),
            ("0", True),
            (str(int(time.time()) - 5), True),
        ):
            with self.subTest(due=due):
                marker, _ = self.shell(due)
                self.assertEqual(marker.exists(), runs)
                if runs:
                    self.assertEqual(marker.read_text().strip(), "--run-schedules")
                marker.unlink(missing_ok=True)

    def test_the_agent_removes_itself_once_vikram_is_gone(self):
        import shutil

        shutil.rmtree(self.home / "install", ignore_errors=True)
        marker, plist = self.shell("0", install=False)
        self.assertFalse(marker.exists())
        self.assertFalse(plist.exists())
        self.assertIn("bootout", (self.home / "launchctl").read_text())


class Approver:
    def __init__(self, *answers):
        self.answers, self.requests = list(answers), []

    async def ask(self, request):
        raise AssertionError("No questions expected.")

    async def review_plan(self, request):
        raise AssertionError("No plans expected.")

    async def approve(self, request):
        self.requests.append(request)
        return self.answers.pop(0)


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ScheduleStore(Path(self.directory.name))
        self.changes = 0

        def changed():
            self.changes += 1

        self.book = TaskBook(
            store=self.store, model="local:1", origin="chat", changed=changed
        )

    async def asyncTearDown(self):
        self.store.close()
        self.directory.cleanup()

    async def reply(self, model, *, interaction=None, mode="normal", tasks=True):
        return [
            event
            async for event in stream_reply(
                "local:1",
                False,
                [],
                ["Go"],
                web="auto",
                session_id="a" * 32,
                transport=httpx.MockTransport(model),
                mode=mode,
                interaction=interaction,
                tasks=self.book if tasks else None,
            )
        ]

    async def test_an_approved_task_is_saved_from_repaired_arguments(self):
        person = Approver(True)
        model = Model(
            tool_call(
                "schedule_task",
                {
                    "name": "Morning tech news",
                    "prompt": "Summarize today's top technology news.",
                    "repeat": "every weekday",
                    "time": "8am",
                    "timezone": "PST",
                },
            ),
            "Scheduled.",
        )
        events = await self.reply(model, interaction=person)
        [task] = self.store.tasks()
        self.assertEqual(task["description"], "Weekdays at 08:00")
        self.assertEqual((task["model"], task["origin"]), ("local:1", "chat"))
        [request] = person.requests
        self.assertEqual(request.kind, "schedule")
        self.assertEqual(
            request.summary, "Schedule “Morning tech news” · Weekdays at 08:00"
        )
        self.assertEqual(request.reason, "Summarize today's top technology news.")
        record = events[-1]["activity"][0]
        self.assertEqual(
            {k: record[k] for k in ("kind", "action", "status", "task_id")},
            {
                "kind": "schedule",
                "action": "create",
                "status": "complete",
                "task_id": task["id"],
            },
        )
        self.assertIn("approving", [e["phase"] for e in events])
        self.assertIn("Saved the scheduled task", tool_results(model.bodies[1])[0])
        self.assertEqual(self.changes, 1)
        self.assertIn("schedule_task", system_prompt(model.bodies[0]))
        self.assertIn(
            "You scheduled the scheduled task “Morning tech news” (Weekdays at 08:00",
            activity_note(events[-1]["activity"]),
        )

    async def test_declined_or_unattended_tasks_are_not_saved(self):
        call = tool_call(
            "schedule_task",
            {"name": "Ping", "prompt": "Say hi.", "repeat": "daily", "time": "09:00"},
        )
        for person in (Approver(False), None):
            with self.subTest(person=person):
                model = Model(call, "Not scheduled.")
                events = await self.reply(model, interaction=person)
                self.assertEqual(self.store.tasks(), [])
                self.assertEqual(events[-1]["activity"][0]["status"], "denied")
                self.assertIn("did not approve", tool_results(model.bodies[1])[0])
        self.assertEqual(self.changes, 0)

    async def test_list_pause_and_delete_need_no_approval(self):
        task = self.store.create(
            name="News",
            prompt="News.",
            schedule=Schedule(repeat="daily", time="08:00"),
            model="m",
        )
        model = Model(
            tool_call("list_scheduled_tasks", {}, "c1"),
            tool_call(
                "update_scheduled_task",
                {"task_id": task["id"][:8], "paused": "true"},
                "c2",
            ),
            tool_call("delete_scheduled_task", {"task": "news"}, "c3"),
            "Done.",
        )
        events = await self.reply(model, interaction=Approver())
        self.assertIn(
            f"“News” (id {task['id'][:8]}) · Every day at 08:00",
            tool_results(model.bodies[1])[0],
        )
        self.assertIn("Paused “News”.", tool_results(model.bodies[2])[-1])
        self.assertEqual(self.store.tasks(), [])
        self.assertEqual(
            [r["action"] for r in events[-1]["activity"]], ["pause", "delete"]
        )
        self.assertEqual(self.changes, 2)

    async def test_an_edit_merges_into_the_saved_schedule_after_approval(self):
        task = self.store.create(
            name="News",
            prompt="News.",
            schedule=Schedule(repeat="weekly", days=["mon"], time="08:00"),
            model="m",
        )
        person = Approver(True)
        model = Model(
            tool_call("update_scheduled_task", {"task": "News", "time": "9:30"}),
            "Changed.",
        )
        await self.reply(model, interaction=person)
        self.assertEqual(
            self.store.task(task["id"])["description"], "Every Mon at 09:30"
        )
        self.assertEqual(
            person.requests[0].summary, "Change “News” · Every Mon at 09:30"
        )

    async def test_bad_schedules_and_plan_mode_are_finished_refusals(self):
        model = Model(
            tool_call(
                "schedule_task",
                {
                    "name": "X",
                    "prompt": "Y",
                    "repeat": "once",
                    "date": "2020-01-01",
                    "time": "09:00",
                },
            ),
            "Sorry.",
        )
        events = await self.reply(model, interaction=Approver(True))
        self.assertIn("already passed", tool_results(model.bodies[1])[0])
        self.assertEqual(events[-1]["activity"][0]["status"], "failed")
        model = Model(
            tool_call(
                "schedule_task",
                {"name": "X", "prompt": "Y", "repeat": "daily", "time": "09:00"},
            ),
            "Plan.",
        )
        await self.reply(model, interaction=Approver(), mode="plan")
        self.assertIn("Plan mode is on", tool_results(model.bodies[1])[0])
        self.assertEqual(self.store.tasks(), [])

    async def test_a_cli_session_opens_the_task_file_only_when_a_tool_runs(self):
        from vikram import cli

        root = Path(self.directory.name) / "library"
        with patch.dict(os.environ, {"VIKRAM_CHAT_DATA_DIR": str(root)}):
            book = cli._task_book()
            self.assertFalse(root.exists())
            self.book = book
            model = Model(tool_call("list_scheduled_tasks", {}), "None yet.")
            await self.reply(model)
            self.assertTrue((root / "schedules.sqlite3").exists())
            self.assertIn(
                "There are no scheduled tasks.", tool_results(model.bodies[1])[0]
            )
            book.store.close()

    async def test_task_tools_are_offered_only_with_tasks(self):
        model = Model("Hi.")
        await self.reply(model, tasks=False)
        self.assertFalse([n for n in model.offered(0) if "schedule" in n])
        self.assertNotIn("schedule_task", system_prompt(model.bodies[0]))


if __name__ == "__main__":
    unittest.main()


class DesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from vikram.desktop.models import parse_model
        from vikram.desktop.runtime import Runtime
        from vikram.desktop.store import Store

        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name))
        self.events = []
        self.runtime = Runtime(self.store, self.events.append, tasks=True)
        self.started = 0

        def start():
            self.started += 1

        self.runtime.start_runner = start

        async def discover():
            return [
                parse_model(
                    {
                        "name": "local:1",
                        "size": 1,
                        "digest": "a",
                        "details": {"format": "gguf"},
                    },
                    {
                        "capabilities": ["completion", "tools"],
                        "details": {"format": "gguf"},
                    },
                )
            ]

        self.runtime.catalog.refresh = discover
        await self.runtime.refresh()

    async def asyncTearDown(self):
        await self.runtime.stop()
        await self.runtime.close()
        self.store.close()
        self.directory.cleanup()

    async def command(self, method, **params):
        from vikram.desktop.protocol import Command, validate_parameters

        cmd = validate_parameters(Command(v=1, id="x", method=method, params=params))
        return await self.runtime.command(cmd.method, cmd.params, "x")

    async def test_the_window_creates_edits_runs_and_deletes_tasks(self):
        listing = await self.command("schedules")
        self.assertEqual(listing, {"tasks": [], "background": "unavailable"})
        fields = {
            "name": "News",
            "prompt": "Summarize the news.",
            "schedule": {
                "repeat": "weekly",
                "days": ["fri", "mon"],
                "time": "08:00",
                "date": None,
            },
            "model": "local:1",
        }
        task = await self.command("create_schedule", fields=fields)
        self.assertEqual(
            (task["description"], task["origin"]), ("Every Mon, Fri at 08:00", "app")
        )
        self.assertIn({"v": 1, "event": "schedules"}, self.events)
        with self.assertRaisesRegex(ValueError, "available local model"):
            await self.command(
                "update_schedule", task_id=task["id"], fields={"model": "gone:1"}
            )
        paused = await self.command(
            "update_schedule", task_id=task["id"], fields={"enabled": False}
        )
        self.assertEqual(paused["state"], "paused")
        asked = await self.command("run_schedule", task_id=task["id"])
        self.assertTrue(asked["run_requested"])
        self.assertEqual(self.started, 1)
        await self.command("delete_schedule", task_id=task["id"])
        self.assertEqual((await self.command("schedules"))["tasks"], [])

    async def test_a_finished_run_continues_as_a_chat(self):
        task = self.runtime.schedules.create(
            name="News",
            prompt="Summarize the news.",
            schedule=Schedule(repeat="daily", time="08:00"),
            model="local:1",
        )
        self.runtime.schedules.request_run(task["id"])
        _, run = self.runtime.schedules.claim()
        with self.assertRaisesRegex(ValueError, "Wait for this run"):
            await self.command("open_run", run_id=run["id"])
        self.runtime.schedules.checkpoint(
            run["id"], content="Three stories.", actual_model="local:1"
        )
        self.runtime.schedules.finish(run["id"], "complete")
        chat = await self.command("open_run", run_id=run["id"])
        self.assertEqual(chat["title"], "News")
        [message] = chat["messages"]
        self.assertEqual(message["text"], "Summarize the news.")
        self.assertEqual(
            (message["attempts"][0]["status"], message["attempts"][0]["content"]),
            ("complete", "Three stories."),
        )
        # The copy is context for the chat's next reply.
        from vikram.desktop.history import inference_messages

        self.store.save_draft(chat["id"], "More?", 1)
        attempt = self.store.begin(chat["id"], "local:1", None, "r2")
        history, prompt = inference_messages(
            self.store, chat["id"], attempt["message_id"], False
        )
        self.assertEqual(history[1].parts[0].content, "Three stories.")

    async def test_the_window_is_told_about_background_changes(self):
        await self.command("schedules")
        other = ScheduleStore(self.store.root)
        try:
            self.events.clear()
            watcher = asyncio.create_task(self.runtime.watch_schedules(interval=0.01))
            await asyncio.sleep(0.03)
            other.create(
                name="News",
                prompt="News.",
                schedule=Schedule(repeat="daily", time="08:00"),
                model="m",
            )
            for _ in range(50):
                if {"v": 1, "event": "schedules"} in self.events:
                    break
                await asyncio.sleep(0.01)
            watcher.cancel()
            self.assertIn({"v": 1, "event": "schedules"}, self.events)
        finally:
            other.close()

    async def test_a_chat_reply_saves_a_task_after_the_window_approves(self):
        from vikram.desktop.runtime import Runtime

        model = Model(
            tool_call(
                "schedule_task",
                {
                    "name": "Dentist",
                    "prompt": "Remind me to call the dentist.",
                    "repeat": "once",
                    "date": "tomorrow",
                    "time": "3pm",
                },
            ),
            "Saved.",
        )
        runtime = Runtime(
            self.store,
            self.events.append,
            streamer=lambda *a, **o: stream_reply(
                *a, transport=httpx.MockTransport(model), **o
            ),
            tasks=True,
        )
        runtime.catalog.refresh = self.runtime.catalog.refresh
        try:
            chat = self.store.create_chat("local:1")
            self.store.save_draft(
                chat["id"], "Remind me tomorrow at 3pm to call the dentist.", 1
            )
            await runtime.send(chat["id"], "r1")
            for _ in range(200):
                if runtime.waiting:
                    break
                await asyncio.sleep(0.01)
            [(call_id, (attempt_id, kind, request, _))] = runtime.waiting.items()
            self.assertEqual(kind, "schedule")
            self.assertTrue(request.summary.startswith("Schedule “Dentist” · Once on"))
            runtime.respond(
                {
                    "chat_id": chat["id"],
                    "attempt_id": attempt_id,
                    "interaction_id": call_id,
                    "kind": "schedule",
                    "decision": "approve",
                }
            )
            await runtime.task
            [task] = runtime.schedules.tasks()
            self.assertEqual(
                (task["name"], task["origin"], task["model"]),
                ("Dentist", "chat", "local:1"),
            )
            self.assertEqual(task["schedule"]["time"], "15:00")
            saved = self.store.chat(chat["id"])["messages"][0]["attempts"][0]
            self.assertEqual(saved["activity"][0]["status"], "complete")
        finally:
            await runtime.stop()
            await runtime.close()

    def test_protocol_validates_task_commands(self):
        from vikram.desktop.protocol import validate_command

        def check(method, params):
            return validate_command(
                json.dumps({"v": 1, "id": "x", "method": method, "params": params})
            )

        with self.assertRaisesRegex(ValueError, "name, prompt, schedule, and model"):
            check("create_schedule", {"fields": {"name": "a"}})
        for bad in (
            {"task_id": "nope", "fields": {"enabled": True}},
            {"task_id": "a" * 32, "fields": {"enabled": "yes"}},
            {"task_id": "a" * 32, "fields": {"schedule": {"repeat": "yearly"}}},
            {"task_id": "a" * 32, "fields": {"origin": "chat"}},
            {"task_id": "a" * 32, "fields": {"name": None}},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                check("update_schedule", bad)
        respond = check(
            "respond",
            {
                "chat_id": "a" * 32,
                "attempt_id": "b" * 32,
                "interaction_id": "c",
                "kind": "schedule",
                "decision": "approve",
            },
        )
        self.assertEqual(respond.params["kind"], "schedule")
