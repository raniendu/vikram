"""Scheduled tasks: prompts that Vikram runs on a schedule, even with the app closed.

Tasks and their runs live in `schedules.sqlite3` beside the chat library. The
chat library stays owned by one open app or CLI, so background runs never touch
it; the app shows runs from this file and can copy one into a chat.

One per-user LaunchAgent runs `/bin/sh` every minute. The shell compares the
clock with a small "next due" file and starts the installed runtime (through the
install's `current` link, so updates need no change) only when a task is due.
The agent is installed while an enabled task exists and removed with the last
one, or by itself once the installation is gone.

Runs are unattended: questions are declined, plans and approvals refused, and
the browser is not offered (`Unattended`, no engine). They may search the web
and use the Python sandbox like a chat reply. Nothing here logs prompts or
answers; the agent's output goes to /dev/null.
"""

import asyncio
import calendar
import contextlib
import fcntl
import json
import os
import plistlib
import re
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

LABEL = "dev.raniendu.vikram.scheduler"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAYS = DAYS[:5]
# A run more than this late (the Mac was asleep or off) is recorded as missed.
CATCH_UP = 12 * 3600
MAX_TASKS = 50
KEEP_RUNS = 50
MIN_INTERVAL, MAX_INTERVAL = 15, 7 * 24 * 60
MAX_NAME, MAX_PROMPT = 80, 4000
# Checkpoint a running answer at most this often; the app reads it live.
CHECKPOINT_SECONDS = 1.0
TERMINAL = {"complete", "failed", "stopped", "interrupted", "missed"}

Repeat = Literal["once", "daily", "weekly", "monthly", "interval"]
Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def data_root() -> Path:
    """The chat library's folder; tasks live beside the chats."""
    # This override is for isolated developer tests; the renderer cannot set it.
    return Path(
        os.environ.get(
            "VIKRAM_CHAT_DATA_DIR",
            Path.home() / "Library/Application Support/dev.raniendu.vikram",
        )
    )


def local_zone() -> tzinfo:
    """This Mac's time zone, so 08:00 stays 08:00 across daylight saving time."""
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return ZoneInfo(target.split("zoneinfo/", 1)[1])
    except (OSError, ValueError, KeyError):
        pass
    return datetime.now().astimezone().tzinfo


