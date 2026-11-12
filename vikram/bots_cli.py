"""The terminal's Home page and bot commands: the same store the app shows."""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from vikram import schedules
from vikram.bots import BotStore, importable, templates
from vikram.schedules import ScheduleStore, local_zone, repair_schedule

SNOOZE = {"day": 1, "week": 7, "month": 30}


def _when(stamp: float | None) -> str:
    if stamp is None:
        return "—"
    return datetime.fromtimestamp(stamp, local_zone()).strftime("%a %-d %b %H:%M")


def _finding_line(finding: dict) -> str:
    repeat = f" ×{finding['count']}" if finding["count"] > 1 else ""
    state = "" if finding["state"] == "new" else f" [{finding['state']}]"
    return (
        f"  {finding['id'][:8]}  {finding['bot_name']}: {finding['title']}{repeat}"
        f"{state}  ({finding['kind']}, {_when(finding['last_seen'])})"
    )


def _bot_line(bot: dict) -> str:
    last = bot.get("last_run")
    last_text = (
        f"last {last.get('outcome') or last['status']} {_when(last['started'])}"
        if last
        else "never ran"
    )
    state = {"paused": "paused", "done": "done"}.get(
        bot["state"], f"next {_when(bot['next_run'])}"
    )
    overdue = "  OVERDUE" if bot["overdue"] else ""
    return (
        f"  {bot['id'][:8]}  {bot['name']} · {bot['description']} · {state} · "
        f"{last_text} · open: {bot['attention']} attention, {bot['material']} "
        f"material, {bot['info']} info, {bot['held']} held{overdue}"
    )


def render_home(home: dict) -> str:
    lines = ["Needs you:"]
    for bot in home["stale"]:
        lines.append(
            f"  —         {bot['name']} has not completed a run in "
            f"{bot['expected_days']} days."
        )
    lines.extend(_finding_line(f) for f in home["needs_you"])
    if not home["needs_you"] and not home["stale"]:
        lines.append("  Nothing.")
    lines.append("Material since you last looked:")
    lines.extend(_finding_line(f) for f in home["material"])
    if not home["material"]:
        lines.append("  Nothing new.")
    lines.append("Bots:")
    lines.extend(_bot_line(b) for b in home["bots"])
    if not home["bots"]:
        lines.append("  No bots yet. Create one with `vikram bots create`.")
    return "\n".join(lines)


def _open() -> tuple[ScheduleStore, BotStore]:
    store = ScheduleStore(schedules.data_root())
    return store, BotStore(store)


def _after_change(store: ScheduleStore) -> None:
    schedules.ensure_agent(store, schedules.installed_agent(store.root))


def home_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="vikram home",
        description="What needs you, what changed, and how each bot is doing.",
    )
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)
    store, bots = _open()
    try:
        home = bots.home()
        print(json.dumps(home, indent=2) if args.json else render_home(home))
        return 0
    finally:
        store.close()


def _schedule_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repeat", help="daily, weekly, monthly, interval, or once (default weekly)"
    )
    parser.add_argument("--time", help="HH:MM in this Mac's local time")
    parser.add_argument("--days", help="for weekly: mon,wed or weekdays")
    parser.add_argument("--day-of-month", type=int)
    parser.add_argument("--every-minutes", type=int)
    parser.add_argument("--date", help="for once: YYYY-MM-DD")


def _schedule_fields(args: argparse.Namespace) -> dict | None:
    raw = {
        "repeat": args.repeat,
        "time": args.time,
        "days": args.days,
        "day_of_month": args.day_of_month,
        "every_minutes": args.every_minutes,
        "date": args.date,
    }
    given = {key: value for key, value in raw.items() if value is not None}
    if not given:
        return None
    given.setdefault("repeat", "weekly")
    return repair_schedule(given)


