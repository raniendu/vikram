import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from vikram import bots as botsmod
from vikram import schedules
from vikram.bots import (
    BotConfig,
    BotStore,
    config_from,
    importable,
    parse_report,
    should_notify,
    templates,
)
from vikram.bots_cli import render_home
from vikram.schedules import Schedule, ScheduleStore

LA = ZoneInfo("America/Los_Angeles")


def setUpModule():
    unittest.enterModuleContext(patch("vikram.schedules.local_zone", return_value=LA))
    unittest.enterModuleContext(patch("vikram.bots.local_zone", return_value=LA))


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=LA)


class Clock:
    def __init__(self, moment: datetime):
        self.now = moment.timestamp()

    def __call__(self) -> float:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta).total_seconds()


def report(outcome="updated", summary="", findings=()):
    body = json.dumps(
        {"outcome": outcome, "summary": summary, "findings": list(findings)}
    )
    return f"Checked the sources.\n\n```vikram-report\n{body}\n```"


def finding(key, title, severity="material", **extra):
    return {
        "key": key,
        "title": title,
        "severity": severity,
        "confidence": "high",
        **extra,
    }


class ConfigTests(unittest.TestCase):
    def test_templates_fill_defaults_and_blocked_ones_refuse(self):
        config = config_from({"template": "topic-watcher", "name": "Immigration"})
        self.assertEqual(config.schedule.repeat, "weekly")
        self.assertEqual(config.expected_days, 10)
        self.assertIn("material", config.charter)
        blocked = next(t for t in templates() if t["id"] == "dependency-keeper")
        self.assertFalse(blocked["available"])
        self.assertIn("sandbox", blocked["reason"])
        with self.assertRaisesRegex(ValueError, "not available"):
            config_from({"template": "dependency-keeper", "name": "Deps"})
        with self.assertRaisesRegex(ValueError, "Unknown bot template"):
            config_from({"template": "nope", "name": "x"})

    def test_cloud_models_need_the_explicit_grant(self):
        fields = {
            "name": "Watch",
            "mandate": "Watch.",
            "schedule": {"repeat": "daily", "time": "08:00"},
            "model": "openrouter:openai/gpt-5",
        }
        with self.assertRaisesRegex(ValueError, "Allow OpenRouter"):
            config_from(fields)
        config = config_from({**fields, "cloud": True})
        self.assertTrue(config.cloud)
        # An export never carries the grant into another library.
        fields = importable(config.model_dump(mode="json"))
        self.assertEqual((fields["cloud"], fields["model"]), (False, None))
        self.assertIsNone(config_from(fields).model)

    def test_invalid_fields_fail_with_a_precise_message(self):
        with self.assertRaisesRegex(ValueError, "name and a mandate"):
            config_from(
                {
                    "name": " ",
                    "mandate": "x",
                    "schedule": Schedule(repeat="daily", time="08:00"),
                }
            )
        with self.assertRaisesRegex(ValueError, "Invalid schedule"):
            config_from({"name": "x", "mandate": "x", "schedule": {"repeat": "weekly"}})
        with self.assertRaisesRegex(ValueError, "budget_minutes"):
            config_from(
                {
                    "name": "x",
                    "mandate": "x",
                    "schedule": {"repeat": "daily", "time": "08:00"},
                    "budget_minutes": 999,
                }
            )

    def test_reports_are_parsed_leniently_but_validated(self):
        self.assertIsNone(parse_report("No report here."))
        self.assertIsNone(parse_report("```vikram-report\nnot json\n```"))
        parsed = parse_report(report("quiet"))
        self.assertEqual((parsed.outcome, parsed.findings), ("quiet", []))
        bare = 'Done.\n{"outcome": "updated", "findings": [{"title": "A"}]}'
        self.assertEqual(parse_report(bare).findings[0].severity, "material")
        fenced = 'x\n```json\n{"outcome": "blocked", "summary": "Site down", "findings": []}\n```'
        self.assertEqual(parse_report(fenced).outcome, "blocked")
        self.assertIsNone(parse_report('```vikram-report\n{"outcome":"quiet"}\n```'))

    def test_schedule_repair_refuses_nonfinite_and_unparsable_numbers(self):
        from vikram.schedules import repair_schedule, schedule_from

        for fields in (
            {"repeat": "interval", "every_hours": float("inf")},
            {"repeat": "interval", "every_hours": float("nan")},
            {"repeat": "interval", "every_minutes": "²"},
            {"repeat": "monthly", "day_of_month": "²", "time": "08:00"},
        ):
            repaired = repair_schedule(fields)
            with self.assertRaises(ValueError):
                schedule_from(repaired)

    def test_notification_policy(self):
        self.assertTrue(should_notify("attention", "needs-attention"))
        self.assertTrue(should_notify("attention", "failed"))
        self.assertFalse(should_notify("attention", "updated"))
        self.assertFalse(should_notify("attention", "quiet"))
        self.assertTrue(should_notify("findings", "updated"))
        self.assertFalse(should_notify("never", "failed"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock(at("2026-11-09T07:00"))
        self.store = ScheduleStore(Path(self.directory.name), clock=self.clock)
        self.bots = BotStore(self.store)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def bot(self, **fields):
        return self.bots.create(
            {
                "template": "topic-watcher",
                "name": "Immigration",
                "sources": ["https://example.gov/news"],
                "model": "local:1",
                **fields,
            }
        )

    def test_bots_are_tasks_of_their_own_kind(self):
        bot = self.bot()
        self.assertEqual(self.store.tasks(), [])
        [task] = self.store.tasks(kind="bot")
        self.assertEqual((task["kind"], task["name"]), ("bot", "Immigration"))
        self.assertTrue(self.store.due_file.is_file())
        with self.assertRaisesRegex(ValueError, "No scheduled task"):
            self.store.find("Immigration")
        self.assertEqual(self.bots.find("immi")["id"], bot["id"])
        self.assertEqual(bot["config"]["template"], "topic-watcher")
        self.assertEqual(bot["description"], "Every Mon at 08:00")

    def test_export_and_import_round_trip_without_the_cloud_grant(self):
        bot = self.bot(cloud=True, model="openrouter:openai/gpt-5")
        exported = self.bots.export(bot["id"])
        self.assertTrue(exported["cloud"])
        copied = self.bots.create(importable(exported))
        self.assertEqual(copied["config"]["charter"], exported["charter"])
        self.assertEqual(copied["config"]["schedule"], exported["schedule"])
        self.assertFalse(copied["config"]["cloud"])
        self.assertIsNone(copied["config"].get("model"))

    def test_update_changes_config_and_pause_needs_no_other_field(self):
        bot = self.bot()
        changed = self.bots.update(
            bot["id"],
            {"name": "Visas", "materiality": {"keywords": ["H-1B"]}, "enabled": False},
        )
        self.assertEqual((changed["name"], changed["state"]), ("Visas", "paused"))
        self.assertEqual(changed["config"]["materiality"]["keywords"], ["H-1B"])
        self.assertEqual(changed["config"]["materiality"]["dedupe_days"], 14)
        self.assertEqual(self.store.tasks(kind="bot")[0]["name"], "Visas")
        resumed = self.bots.update(bot["id"], {"enabled": True})
        self.assertEqual(resumed["state"], "scheduled")
        cleared = self.bots.update(bot["id"], {"model": None})
        self.assertIsNone(cleared["model"])
        with self.assertRaisesRegex(ValueError, "Allow OpenRouter"):
            self.bots.update(bot["id"], {"model": "openrouter:openai/gpt-5"})

    def test_outcomes_are_derived_from_findings_not_the_models_word(self):
        bot = self.bot()
        record = lambda text, run="r": self.bots.record(
            bot["id"], run, parse_report(text), error=None
        )
        # Quiet means nothing surfaced, whatever the model claimed.
        self.assertEqual(record(report("needs-attention"))[0], "quiet")
        outcome, reason = record(
            report("quiet", "Fee changed", [finding("fee", "Fee changed", "attention")])
        )
        self.assertEqual((outcome, reason), ("needs-attention", "Fee changed (1 new)"))
        self.assertEqual(
            record(report("updated", "", [finding("faq", "FAQ")]))[0], "updated"
        )
        # Blocked always leaves an attention finding behind.
        outcome, _ = record(report("blocked", "The site timed out."))
        self.assertEqual(outcome, "blocked")
        blocked = [
            f for f in self.bots.findings(task_id=bot["id"]) if f["key"] == "blocked"
        ]
        self.assertEqual(blocked[0]["detail"], "The site timed out.")
        # A missing report is a failure finding, never silence.
        outcome, _ = self.bots.record(bot["id"], "r", None, error=None)
        self.assertEqual(outcome, "failed")
        self.assertIn(
            "report-missing", [f["key"] for f in self.bots.findings(task_id=bot["id"])]
        )

    def test_repeats_merge_and_dismissed_ones_stay_quiet_until_the_fact_changes(self):
        bot = self.bot()
        fee = finding("fee", "Fee changed", "attention", detail="Now 100")
        self.bots.record(
            bot["id"], "r1", parse_report(report("updated", "", [fee])), error=None
        )
        [stored] = self.bots.findings(task_id=bot["id"])
        self.assertEqual((stored["state"], stored["count"]), ("new", 1))
        outcome, reason = self.bots.record(
            bot["id"], "r2", parse_report(report("updated", "", [fee])), error=None
        )
        self.assertEqual((outcome, reason), ("quiet", "1 merged"))
        self.assertEqual(self.bots.findings(task_id=bot["id"])[0]["count"], 2)
        self.bots.set_finding(stored["id"], "dismissed")
        outcome, reason = self.bots.record(
            bot["id"], "r3", parse_report(report("updated", "", [fee])), error=None
        )
        self.assertEqual((outcome, reason), ("quiet", "1 suppressed"))
        self.assertEqual(self.bots.home()["needs_you"], [])
        changed = finding("fee", "Fee changed", "attention", detail="Now 250")
        outcome, reason = self.bots.record(
            bot["id"], "r4", parse_report(report("updated", "", [changed])), error=None
        )
        self.assertEqual((outcome, reason), ("needs-attention", "1 reopened"))
        [again] = self.bots.home()["needs_you"]
        self.assertEqual((again["detail"], again["state"]), ("Now 250", "new"))

    def test_resolved_findings_stay_quiet_inside_the_dedupe_window(self):
        bot = self.bot()
        item = finding("faq", "FAQ updated")
        self.bots.record(
            bot["id"], "r1", parse_report(report("updated", "", [item])), error=None
        )
        [stored] = self.bots.findings(task_id=bot["id"])
        self.bots.set_finding(stored["id"], "resolved")
        self.clock.advance(days=3)
        outcome, _ = self.bots.record(
            bot["id"], "r2", parse_report(report("updated", "", [item])), error=None
        )
        self.assertEqual(outcome, "quiet")
        self.clock.advance(days=15)
        outcome, reason = self.bots.record(
            bot["id"], "r3", parse_report(report("updated", "", [item])), error=None
        )
        self.assertEqual((outcome, reason), ("updated", "1 reopened"))

    def test_snoozing_quiets_until_the_time_and_feedback_teaches_the_bot(self):
        bot = self.bot()
        item = finding("rumour", "A rumour", "attention")
        self.bots.record(
            bot["id"], "r1", parse_report(report("updated", "", [item])), error=None
        )
        [stored] = self.bots.home()["needs_you"]
        with self.assertRaisesRegex(ValueError, "future time"):
            self.bots.set_finding(stored["id"], "snoozed", until=self.clock() - 1)
        self.bots.set_finding(stored["id"], "snoozed", until=self.clock() + 3600)
        self.assertEqual(self.bots.home()["needs_you"], [])
        self.clock.advance(hours=2)
        self.assertEqual(
            [f["id"] for f in self.bots.home()["needs_you"]], [stored["id"]]
        )
        taught = self.bots.feedback(stored["id"], "not-material")
        self.assertEqual(taught["state"], "dismissed")
        config = self.bots.bot(bot["id"])["config"]
        self.assertEqual(config["materiality"]["examples"], ["Not material: A rumour"])
        _, prompt = self.bots.run_prompt(bot["id"], BotConfig.model_validate(config))
        self.assertIn("closed by the person", prompt)
        self.assertIn("[rumour] A rumour", prompt)

    def test_low_confidence_and_keyword_gates_hold_findings_off_home(self):
        bot = self.bot(materiality={"keywords": ["visa"], "max_attention": 1})
        items = [
            finding("a", "Visa rule changed", "attention"),
            finding("b", "Visa fee changed", "attention"),
            finding("c", "Something else", "attention"),
            finding("d", "Visa guess", "attention", confidence="low"),
        ]
        outcome, reason = self.bots.record(
            bot["id"], "r1", parse_report(report("updated", "", items)), error=None
        )
        self.assertEqual((outcome, reason), ("needs-attention", "4 new"))
        home = self.bots.home()
        self.assertEqual([f["key"] for f in home["needs_you"]], ["a"])
        self.assertEqual([f["key"] for f in home["material"]], ["b"])
        held = [f for f in self.bots.bot(bot["id"])["findings"] if f["held"]]
        self.assertEqual(sorted(f["key"] for f in held), ["c", "d"])
        self.assertEqual(home["bots"][0]["held"], 2)

    def test_home_lists_overdue_bots_and_reset_closes_findings(self):
        bot = self.bot(expected_days=2)
        self.assertEqual(self.bots.home()["stale"], [])
        self.clock.advance(days=3)
        self.assertEqual([b["id"] for b in self.bots.home()["stale"]], [bot["id"]])
        self.bots.update(bot["id"], {"enabled": False})
        self.assertEqual(self.bots.home()["stale"], [])
        self.bots.record(
            bot["id"],
            "r1",
            parse_report(report("updated", "", [finding("x", "X")])),
            error=None,
        )
        self.bots.reset(bot["id"])
        self.assertEqual(self.bots.findings(task_id=bot["id"]), [])
        self.assertEqual(self.bots.state(bot["id"]), {})
        text = render_home(self.bots.home())
        self.assertIn("Nothing.", text)
        self.assertIn("Immigration", text)

    def test_failures_become_findings_and_the_limit_holds(self):
        bot = self.bot()
        for _ in range(3):
            self.bots.record(bot["id"], "r", None, error="Ollama is not running.")
        [failure] = self.bots.findings(task_id=bot["id"])
        self.assertEqual((failure["kind"], failure["count"]), ("failure", 3))
        self.assertEqual(failure["title"], "Immigration could not run")
        for _ in range(botsmod.MAX_BOTS - 1):
            self.bot(name="More")
        with self.assertRaisesRegex(ValueError, "already"):
            self.bot(name="Too many")

    def test_conversion_preserves_history_but_pauses_and_strips_cloud(self):
        task = self.store.create(
            name="Old news",
            prompt="Watch the news.",
            schedule=Schedule(repeat="daily", time="08:00"),
            model="openrouter:openai/gpt-5",
        )
        self.clock.advance(hours=1)
        _, run = self.store.claim()
        self.store.checkpoint(run["id"], content="An old result.")
        self.store.finish(run["id"], "complete")
        converted = self.bots.convert_schedule(task["id"])
        self.assertEqual(converted["id"], task["id"])
        self.assertFalse(converted["enabled"])
        self.assertFalse(converted["cloud"])
        self.assertIsNone(converted["model"])
        self.assertEqual(converted["runs"][0]["id"], run["id"])
        self.assertEqual(self.store.run(run["id"])["content"], "An old result.")
        self.assertEqual(self.store.tasks(), [])
        self.assertFalse(self.store.due_file.exists())

    def test_changing_config_and_resetting_are_refused_while_running(self):
        bot = self.bot()
        self.store.request_run(bot["id"])
        _, run = self.store.claim()
        with self.assertRaisesRegex(ValueError, "finish"):
            self.bots.update(bot["id"], {"name": "New"})
        with self.assertRaisesRegex(ValueError, "finish"):
            self.bots.reset(bot["id"])
        self.bots.update(bot["id"], {"enabled": False})  # Pausing reduces future work.
        self.store.finish(run["id"], "stopped")
        self.bots.reset(bot["id"])

    def test_failed_checks_do_not_advance_the_last_verified_baseline(self):
        bot = self.bot()
        self.bots.record(bot["id"], "r1", parse_report(report("quiet")), error=None)
        checked_at = self.bots.state(bot["id"])["last_run_at"]
        self.clock.advance(days=1)
        self.bots.record(bot["id"], "r2", None, error="A source failed.")
        self.assertEqual(self.bots.state(bot["id"])["last_run_at"], checked_at)
        self.clock.advance(days=1)
        self.bots.record(bot["id"], "r3", parse_report(report("blocked")), error=None)
        self.assertEqual(self.bots.state(bot["id"])["last_run_at"], checked_at)

    def test_delivery_receipts_dedupe_failures_even_when_delivery_is_uncertain(self):
        bot = self.bot()
        self.bots.record(bot["id"], "r", None, error="Ollama unavailable.")
        deliveries = []

        def uncertain(*args):
            deliveries.append(args)
            raise OSError("Delivery unavailable")

        self.bots.deliver(bot["id"], bot["name"], "failed", uncertain)
        self.bots.deliver(bot["id"], bot["name"], "failed", uncertain)
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(self.bots.state(bot["id"])["delivery"]["status"], "uncertain")
        self.bots.record(bot["id"], "r2", None, error="Model removed.")
        self.bots.deliver(bot["id"], bot["name"], "failed", uncertain)
        self.assertEqual(len(deliveries), 2)  # Underlying failure changed.

    def test_interrupted_and_missed_runs_surface_on_home_without_replay(self):
        bot = self.bot()
        self.store.request_run(bot["id"])
        _, run = self.store.claim()
        self.store.interrupt_stale()
        home = self.bots.home()
        self.assertEqual(home["needs_you"][0]["key"], "execution-gap")
        self.assertEqual(self.store.run(run["id"])["outcome"], "incomplete")
        self.assertNotIn("last_run_at", self.bots.state(bot["id"]))
        before = self.bots.findings(task_id=bot["id"])[0]["count"]
        self.bots.home()
        self.assertEqual(self.bots.findings(task_id=bot["id"])[0]["count"], before)
        self.clock.advance(days=2)
        self.assertIsNone(self.store.claim())  # Too late: marked missed, not replayed.
        self.assertEqual(self.bots.home()["needs_you"][0]["key"], "execution-gap")

    def test_corrupt_state_and_sources_fail_visibly_and_reset_is_explicit(self):
        bot = self.bot()
        self.bots.record(
            bot["id"],
            "r",
            parse_report(report("updated", "", [finding("fee", "Fee changed")])),
            error=None,
        )
        with self.store.db:
            self.store.db.execute(
                "UPDATE bot_state SET value='not-json' WHERE task_id=?", (bot["id"],)
            )
        with self.assertRaisesRegex(ValueError, "saved state"):
            self.bots.bot(bot["id"])
        self.bots.reset(bot["id"])
        self.assertEqual(self.bots.state(bot["id"]), {})
        self.bots.record(
            bot["id"],
            "r2",
            parse_report(report("updated", "", [finding("fee", "Fee changed again")])),
            error=None,
        )
        with self.store.db:
            self.store.db.execute(
                "UPDATE findings SET sources='broken' WHERE task_id=?", (bot["id"],)
            )
        with self.assertRaisesRegex(ValueError, "saved sources"):
            self.bots.home()
        self.bots.delete(bot["id"])
        self.assertEqual(self.bots.home()["bots"], [])

    def test_an_old_library_is_migrated(self):
        import sqlite3

        root = Path(self.directory.name) / "old"
        root.mkdir()
        db = sqlite3.connect(root / "schedules.sqlite3")
        db.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL,
                schedule TEXT NOT NULL, model TEXT, thinking TEXT NOT NULL DEFAULT 'null',
                enabled INTEGER NOT NULL DEFAULT 1, origin TEXT NOT NULL DEFAULT 'app',
                anchor REAL NOT NULL, next_run REAL, run_requested INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE runs (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                trigger TEXT NOT NULL, scheduled_for REAL, started REAL NOT NULL,
                finished REAL, status TEXT NOT NULL, prompt TEXT NOT NULL,
                model TEXT, actual_model TEXT, content TEXT NOT NULL DEFAULT '',
                reasoning TEXT NOT NULL DEFAULT '', activity TEXT NOT NULL DEFAULT '[]',
                sources TEXT NOT NULL DEFAULT '[]', searches TEXT NOT NULL DEFAULT '[]',
                error TEXT
            );
            INSERT INTO tasks VALUES('a','Old','Prompt','{"repeat":"daily","time":"08:00"}',
                'local:1','null',1,'app',0,9e9,0,0,0);
            PRAGMA user_version=1;
            """)
        db.commit()
        db.close()
        store = ScheduleStore(root, clock=self.clock)
        try:
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 2)
            [task] = store.tasks()
            self.assertEqual((task["name"], task["kind"]), ("Old", "task"))
            self.assertEqual(BotStore(store).bots(), [])
        finally:
            store.close()


class FakeCatalog:
    def __init__(self, models):
        self.models = models
        self.cloud = []

    async def refresh(self, include_cloud=False):
        self.cloud.append(include_cloud)
        return self.models


LOCAL = {
    "name": "local:1",
    "available": True,
    "vision": False,
    "tools": True,
    "thinking": [],
    "thinking_default": None,
}
CLOUD = {**LOCAL, "name": "openrouter:openai/gpt-5"}
PLAIN = {**LOCAL, "name": "plain:1", "tools": False}


def streamer_with(text, seen=None, delay=0.0):
    async def streamer(model, thinking, history, prompt, **options):
        if seen is not None:
            seen.update(options, model=model, prompt=prompt, history=history)
        yield {"text": "Checking…", "phase": "answering", "activity": []}
        if delay:
            await asyncio.sleep(delay)
        yield {
            "text": text,
            "phase": "answering",
            "actual_model": model,
            "sources": [{"url": "https://example.gov/news"}, {"url": "https://x"}],
            "searches": [
                {
                    "tool": "web_fetch",
                    "status": "complete",
                    "error": None,
                    "arguments": {"urls": ["https://example.gov/news"]},
                }
            ],
        }

    return streamer


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.clock = Clock(at("2026-11-09T07:59"))
        self.store = ScheduleStore(Path(self.directory.name), clock=self.clock)
        self.bots = BotStore(self.store)
        self.alerts = []

    async def asyncTearDown(self):
        self.store.close()
        self.directory.cleanup()

    def bot(self, **fields):
        return self.bots.create(
            {
                "template": "topic-watcher",
                "name": "Immigration",
                "schedule": {"repeat": "daily", "time": "08:00"},
                "sources": ["https://example.gov/news"],
                "model": "local:1",
                **fields,
            }
        )

    async def run_due(self, streamer, catalog=None):
        self.clock.advance(minutes=2)
        return await schedules.run_due(
            self.store,
            streamer=streamer,
            catalog=catalog or FakeCatalog([LOCAL]),
            alert=lambda *args: self.alerts.append(args),
        )

    async def test_a_run_is_unattended_reports_findings_and_notifies_by_policy(self):
        bot = self.bot()
        seen = {}
        text = report(
            "updated",
            "One change.",
            [finding("fee", "Fee changed", "attention", sources=["https://x"])],
        )
        count = await self.run_due(streamer_with(text, seen))
        self.assertEqual(count, 1)
        self.assertEqual(seen["model"], "local:1")
        self.assertEqual(seen["history"], [])
        self.assertIsNone(seen["browser"])
        self.assertNotIn("tasks", seen)
        self.assertIn("“Immigration” (bot, every day at 08:00)", seen["scheduled"])
        self.assertIn("You are “Immigration”", seen["instructions"])
        self.assertIn("vikram-report", seen["instructions"])
        self.assertIn("https://example.gov/news", seen["instructions"])
        self.assertIn("first run", seen["prompt"][0])
        detail = self.bots.bot(bot["id"])
        [run] = detail["runs"]
        self.assertEqual(
            (run["status"], run["outcome"]), ("complete", "needs-attention")
        )
        self.assertEqual(run["report"], "One change. (1 new)")
        self.assertEqual(self.store.run(run["id"])["prompt"], seen["prompt"][0])
        self.assertEqual([f["key"] for f in detail["findings"]], ["fee"])
        self.assertEqual(self.alerts, [("Immigration", "needs-attention")])
        # The next run is told what was already reported.
        seen.clear()
        self.store.request_run(bot["id"])
        await self.run_due(streamer_with(report("quiet", "Nothing new."), seen))
        self.assertIn("[fee] Fee changed", seen["prompt"][0])
        self.assertIn("outcome needs-attention", seen["prompt"][0])
        runs = self.bots.bot(bot["id"])["runs"]
        self.assertEqual(runs[0]["outcome"], "quiet")
        self.assertEqual(len(self.alerts), 1)  # Quiet runs never notify.

    async def test_real_reply_loop_shares_safe_tools_and_retrieval_evidence(self):
        import httpx
        from test_toolkit import Model, fake_vm, tool_call

        from vikram.conversation import stream_reply
        from vikram.web import WebSession

        bot = self.bot()
        model = Model(
            tool_call("web_fetch", {"urls": ["https://example.gov/news"]}),
            report(
                "updated",
                "Official change.",
                [
                    finding(
                        "fee",
                        "Official fee changed",
                        "attention",
                        sources=["https://example.gov/news"],
                    )
                ],
            ),
        )

        async def remote(*args):
            return {
                "results": [
                    {
                        "url": "https://example.gov/news",
                        "title": "Official news",
                        "excerpts": ["The official fee changed today."],
                    }
                ]
            }

        def session(*args, **kwargs):
            return WebSession(*args, remote=remote, **kwargs)

        with fake_vm(), patch("vikram.conversation.WebSession", side_effect=session):
            await self.run_due(
                lambda *a, **kw: stream_reply(
                    *a, transport=httpx.MockTransport(model), **kw
                )
            )
        offered = model.offered(0)
        self.assertIn("web_fetch", offered)
        self.assertIn("run_code", offered)
        self.assertFalse(
            any(
                name.startswith(("browser_", "mcp__", "ext__"))
                or name
                in (
                    "bash",
                    "read",
                    "write",
                    "edit",
                    "schedule_task",
                    "ask_user_question",
                )
                for name in offered
            )
        )
        [run] = self.bots.bot(bot["id"])["runs"]
        self.assertEqual(run["outcome"], "needs-attention")
        self.assertEqual(self.store.run(run["id"])["searches"][0]["status"], "complete")

    async def test_a_missing_report_fails_the_run_and_surfaces_on_home(self):
        self.bot()
        await self.run_due(streamer_with("I looked around and found nothing."))
        [listed] = self.bots.bots()
        [run] = self.bots.bot(listed["id"])["runs"]
        self.assertEqual((run["status"], run["outcome"]), ("complete", "failed"))
        [item] = self.bots.home()["needs_you"]
        self.assertEqual(item["key"], "report-missing")
        self.assertEqual(self.alerts, [("Immigration", "failed")])

    async def test_the_budget_ends_a_run_as_incomplete(self):
        bot = self.bot(budget_minutes=1)
        real_timeout = asyncio.timeout
        with patch.object(botsmod.asyncio, "timeout", lambda s: real_timeout(0.05)):
            await self.run_due(streamer_with(report("quiet"), delay=5))
        [run] = self.bots.bot(bot["id"])["runs"]
        self.assertEqual((run["status"], run["outcome"]), ("complete", "incomplete"))
        self.assertIn("1-minute budget", run["error"])
        self.assertEqual(self.store.run(run["id"])["content"], "Checking…")
        [item] = self.bots.home()["needs_you"]
        self.assertEqual(item["title"], "Immigration ran out of time")
        self.assertEqual(self.alerts, [("Immigration", "incomplete")])

    async def test_cloud_runs_need_the_bots_grant_and_tools(self):
        from vikram.settings import Settings

        inherits = self.bot(name="Inherits", model=None)
        Settings(root=self.store.root).set("model", "openrouter:openai/gpt-5")
        catalog = FakeCatalog([LOCAL, CLOUD, PLAIN])
        await self.run_due(streamer_with(report("quiet")), catalog)
        [run] = self.bots.bot(inherits["id"])["runs"]
        self.assertEqual((run["status"], run["outcome"]), ("failed", "failed"))
        self.assertIn("inherits the default model", run["error"])
        self.assertEqual(catalog.cloud, [])
        granted = self.bot(name="Granted", model="openrouter:openai/gpt-5", cloud=True)
        self.store.request_run(granted["id"])
        await self.run_due(streamer_with(report("quiet")), catalog)
        [run] = self.bots.bot(granted["id"])["runs"]
        self.assertEqual((run["status"], run["outcome"]), ("complete", "quiet"))
        self.assertEqual(catalog.cloud, [True])
        plain = self.bot(name="Plain", model="plain:1")
        self.store.request_run(plain["id"])
        await self.run_due(streamer_with(report("quiet")), catalog)
        [run] = self.bots.bot(plain["id"])["runs"]
        self.assertIn("tool-capable", run["error"])

    async def test_source_failure_and_unchecked_reports_cannot_claim_quiet(self):
        bot = self.bot()

        async def unchecked(*args, **kwargs):
            yield {"text": report("quiet"), "phase": "answering"}

        await self.run_due(unchecked)
        [run] = self.bots.bot(bot["id"])["runs"]
        self.assertEqual(run["outcome"], "blocked")
        self.assertNotIn("last_run_at", self.bots.state(bot["id"]))
        self.assertEqual(self.bots.home()["needs_you"][0]["key"], "blocked")
        self.store.request_run(bot["id"])

        async def failed(*args, **kwargs):
            yield {
                "text": report("quiet"),
                "sources": [{"url": "https://example.gov/news"}],
                "searches": [
                    {"tool": "web_fetch", "status": "failed", "error": "Down"}
                ],
            }

        await self.run_due(failed)
        self.assertEqual(self.bots.bot(bot["id"])["runs"][0]["outcome"], "blocked")
        self.assertNotIn("last_run_at", self.bots.state(bot["id"]))

    async def test_unsupported_citations_are_held_not_shown_on_home(self):
        bot = self.bot()
        await self.run_due(
            streamer_with(
                report(
                    "updated",
                    "",
                    [
                        finding(
                            "unverified",
                            "A claim",
                            "attention",
                            sources=["https://invented.example"],
                        )
                    ],
                )
            )
        )
        [run] = self.bots.bot(bot["id"])["runs"]
        self.assertEqual(run["outcome"], "quiet")
        self.assertEqual(self.bots.home()["needs_you"], [])
        self.assertTrue(self.bots.bot(bot["id"])["findings"][0]["held"])

    async def test_tool_budget_is_reported_as_incomplete(self):
        from pydantic_ai.exceptions import UsageLimitExceeded

        from vikram.errors import ReplyError

        bot = self.bot(max_requests=2, max_tool_calls=3)
        seen = {}

        async def exhausted(*args, **options):
            seen.update(options)
            yield {"text": "Partial evidence."}
            error = ReplyError("Usage limit.")
            error.__cause__ = UsageLimitExceeded("Too many tools")
            raise error

        await self.run_due(exhausted)
        self.assertEqual(seen["usage_limits"].request_limit, 2)
        self.assertEqual(seen["usage_limits"].tool_calls_limit, 3)
        [run] = self.bots.bot(bot["id"])["runs"]
        self.assertEqual(run["outcome"], "incomplete")
        self.assertNotIn("last_run_at", self.bots.state(bot["id"]))

    async def test_a_stop_leaves_no_finding(self):
        bot = self.bot()
        self.clock.advance(minutes=2)
        work = asyncio.create_task(
            schedules.run_due(
                self.store,
                streamer=streamer_with(report("quiet"), delay=30),
                catalog=FakeCatalog([LOCAL]),
                alert=lambda *a: self.alerts.append(a),
            )
        )
        await asyncio.sleep(0.05)
        work.cancel()
        await work
        [run] = self.bots.bot(bot["id"])["runs"]
        self.assertEqual((run["status"], run["outcome"]), ("stopped", None))
        self.assertEqual(self.bots.home()["needs_you"], [])
        self.assertEqual(self.alerts, [])


class CliTests(unittest.TestCase):
    def test_cli_create_edit_import_and_finding_states_share_the_store(self):
        import contextlib
        import io

        from vikram.cli import main

        with tempfile.TemporaryDirectory() as directory:

            def command(*args):
                output = io.StringIO()
                with (
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(output),
                ):
                    status = main(list(args))
                self.assertEqual(status, 0, output.getvalue())
                return output.getvalue()

            with patch.dict("os.environ", {"VIKRAM_CHAT_DATA_DIR": directory}):
                command(
                    "bots",
                    "create",
                    "Immigration",
                    "--template",
                    "topic-watcher",
                    "--source",
                    "https://example.gov/news",
                    "--max-requests",
                    "2",
                )
                exported = json.loads(command("bots", "export", "Immigration"))
                self.assertEqual(exported["max_requests"], 2)
                exported["mandate"] = "Track new official rules."
                path = Path(directory) / "bot.json"
                path.write_text(json.dumps(exported))
                command("bots", "edit", "Immigration", str(path))
                self.assertIn(
                    "Track new official rules", command("bots", "show", "Immigration")
                )
                command("bots", "pause", "Immigration")
                command("bots", "import", str(path))
                store = ScheduleStore(Path(directory))
                try:
                    bots = BotStore(store)
                    self.assertEqual(len(bots.bots()), 2)
                    self.assertTrue(all(not b["enabled"] for b in bots.bots()))
                    bot = bots.bots()[0]
                    bots.record(
                        bot["id"],
                        "r1",
                        parse_report(
                            report(
                                "updated",
                                "",
                                [finding("fee", "Fee changed", "attention")],
                            )
                        ),
                        error=None,
                    )
                    [item] = bots.home()["needs_you"]
                    self.assertIn("Fee changed", command("home"))
                    command("findings", "dismiss", item["id"][:8])
                    self.assertEqual(
                        json.loads(command("home", "--json"))["needs_you"], []
                    )
                    command("findings", "reopen", item["id"][:8])
                    self.assertIn("Fee changed", command("findings", "--json"))
                finally:
                    store.close()


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

        async def discover(include_cloud=False):
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

    async def test_the_window_manages_bots_and_findings(self):
        self.assertIn("bots", (await self.command("hello"))["capabilities"])
        home = await self.command("home")
        self.assertEqual(
            home, {"needs_you": [], "material": [], "stale": [], "bots": []}
        )
        listing = await self.command("bot_templates")
        self.assertIn("topic-watcher", [t["id"] for t in listing["templates"]])
        with self.assertRaisesRegex(ValueError, "available model"):
            await self.command(
                "create_bot",
                fields={"template": "topic-watcher", "name": "X", "model": "gone:1"},
            )
        bot = await self.command(
            "create_bot",
            fields={
                "template": "topic-watcher",
                "name": "Immigration",
                "model": "local:1",
            },
        )
        self.assertEqual(bot["name"], "Immigration")
        self.assertEqual((await self.command("bots"))["bots"][0]["id"], bot["id"])
        # Bots never appear among the scheduled prompts.
        self.assertEqual((await self.command("schedules"))["tasks"], [])
        changed = await self.command(
            "update_bot", task_id=bot["id"], fields={"mandate": "Watch visas."}
        )
        self.assertEqual(changed["mandate"], "Watch visas.")
        await self.command("run_bot", task_id=bot["id"])
        self.assertEqual(self.started, 1)
        self.assertTrue((await self.command("bot", task_id=bot["id"]))["run_requested"])
        # A finding's state changes tell the window to refresh.
        self.runtime.bots.record(
            bot["id"],
            "r",
            parse_report(report("updated", "", [finding("fee", "Fee", "attention")])),
            error=None,
        )
        [item] = (await self.command("home"))["needs_you"]
        seen = await self.command("finding", finding_id=item["id"], state="seen")
        self.assertEqual(seen["state"], "seen")
        taught = await self.command(
            "finding_feedback", finding_id=item["id"], verdict="not-material"
        )
        self.assertEqual(taught["state"], "dismissed")
        self.assertIn("schedules", [e["event"] for e in self.events])
        exported = await self.command("export_bot", task_id=bot["id"])
        exported["model"] = "openrouter:openai/gpt-5"
        exported["cloud"] = True
        copied = await self.command("import_bot", config=exported)
        self.assertFalse(copied["cloud"])
        self.assertIsNone(copied["model"])
        self.assertFalse(copied["enabled"])
        reset = await self.command("reset_bot", task_id=bot["id"])
        self.assertEqual(reset["findings"], [])
        await self.command("delete_bot", task_id=copied["id"])
        await self.command("delete_bot", task_id=bot["id"])
        self.assertEqual((await self.command("bots"))["bots"], [])


if __name__ == "__main__":
    unittest.main()