class Schedule(BaseModel):
    """When a task runs, in the Mac's local time.

    once: `date` and `time`; daily: `time`; weekly: `days` and `time`; monthly:
    `day_of_month` (the last day in shorter months) and `time`; interval: every
    `every_minutes`, counted from when the task was created or last changed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    repeat: Repeat
    time: str | None = Field(default=None, pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    days: tuple[Day, ...] | None = None
    day_of_month: int | None = Field(default=None, ge=1, le=31)
    every_minutes: int | None = Field(default=None, ge=MIN_INTERVAL, le=MAX_INTERVAL)

    @model_validator(mode="after")
    def _fields_fit_repeat(self):
        needs = {
            "once": {"date", "time"},
            "daily": {"time"},
            "weekly": {"days", "time"},
            "monthly": {"day_of_month", "time"},
            "interval": {"every_minutes"},
        }[self.repeat]
        given = {
            name
            for name in ("time", "date", "days", "day_of_month", "every_minutes")
            if getattr(self, name) not in (None, ())
        }
        if needs - given:
            missing = ", ".join(sorted(needs - given))
            raise ValueError(f"A {self.repeat} schedule needs {missing}.")
        if given - needs:
            extra = ", ".join(sorted(given - needs))
            raise ValueError(f"A {self.repeat} schedule does not use {extra}.")
        if self.date is not None:
            date.fromisoformat(self.date)
        if self.days is not None:
            object.__setattr__(
                self, "days", tuple(d for d in DAYS if d in set(self.days))
            )
        return self


def _clock(value: str) -> str | None:
    """'8am', '8:30 PM', '20.30', '08:00:00', or '8' as 'HH:MM'."""
    text = value.strip().lower().replace(".", ":")
    match = re.fullmatch(
        r"(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*(a\.?m\.?|p\.?m\.?)?", text
    )
    if not match:
        return None
    hour, minute = int(match[1]), int(match[2] or 0)
    if match[3]:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if match[3].startswith("p") else 0)
    return f"{hour:02d}:{minute:02d}" if hour < 24 and minute < 60 else None


def _day(value: str) -> str | None:
    text = value.strip().lower()[:3]
    return text if text in DAYS else None


DAY_GROUPS = {
    "weekdays": WEEKDAYS,
    "weekday": WEEKDAYS,
    "workdays": WEEKDAYS,
    "mon-fri": WEEKDAYS,
    "monday-friday": WEEKDAYS,
    "weekends": ("sat", "sun"),
    "weekend": ("sat", "sun"),
    "sat-sun": ("sat", "sun"),
    "everyday": DAYS,
    "daily": DAYS,
    "all": DAYS,
}


def _days(value) -> list[str] | None:
    """'weekdays', 'Monday and Wednesday', or ['Mon', 'fri'] as day codes."""
    if isinstance(value, str):
        text = value.strip().lower().replace("every day", "everyday")
        value = [part for part in re.split(r"[,\s/&]+|\band\b", text) if part]
    if not isinstance(value, list):
        return None
    days = []
    for item in value:
        if not isinstance(item, str):
            return None
        word = item.strip().lower()
        if word in ("", "every", "each", "on"):
            continue
        if word in DAY_GROUPS:
            days.extend(DAY_GROUPS[word])
            continue
        day = _day(word)
        if day is None:
            return None
        days.append(day)
    return days or None


REPEATS = {
    "once": "once",
    "one time": "once",
    "one-time": "once",
    "onetime": "once",
    "single": "once",
    "none": "once",
    "no": "once",
    "never": "once",
    "daily": "daily",
    "day": "daily",
    "every day": "daily",
    "everyday": "daily",
    "weekly": "weekly",
    "week": "weekly",
    "every week": "weekly",
    "monthly": "monthly",
    "month": "monthly",
    "every month": "monthly",
    "interval": "interval",
    "every": "interval",
    "minutes": "interval",
    "hourly": "interval",
    "hours": "interval",
}


def repair_schedule(args: dict, now: datetime | None = None) -> dict:
    """Coerce common local-model deviations into the schedule fields.

    Small models send '8am', 'weekdays', day names, a datetime in `time`, an
    hour count, or a nested `schedule` object. Strict validation still runs on
    the result; anything that cannot be repaired is passed on unchanged.
    """
    args = dict(args)
    nested = args.pop("schedule", None)
    if isinstance(nested, dict):
        args = {**nested, **args}
    now = now or datetime.now(local_zone())
    repeat = args.get("repeat", args.get("frequency", args.get("recurrence")))
    args.pop("frequency", None)
    args.pop("recurrence", None)
    if isinstance(repeat, str):
        text = " ".join(repeat.strip().lower().replace("_", " ").split())
        if text in ("weekdays", "weekday", "every weekday", "workdays", "mon-fri"):
            repeat = "weekly"
            args.setdefault("days", list(WEEKDAYS))
        elif text in ("weekends", "weekend", "every weekend"):
            repeat = "weekly"
            args.setdefault("days", ["sat", "sun"])
        else:
            repeat = REPEATS.get(text, text)
        if text == "hourly":
            args.setdefault("every_minutes", 60)
        args["repeat"] = repeat
    for key in ("every_hours", "hours", "interval_hours"):
        if key in args:
            hours = args.pop(key)
            if isinstance(hours, (int, float)) and not isinstance(hours, bool):
                args.setdefault("every_minutes", int(hours * 60))
    for key in ("minutes", "interval_minutes", "interval"):
        if key in args:
            args.setdefault("every_minutes", args.pop(key))
    every = args.get("every_minutes")
    if isinstance(every, str) and every.strip().isdigit():
        args["every_minutes"] = int(every)
    when = args.get("time")
    if isinstance(when, str):
        stamp = when.strip().replace(" ", "T", 1)
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            moment = None
        if moment is not None and "T" in stamp:
            if moment.tzinfo is not None:
                moment = moment.astimezone(now.tzinfo)
            args.setdefault("date", moment.date().isoformat())
            args["time"] = moment.strftime("%H:%M")
        elif (clock := _clock(when)) is not None:
            args["time"] = clock
    day = args.get("date")
    if isinstance(day, str):
        text = day.strip().lower()
        if text in ("today", "tomorrow"):
            args["date"] = (now.date() + timedelta(days=text == "tomorrow")).isoformat()
        else:
            try:
                moment = datetime.fromisoformat(day.strip().replace(" ", "T", 1))
                if moment.tzinfo is not None:
                    moment = moment.astimezone(now.tzinfo)
                args["date"] = moment.date().isoformat()
                if "time" not in args and ("T" in day or " " in day.strip()):
                    args["time"] = moment.strftime("%H:%M")
            except ValueError:
                pass
    if "days" in args:
        days = _days(args["days"])
        if days is not None:
            args["days"] = days
    dom = args.get("day_of_month", args.pop("day", None))
    if isinstance(dom, str) and dom.strip().rstrip("stndrdth").isdigit():
        dom = int(dom.strip().rstrip("stndrdth"))
    if dom is not None:
        args["day_of_month"] = dom
    repeat = args.get("repeat")
    if repeat == "daily" and isinstance(args.get("days"), list):
        if set(args["days"]) == set(DAYS):
            args.pop("days")
        else:
            args["repeat"] = "weekly"
    if repeat == "once" and "date" not in args and isinstance(args.get("time"), str):
        # A time alone means its next occurrence: today if still ahead.
        clock = _clock(args["time"])
        if clock is not None:
            hour, minute = map(int, clock.split(":"))
            moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if moment <= now:
                moment += timedelta(days=1)
            args["date"] = moment.date().isoformat()
    # Drop fields this repeat does not use, which small models fill with nulls.
    uses = {
        "once": {"date", "time"},
        "daily": {"time"},
        "weekly": {"days", "time"},
        "monthly": {"day_of_month", "time"},
        "interval": {"every_minutes"},
    }.get(args.get("repeat"))
    if uses is not None:
        for key in ("time", "date", "days", "day_of_month", "every_minutes"):
            if key not in uses and key in args:
                args.pop(key)
    return args


def schedule_from(args: dict) -> Schedule:
    """The schedule part of tool or app arguments; `ValueError` if invalid."""
    fields = {
        key: args[key]
        for key in ("repeat", "time", "date", "days", "day_of_month", "every_minutes")
        if args.get(key) is not None
    }
    try:
        return Schedule.model_validate(fields)
    except ValueError as error:
        raise ValueError(_first_error(error)) from None


def _first_error(error: Exception) -> str:
    errors = getattr(error, "errors", None)
    if callable(errors):
        for item in errors():
            message = str(item.get("msg", "")).removeprefix("Value error, ")
            where = ".".join(str(part) for part in item.get("loc", ()))
            if message:
                return f"{where}: {message}" if where else message
    return "Invalid schedule."


def _at(day: date, clock: str, zone: tzinfo) -> datetime:
    hour, minute = map(int, clock.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)


def next_run(
    schedule: Schedule,
    after: datetime,
    anchor: datetime | None = None,
    zone: tzinfo | None = None,
) -> datetime | None:
    """The first run strictly after `after`, or `None` for a past one-time task.

    `anchor` is where interval schedules count from.
    """
    zone = zone or local_zone()
    after = after.astimezone(zone)
    if schedule.repeat == "once":
        moment = _at(date.fromisoformat(schedule.date), schedule.time, zone)
        return moment if moment > after else None
    if schedule.repeat == "interval":
        step = timedelta(minutes=schedule.every_minutes)
        start = (anchor or after).astimezone(zone)
        if start > after:
            return start
        count = int((after - start) / step) + 1
        return start + count * step
    if schedule.repeat == "monthly":
        year, month = after.year, after.month
        for _ in range(14):
            last = calendar.monthrange(year, month)[1]
            day = date(year, month, min(schedule.day_of_month, last))
            moment = _at(day, schedule.time, zone)
            if moment > after:
                return moment
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return None
    days = set(schedule.days or DAYS)
    for offset in range(9):
        day = after.date() + timedelta(days=offset)
        moment = _at(day, schedule.time, zone)
        if moment > after and DAYS[day.weekday()] in days:
            return moment
    return None


def describe(schedule: Schedule) -> str:
    """One line a person can read: 'Weekdays at 08:00'."""
    if schedule.repeat == "once":
        day = date.fromisoformat(schedule.date)
        return f"Once on {day.strftime('%a %-d %b %Y')} at {schedule.time}"
    if schedule.repeat == "daily":
        return f"Every day at {schedule.time}"
    if schedule.repeat == "weekly":
        days = tuple(schedule.days)
        if days == WEEKDAYS:
            which = "Weekdays"
        elif days == ("sat", "sun"):
            which = "Weekends"
        elif days == DAYS:
            which = "Every day"
        else:
            which = "Every " + ", ".join(d.capitalize() for d in days)
        return f"{which} at {schedule.time}"
    if schedule.repeat == "monthly":
        return f"Monthly on day {schedule.day_of_month} at {schedule.time}"
    minutes = schedule.every_minutes
    if minutes % (24 * 60) == 0:
        count, unit = minutes // (24 * 60), "day"
    elif minutes % 60 == 0:
        count, unit = minutes // 60, "hour"
    else:
        count, unit = minutes, "minute"
    return f"Every {unit}" if count == 1 else f"Every {count} {unit}s"


def _clean(text: str, limit: int) -> str:
    # Names and prompts reach the model and the terminal: keep newlines only.
    text = "".join(c for c in text.replace("\t", " ") if c == "\n" or c.isprintable())
    return text.strip()[:limit]


def _uid() -> str:
    return uuid4().hex


class ScheduleStore:
    """Tasks and runs, shared by the app, the CLI, and background runs.

    Several processes open this file at once, so it uses WAL and short
    transactions. Every change rewrites the "next due" file the agent reads.
    """

    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time):
        self.root, self.clock = root, clock
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "schedules.sqlite3"
        self.db = sqlite3.connect(self.path, timeout=10)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError("Scheduled tasks need a newer version of Vikram.")
        self.db.executescript("""
        BEGIN;
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL,
            schedule TEXT NOT NULL, model TEXT, thinking TEXT NOT NULL DEFAULT 'null',
            enabled INTEGER NOT NULL DEFAULT 1, origin TEXT NOT NULL DEFAULT 'app',
            anchor REAL NOT NULL, next_run REAL, run_requested INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL, updated REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            trigger TEXT NOT NULL, scheduled_for REAL, started REAL NOT NULL,
            finished REAL, status TEXT NOT NULL, prompt TEXT NOT NULL,
            model TEXT, actual_model TEXT, content TEXT NOT NULL DEFAULT '',
            reasoning TEXT NOT NULL DEFAULT '', activity TEXT NOT NULL DEFAULT '[]',
            sources TEXT NOT NULL DEFAULT '[]', searches TEXT NOT NULL DEFAULT '[]',
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS runs_task ON runs(task_id, started);
        PRAGMA user_version=1;
        COMMIT;
        """)
        self.sync()

    def close(self) -> None:
        self.db.close()

    @property
    def due_file(self) -> Path:
        return self.root / "scheduler-next-due"

    def data_version(self) -> int:
        """Changes when another process commits, so the app can refresh."""
        return self.db.execute("PRAGMA data_version").fetchone()[0]

    # Reading

    def _task(self, row: sqlite3.Row) -> dict:
        task = dict(row)
        schedule = Schedule.model_validate_json(task["schedule"])
        task["schedule"] = schedule.model_dump(exclude_none=True)
        task["description"] = describe(schedule)
        task["thinking"] = json.loads(task["thinking"])
        task["enabled"] = bool(task["enabled"])
        task["run_requested"] = bool(task["run_requested"])
        task["state"] = (
            "paused"
            if not task["enabled"]
            else "scheduled" if task["next_run"] is not None else "done"
        )
        return task

    def tasks(self) -> list[dict]:
        tasks = [
            self._task(row)
            for row in self.db.execute("SELECT * FROM tasks ORDER BY created, id")
        ]
        last = {
            row["task_id"]: dict(row)
            for row in self.db.execute(
                "SELECT task_id,id,status,started,finished FROM runs r WHERE started="
                "(SELECT max(started) FROM runs WHERE task_id=r.task_id)"
            )
        }
        for task in tasks:
            task["last_run"] = last.get(task["id"])
        return tasks

    def task(self, task_id: str) -> dict:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("That scheduled task no longer exists.")
        task = self._task(row)
        task["runs"] = [
            dict(r)
            for r in self.db.execute(
                "SELECT id,trigger,scheduled_for,started,finished,status,error,"
                "actual_model,model,substr(content,1,200) AS preview FROM runs "
                "WHERE task_id=? ORDER BY started DESC, rowid DESC",
                (task_id,),
            )
        ]
        return task

    def run(self, run_id: str) -> dict:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("That run is no longer available.")
        run = dict(row)
        for key in ("activity", "sources", "searches"):
            run[key] = json.loads(run[key])
        return run

    def find(self, ref: str) -> dict:
        """A task by ID, ID prefix, or name (case-insensitive), for the model."""
        ref = " ".join(str(ref).split()).strip("“”\"'")
        tasks = self.tasks()
        for test in (
            lambda t: t["id"] == ref.lower(),
            lambda t: t["name"].casefold() == ref.casefold(),
            lambda t: len(ref) >= 6 and t["id"].startswith(ref.lower()),
            lambda t: ref.casefold() in t["name"].casefold(),
        ):
            found = [t for t in tasks if test(t)]
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise ValueError(
                    "Several scheduled tasks match. Use list_scheduled_tasks and "
                    "pass the task's id."
                )
        raise ValueError(
            "No scheduled task matches. Use list_scheduled_tasks to see them."
        )

    # Changing

    def _next(self, schedule: Schedule, anchor: float, after: float) -> float | None:
        moment = next_run(
            schedule,
            datetime.fromtimestamp(after, local_zone()),
            anchor=datetime.fromtimestamp(anchor, local_zone()),
        )
        return moment.timestamp() if moment is not None else None

    def create(
        self,
        *,
        name: str,
        prompt: str,
        schedule: Schedule,
        model: str | None,
        thinking=None,
        origin: str = "app",
    ) -> dict:
        name, prompt = _clean(name, MAX_NAME), _clean(prompt, MAX_PROMPT)
        if not name or not prompt:
            raise ValueError("A scheduled task needs a name and a prompt.")
        count = self.db.execute("SELECT count(*) FROM tasks").fetchone()[0]
        if count >= MAX_TASKS:
            raise ValueError(
                f"There are already {MAX_TASKS} scheduled tasks. Delete one first."
            )
        task_id, now = _uid(), self.clock()
        anchor = now
        if schedule.repeat == "interval":
            # The first interval run happens one interval from now.
            anchor = now + schedule.every_minutes * 60
        next_at = self._next(schedule, anchor, now)
        if next_at is None:
            raise ValueError("That time has already passed. Choose a future time.")
        with self.db:
            self.db.execute(
                "INSERT INTO tasks(id,name,prompt,schedule,model,thinking,enabled,"
                "origin,anchor,next_run,created,updated) VALUES(?,?,?,?,?,?,1,?,?,?,?,?)",
                (
                    task_id,
                    name,
                    prompt,
                    schedule.model_dump_json(exclude_none=True),
                    model,
                    json.dumps(thinking),
                    origin,
                    anchor,
                    next_at,
                    now,
                    now,
                ),
            )
        self.sync()
        return self.task(task_id)

    def update(self, task_id: str, **fields: Any) -> dict:
        """Change name, prompt, schedule, model, thinking, or enabled."""
        task = self.task(task_id)
        if not fields or set(fields) - {
            "name",
            "prompt",
            "schedule",
            "model",
            "thinking",
            "enabled",
        }:
            raise ValueError("Unsupported scheduled task change.")
        now = self.clock()
        values: dict[str, Any] = {}
        if "name" in fields:
            values["name"] = _clean(str(fields["name"]), MAX_NAME)
            if not values["name"]:
                raise ValueError("Enter a name for the scheduled task.")
        if "prompt" in fields:
            values["prompt"] = _clean(str(fields["prompt"]), MAX_PROMPT)
            if not values["prompt"]:
                raise ValueError("Enter what Vikram should do.")
        if "model" in fields:
            values["model"] = fields["model"]
        if "thinking" in fields:
            values["thinking"] = json.dumps(fields["thinking"])
        schedule = Schedule.model_validate(task["schedule"])
        anchor = task["anchor"]
        if "schedule" in fields:
            schedule = fields["schedule"]
            anchor = now + (
                schedule.every_minutes * 60 if schedule.repeat == "interval" else 0
            )
            values["schedule"] = schedule.model_dump_json(exclude_none=True)
            values["anchor"] = anchor
        enabled = bool(fields.get("enabled", task["enabled"]))
        values["enabled"] = int(enabled)
        if "schedule" in fields or ("enabled" in fields and enabled):
            next_at = self._next(schedule, anchor, now)
            if next_at is None and "schedule" in fields:
                raise ValueError("That time has already passed. Choose a future time.")
            values["next_run"] = next_at
        values["updated"] = now
        with self.db:
            self.db.execute(
                f"UPDATE tasks SET {','.join(f'{key}=?' for key in values)} WHERE id=?",
                (*values.values(), task_id),
            )
        self.sync()
        return self.task(task_id)

    def delete(self, task_id: str) -> None:
        self.task(task_id)
        if self.db.execute(
            "SELECT 1 FROM runs WHERE task_id=? AND status='running'", (task_id,)
        ).fetchone():
            raise ValueError("Wait for this task's run to finish before deleting it.")
        with self.db:
            self.db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        self.sync()

    def request_run(self, task_id: str) -> dict:
        """Run now, in the background runner, without changing the schedule."""
        self.task(task_id)
        with self.db:
            self.db.execute("UPDATE tasks SET run_requested=1 WHERE id=?", (task_id,))
        self.sync()
        return self.task(task_id)

    def sync(self) -> None:
        """Write the epoch second when the agent must next start the runtime.

        A missing file means nothing is scheduled; `0` means run now.
        """
        row = self.db.execute(
            "SELECT max(run_requested) AS asked, min(CASE WHEN enabled THEN next_run "
            "END) AS soonest FROM tasks"
        ).fetchone()
        if row["asked"]:
            due = 0
        elif row["soonest"] is not None:
            due = int(row["soonest"])
        else:
            self.due_file.unlink(missing_ok=True)
            return
        temporary = self.due_file.with_name(f".{self.due_file.name}-{_uid()}")
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(f"{due}\n")
            os.replace(temporary, self.due_file)
        finally:
            temporary.unlink(missing_ok=True)

    def active(self) -> bool:
        """Whether the background agent is needed: any enabled task."""
        return bool(
            self.db.execute("SELECT 1 FROM tasks WHERE enabled=1 LIMIT 1").fetchone()
        )

    # Running

    def interrupt_stale(self) -> None:
        """Runs left 'running' by a runner that was killed; call with the lock."""
        with self.db:
            self.db.execute(
                "UPDATE runs SET status='interrupted',finished=?,error="
                "'The run stopped before it finished.' WHERE status='running'",
                (self.clock(),),
            )

    def claim(self) -> tuple[dict, dict] | None:
        """Start the next due run: missed ones are recorded and skipped.

        A task that missed several times while the Mac slept runs once.
        """
        while True:
            now = self.clock()
            row = self.db.execute(
                "SELECT * FROM tasks WHERE run_requested=1 OR (enabled=1 AND "
                "next_run IS NOT NULL AND next_run<=?) ORDER BY run_requested DESC, "
                "next_run, id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            task = self._task(row)
            schedule = Schedule.model_validate(task["schedule"])
            due = task["enabled"] and task["next_run"] is not None
            due = due and task["next_run"] <= now
            next_at = (
                self._next(schedule, task["anchor"], now) if due else task["next_run"]
            )
            manual = task["run_requested"]
            missed = due and not manual and now - task["next_run"] > CATCH_UP
            run_id = _uid()
            with self.db:
                self.db.execute(
                    "UPDATE tasks SET run_requested=0,next_run=? WHERE id=?",
                    (next_at, task["id"]),
                )
                self.db.execute(
                    "INSERT INTO runs(id,task_id,trigger,scheduled_for,started,"
                    "finished,status,prompt,model,error) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        task["id"],
                        (
                            "manual"
                            if manual
                            else (
                                "catch_up"
                                if now - task["next_run"] > 120
                                else "schedule"
                            )
                        ),
                        task["next_run"] if due else None,
                        now,
                        now if missed else None,
                        "missed" if missed else "running",
                        task["prompt"],
                        task["model"],
                        (
                            "The Mac was asleep or off at the scheduled time."
                            if missed
                            else None
                        ),
                    ),
                )
                self._prune(task["id"])
            self.sync()
            if not missed:
                return task, self.run(run_id)

    def _prune(self, task_id: str) -> None:
        self.db.execute(
            "DELETE FROM runs WHERE task_id=? AND status!='running' AND id NOT IN "
            "(SELECT id FROM runs WHERE task_id=? ORDER BY started DESC LIMIT ?)",
            (task_id, task_id, KEEP_RUNS),
        )

    def checkpoint(self, run_id: str, **fields: Any) -> None:
        values = {
            key: (
                json.dumps(value)
                if key in ("activity", "sources", "searches")
                else value
            )
            for key, value in fields.items()
            if key
            in (
                "content",
                "reasoning",
                "activity",
                "sources",
                "searches",
                "actual_model",
            )
            and value is not None
        }
        if not values:
            return
        with self.db:
            self.db.execute(
                f"UPDATE runs SET {','.join(f'{key}=?' for key in values)} "
                "WHERE id=? AND status='running'",
                (*values.values(), run_id),
            )

    def finish(self, run_id: str, status: str, error: str | None = None) -> None:
        if status not in TERMINAL:
            raise ValueError("Invalid run state.")
        with self.db:
            self.db.execute(
                "UPDATE runs SET status=?,error=?,finished=? WHERE id=? AND "
                "status='running'",
                (status, error, self.clock(), run_id),
            )


# The background agent


@dataclass(frozen=True)
class Agent:
    """Where the LaunchAgent lives and what it starts."""

    home: Path
    install_root: Path
    runtime: Path
    due_file: Path

    @property
    def plist(self) -> Path:
        return self.home / "Library/LaunchAgents" / f"{LABEL}.plist"


# Arguments: $1 due file, $2 runtime, $3 install root, $4 this plist, $5 label.
SCRIPT = """\
if [ ! -d "$3" ]; then
  rm -f "$4"
  launchctl bootout "gui/$(id -u)/$5" >/dev/null 2>&1
  exit 0