def bots_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="vikram bots",
        description=(
            "Bots own an ongoing responsibility and report only what needs you. "
            "Runs are unattended: web search and the offline Python sandbox, no "
            "browser, no host tools."
        ),
    )
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("list", help="list bots (the default)")
    sub.add_parser("templates", help="list templates and whether they are available")
    create = sub.add_parser("create", help="create a bot from a template")
    create.add_argument("name")
    create.add_argument("--template", default="topic-watcher")
    create.add_argument("--mandate", help="the outcome, in your words")
    create.add_argument("--charter", help="standing instructions")
    create.add_argument("--source", action="append", default=[], help="repeatable")
    create.add_argument("--model", help="a local model; omit to inherit the default")
    create.add_argument(
        "--allow-openrouter",
        action="store_true",
        help="allow this bot to use an OpenRouter model (explicit opt-in)",
    )
    create.add_argument("--rubric", help="what warrants attention")
    create.add_argument("--keyword", action="append", default=[], help="gate")
    create.add_argument("--budget-minutes", type=int)
    create.add_argument("--max-requests", type=int, help="model requests, at most 10")
    create.add_argument(
        "--max-tool-calls", type=int, help="successful tools, at most 20"
    )
    create.add_argument("--expected-days", type=int, help="liveness window")
    create.add_argument("--notify", choices=("attention", "findings", "never"))
    _schedule_arguments(create)
    show = sub.add_parser("show", help="a bot's config, findings, and runs")
    show.add_argument("bot")
    show.add_argument("--json", action="store_true")
    for name, text in (
        ("run", "run a bot now, in this terminal (other due tasks run too)"),
        ("pause", "pause a bot"),
        ("resume", "resume a bot"),
        ("delete", "delete a bot and its findings"),
        ("reset", "forget what a bot has reported (findings become resolved)"),
        ("export", "print a bot's config as JSON"),
    ):
        sub.add_parser(name, help=text).add_argument("bot")
    imp = sub.add_parser("import", help="create a bot from an exported JSON file")
    imp.add_argument("file")
    edit = sub.add_parser("edit", help="update a bot from a JSON config file")
    edit.add_argument("bot")
    edit.add_argument("file")
    edit.add_argument(
        "--allow-openrouter", action="store_true", help="explicit cloud opt-in"
    )
    args = parser.parse_args(argv)
    action = args.action or "list"
    store, bots = _open()
    try:
        if action == "list":
            listing = bots.bots()
            print("\n".join(_bot_line(b) for b in listing) or "No bots yet.")
            return 0
        if action == "templates":
            for item in templates():
                mark = (
                    "available" if item["available"] else f"blocked: {item['reason']}"
                )
                print(
                    f"  {item['id']:<20} {item['title']} — {item['summary']} [{mark}]"
                )
            return 0
        if action == "create":
            fields = {
                "template": args.template,
                "name": args.name,
                "mandate": args.mandate,
                "charter": args.charter,
                "sources": args.source or None,
                "model": args.model,
                "cloud": True if args.allow_openrouter else None,
                "budget_minutes": args.budget_minutes,
                "max_requests": args.max_requests,
                "max_tool_calls": args.max_tool_calls,
                "expected_days": args.expected_days,
                "notify": args.notify,
                "schedule": _schedule_fields(args),
            }
            if args.rubric or args.keyword:
                fields["materiality"] = {
                    k: v
                    for k, v in {
                        "rubric": args.rubric,
                        "keywords": args.keyword or None,
                    }.items()
                    if v is not None
                }
            bot = bots.create(fields, origin="cli")
            _after_change(store)
            print(
                f"Created “{bot['name']}” ({bot['description']}); id {bot['id'][:8]}."
            )
            return 0
        if action == "import":
            config = _load_config(args.file)
            bot = bots.create(importable(config), origin="cli", enabled=False)
            _after_change(store)
            print(
                f"Imported “{bot['name']}” ({bot['description']}); id {bot['id'][:8]}."
                " The bot is paused. Cloud permission is never imported; review "
                "its configuration before resuming it."
            )
            return 0
        bot = bots.find(args.bot)
        if action == "edit":
            config = _load_config(args.file)
            keep_cloud = config.get("cloud", bot["cloud"]) is True and bot["cloud"]
            config["cloud"] = args.allow_openrouter or keep_cloud
            changed = bots.update(bot["id"], config)
            _after_change(store)
            print(f"Updated “{changed['name']}”.")
            return 0
        if action == "show":
            detail = bots.bot(bot["id"])
            if args.json:
                print(json.dumps(detail, indent=2))
                return 0
            print(_bot_line(detail))
            print(f"  Mandate: {detail['mandate']}")
            if detail.get("last_summary"):
                print(f"  Last report: {detail['last_summary']}")
            print("  Findings:")
            for finding in detail["findings"]:
                held = " (held)" if finding["held"] else ""
                print(_finding_line(finding) + held)
            if not detail["findings"]:
                print("    none open")
            print("  Runs:")
            for run in detail["runs"][:10]:
                print(
                    f"    {_when(run['started'])}  {run['status']}"
                    + (f" · {run['outcome']}" if run.get("outcome") else "")
                    + (f" · {run['report']}" if run.get("report") else "")
                )
            return 0
        if action == "export":
            print(json.dumps(bots.export(bot["id"]), indent=2))
            return 0
        if action in ("pause", "resume"):
            bots.update(bot["id"], {"enabled": action == "resume"})
            _after_change(store)
            print(f"{'Paused' if action == 'pause' else 'Resumed'} “{bot['name']}”.")
            return 0
        if action == "delete":
            bots.delete(bot["id"])
            _after_change(store)
            print(f"Deleted “{bot['name']}”.")
            return 0
        if action == "reset":
            bots.reset(bot["id"])
            print(f"Reset “{bot['name']}”; its open findings are now resolved.")
            return 0
        if action == "run":
            bots.request_run(bot["id"])
            with schedules.runner_lock(store.root) as lock:
                if lock is None:
                    print("Another run is in progress; this one starts after it.")
                    return 1
                count = asyncio.run(schedules.run_due(store))
            detail = bots.bot(bot["id"])
            last = detail["runs"][0] if detail["runs"] else None
            print(
                f"Ran {count} {'task' if count == 1 else 'tasks'}. “{bot['name']}”: "
                + (
                    f"{last['status']} · {last.get('outcome') or '—'}"
                    + (f" · {last['report']}" if last and last.get("report") else "")
                    if last
                    else "no run recorded"
                )
            )
            return 0
        parser.error("Unknown action.")
        return 2
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        store.close()