fi
next=$(cat "$1" 2>/dev/null) || exit 0
case "$next" in ''|*[!0-9]*) exit 0 ;; esac
[ "$(date +%s)" -ge "$next" ] || exit 0
[ -x "$2" ] || exit 0
exec "$2" --run-schedules
"""


def agent_plist(agent: Agent) -> bytes:
    """Every minute, a shell checks the due file; Python starts only when due."""
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [
                "/bin/sh",
                "-c",
                SCRIPT,
                "vikram-scheduler",
                str(agent.due_file),
                str(agent.runtime),
                str(agent.install_root),
                str(agent.plist),
                LABEL,
            ],
            "StartInterval": 60,
            "RunAtLoad": True,
            "ProcessType": "Background",
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": "/dev/null",
        },
        sort_keys=True,
    )


def installed_agent(root: Path | None = None) -> Agent | None:
    """The agent for this installed runtime; `None` in development or tests.

    Only the installed app (frozen, in the managed install folder or the app in
    ~/Applications) installs the agent, and never for an overridden test chat
    library.
    """
    from vikram.installation import installed_runtime, runtime_layout

    if not getattr(sys, "frozen", False) or "VIKRAM_CHAT_DATA_DIR" in os.environ:
        return None
    layout = runtime_layout()
    if not installed_runtime(layout):
        return None
    return agent_for(layout.home, root or data_root())


def agent_for(home: Path, root: Path) -> Agent:
    from vikram.installation import Layout

    layout = Layout(home)
    return Agent(
        home=layout.home,
        install_root=layout.root,
        runtime=layout.current / "Vikram.app/Contents/MacOS/vikram-sidecar",
        due_file=root / "scheduler-next-due",
    )


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/launchctl", *args], capture_output=True, text=True, timeout=20
    )


def _domain() -> str:
    return f"gui/{os.getuid()}"


def agent_loaded(run=launchctl) -> bool:
    try:
        return run("print", f"{_domain()}/{LABEL}").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_agent(store: ScheduleStore, agent: Agent | None, run=launchctl) -> str:
    """Install, refresh, or remove the agent to match the tasks.

    Returns the background state the app shows: "on", "off" (no enabled task),
    "unavailable" (development or test runtime), or "failed".
    """
    if agent is None:
        return "unavailable"
    try:
        if not store.active():
            remove_agent(agent, run)
            return "off"
        content = agent_plist(agent)
        changed = not agent.plist.is_file() or agent.plist.read_bytes() != content
        if changed:
            agent.plist.parent.mkdir(parents=True, exist_ok=True)
            temporary = agent.plist.with_name(f".{agent.plist.name}-{_uid()}")
            try:
                temporary.write_bytes(content)
                temporary.chmod(0o644)
                os.replace(temporary, agent.plist)
            finally:
                temporary.unlink(missing_ok=True)
        loaded = agent_loaded(run)
        if changed and loaded:
            run("bootout", f"{_domain()}/{LABEL}")
            loaded = False
        if not loaded:
            result = run("bootstrap", _domain(), str(agent.plist))
            if result.returncode != 0 and not agent_loaded(run):
                return "failed"
        return "on"
    except (OSError, subprocess.SubprocessError):
        return "failed"


def remove_agent(agent: Agent, run=launchctl) -> None:
    if agent_loaded(run):
        run("bootout", f"{_domain()}/{LABEL}")
    agent.plist.unlink(missing_ok=True)


def refresh_installed_agent(home: Path, run=launchctl) -> None:
    """After an install or update: rewrite an existing agent for this version.

    The agent already starts the new version through `current`; this keeps its
    definition current too. Nothing is installed if there was no agent.
    """
    agent = agent_for(
        home, Path(home) / "Library/Application Support/dev.raniendu.vikram"
    )
    if not agent.plist.is_file():
        return
    content = agent_plist(agent)
    if agent.plist.read_bytes() == content:
        return
    agent.plist.write_bytes(content)
    if Path(home).resolve() == Path.home().resolve() and agent_loaded(run):
        run("bootout", f"{_domain()}/{LABEL}")
        run("bootstrap", _domain(), str(agent.plist))


# Model access for chat replies


@dataclass
class TaskBook:
    """Scheduled tasks as one chat reply sees them: the store (or how to open it
    on first use), the chat's model for new tasks, and what to do after a change
    (refresh the app, match the agent)."""

    store: ScheduleStore | None = None
    model: str | None = None
    thinking: Any = None
    origin: str = "chat"
    changed: Callable[[], None] = field(default=lambda: None)
    opener: Callable[[], ScheduleStore] | None = None

    def open(self) -> ScheduleStore:
        if self.store is None:
            if self.opener is None:
                raise ValueError("Scheduled tasks are unavailable.")
            self.store = self.opener()
        return self.store


# Background runs


def notify(name: str, status: str) -> None:
    """A macOS notification with the task name only (the owner's choice).

    Isolated test libraries (`VIKRAM_CHAT_DATA_DIR`) never notify.
    """
    if "VIKRAM_CHAT_DATA_DIR" in os.environ:
        return
    message = f"{name} is ready." if status == "complete" else f"{name} did not finish."
    script = (
        "on run argv\n"
        'display notification (item 1 of argv) with title "Vikram"\n'
        "end run"
    )
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["/usr/bin/osascript", "-e", script, message[:200]],
            capture_output=True,
            timeout=15,
        )


def scheduled_context(task: dict) -> str:
    return (
        f"“{task['name']}” ({task['description'][0].lower()}{task['description'][1:]})"
    )


async def execute(
    store: ScheduleStore,
    task: dict,
    run: dict,
    *,
    streamer=None,
    catalog=None,
    alert: Callable[[str, str], None] = notify,
) -> str:
    """Run one claimed task like a chat reply nobody attends; returns its status."""
    from vikram.conversation import stream_reply
    from vikram.errors import reply_error
    from vikram.interaction import Unattended
    from vikram.models import ModelCatalog, prepare_model

    streamer = streamer or stream_reply
    status, error, iterator = "complete", None, None
    content = ""
    try:
        if not task["model"]:
            raise ValueError("Choose a model for this scheduled task.")
        models = await (catalog or ModelCatalog()).refresh()
        selected, _setting, thinking, web, _notice = prepare_model(
            models, task["model"], task["thinking"]
        )
        iterator = streamer(
            selected["name"],
            thinking,
            [],
            [run["prompt"]],
            web=web,
            session_id=run["id"],
            mode="normal",
            interaction=Unattended(),
            browser=None,
            vision=bool(selected.get("vision")),
            scheduled=scheduled_context(task),
        )
        saved = 0.0
        last: dict = {}
        async for chunk in iterator:
            last, content = chunk, chunk["text"]
            if time.monotonic() - saved >= CHECKPOINT_SECONDS:
                saved = time.monotonic()
                store.checkpoint(
                    run["id"],
                    content=content,
                    reasoning=chunk.get("reasoning"),
                    activity=chunk.get("activity"),
                    sources=chunk.get("sources"),
                    searches=chunk.get("searches"),
                    actual_model=chunk.get("actual_model"),
                )
        if last:
            store.checkpoint(
                run["id"],
                content=content,
                reasoning=last.get("reasoning"),
                activity=last.get("activity"),
                sources=last.get("sources"),
                searches=last.get("searches"),
                actual_model=last.get("actual_model"),
            )
        if not content:
            status, error = "failed", "The model finished without an answer."
    except asyncio.CancelledError:
        status, error = "stopped", "The run was stopped."
    except ValueError as exc:
        status, error = "failed", str(exc)
    except Exception as exc:
        status, error = "failed", reply_error(exc)
    finally:
        if iterator is not None:
            with contextlib.suppress(Exception):
                await iterator.aclose()
        store.finish(run["id"], status, error)
    if status != "stopped":
        alert(task["name"], status)
    return status


async def run_due(store: ScheduleStore, **options) -> int:
    """Run every due task, one at a time, until none is due; returns the count."""
    count = 0
    store.interrupt_stale()
    while (claimed := store.claim()) is not None:
        task, run = claimed
        status = await execute(store, task, run, **options)
        count += 1
        if status == "stopped":
            break
    store.sync()
    return count


@contextlib.contextmanager
def runner_lock(root: Path):
    """One background runner at a time; `None` if another one holds it."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "scheduler.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield None
            return
        yield lock


def start_runner() -> None:
    """Start a detached background runner now, for "Run now".

    It outlives the app; if a runner is already working, this one exits and
    that runner picks the request up.
    """
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--run-schedules"]
    else:
        command = [sys.executable, "-m", "vikram.schedules"]
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
    )


def main() -> int:
    """The agent's entry point: run what is due, then exit."""
    import logging

    os.umask(0o077)
    logging.disable(logging.CRITICAL)
    root = data_root()
    with runner_lock(root) as lock:
        if lock is None:
            return 0
        store = ScheduleStore(root)
        try:

            async def work():
                task = asyncio.current_task()
                loop = asyncio.get_running_loop()
                # launchd stops the agent with SIGTERM (log out, shutdown, removal).
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(sig, task.cancel)
                with contextlib.suppress(asyncio.CancelledError):
                    await run_due(store)

            asyncio.run(work())
            # The last enabled task may have been a one-time task that just ran.
            # Removing the agent ends this process too, so it comes last.
            agent = installed_agent(root)
            if agent is not None and not store.active():
                store.close()
                remove_agent(agent)
        finally:
            store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