def findings_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="vikram findings", description="Open findings across bots."
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--bot", help="only this bot")
    parser.add_argument(
        "--all", action="store_true", help="include resolved and dismissed"
    )
    sub = parser.add_subparsers(dest="action")
    for name, text in (
        ("seen", "mark a finding seen"),
        ("resolve", "resolve a finding"),
        ("dismiss", "dismiss a finding; it stays quiet until the fact changes"),
        ("reopen", "mark a finding new again"),
        ("material", "teach the bot this was material"),
        ("not-material", "teach the bot this was not material (and dismiss it)"),
    ):
        sub.add_parser(name, help=text).add_argument("finding")
    snooze = sub.add_parser("snooze", help="snooze for a day, week, or month")
    snooze.add_argument("finding")
    snooze.add_argument("period", choices=tuple(SNOOZE), nargs="?", default="week")
    args = parser.parse_args(argv)
    store, bots = _open()
    try:
        if args.action:
            finding = _find_finding(bots, args.finding)
            if args.action in ("material", "not-material"):
                result = bots.feedback(finding["id"], args.action)
            elif args.action == "snooze":
                until = (
                    store.clock() + timedelta(days=SNOOZE[args.period]).total_seconds()
                )
                result = bots.set_finding(finding["id"], "snoozed", until=until)
            else:
                state = {"resolve": "resolved", "dismiss": "dismissed", "reopen": "new"}
                result = bots.set_finding(finding["id"], state.get(args.action, "seen"))
            print(f"{result['title']}: {result['state']}.")
            return 0
        task_id = bots.find(args.bot)["id"] if args.bot else None
        states = (
            ("new", "seen", "snoozed", "resolved", "dismissed")
            if args.all
            else ("new", "seen", "snoozed")
        )
        listing = bots.findings(task_id=task_id, states=states)
        if args.json:
            print(json.dumps(listing, indent=2))
        else:
            print("\n".join(_finding_line(f) for f in listing) or "No open findings.")
        return 0
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        store.close()


def _load_config(filename: str) -> dict:
    try:
        with Path(filename).open("rb") as stream:
            data = stream.read(64 * 1024 + 1)
        if len(data) > 64 * 1024:
            raise ValueError("Bot configurations must be smaller than 64 KiB.")
        config = json.loads(data)
    except OSError:
        raise ValueError("Could not read the bot configuration.") from None
    if not isinstance(config, dict):
        raise ValueError("The file does not hold a bot config.")
    return config


def _find_finding(bots: BotStore, ref: str) -> dict:
    ref = ref.strip().lower()
    matches = [
        f
        for f in bots.findings(
            states=("new", "seen", "snoozed", "resolved", "dismissed")
        )
        if f["id"] == ref or (len(ref) >= 6 and f["id"].startswith(ref))
    ]
    if len(matches) != 1:
        raise ValueError("No single finding matches that id.")
    return matches[0]
