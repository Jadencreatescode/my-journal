"""The scriptable `hermes journal` command tree."""

from __future__ import annotations

import argparse
import json

from .tools import (
    handle_find_gaps,
    handle_plan_backfill,
    handle_resolve_range,
    handle_status,
)
from .operations import maintenance, preview, purge, run_backfill, run_generation, schedule_create, schedule_remove


def register_cli(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="journal_command")
    subs.add_parser("status", help="Show journal entry coverage and gaps")
    gaps = subs.add_parser("gaps", help="List missing journal dates")
    gaps.add_argument("range", nargs="?", default="")
    plan = subs.add_parser("backfill-plan", help="Plan a historical backfill without writing")
    plan.add_argument("range")
    resolve = subs.add_parser("resolve-range", help="Resolve a natural date range")
    resolve.add_argument("range")
    generate = subs.add_parser("generate", help="Generate and validate one journal date")
    generate.add_argument("date")
    backfill = subs.add_parser("backfill", help="Execute a complete historical backfill")
    backfill.add_argument("range")
    preview_parser = subs.add_parser("preview", help="Preview dates without writing")
    preview_parser.add_argument("range")
    cron_setup = subs.add_parser("cron-setup", help="Create the daily Hermes journal job")
    cron_setup.add_argument("--schedule", default="0 11 * * *")
    cron_setup.add_argument("--deliver", default="local")
    subs.add_parser("cron-remove", help="Remove the daily Hermes journal job")
    subs.add_parser("maintenance", help="Inspect gaps and pending runs")
    purge_parser = subs.add_parser("purge", help="Delete journal data and preserve configuration")
    purge_parser.add_argument("--apply", action="store_true")
    purge_parser.add_argument("--confirm", default="")
    parser.set_defaults(func=journal_command)


def journal_command(args: argparse.Namespace) -> int:
    command = getattr(args, "journal_command", None)
    if command == "status":
        output = handle_status({})
    elif command == "gaps":
        output = handle_find_gaps({"range": getattr(args, "range", "")})
    elif command == "backfill-plan":
        output = handle_plan_backfill({"range": args.range})
    elif command == "resolve-range":
        output = handle_resolve_range({"range": args.range})
    elif command == "generate":
        output = json.dumps(
            run_generation(
                f"Generate journal date {args.date}.", expected_dates=[args.date]
            )
        )
    elif command == "backfill":
        output = json.dumps(run_backfill(args.range))
    elif command == "preview":
        output = json.dumps(preview(args.range))
    elif command == "cron-setup":
        output = json.dumps(schedule_create(args.schedule, args.deliver))
    elif command == "cron-remove":
        output = json.dumps(schedule_remove())
    elif command == "maintenance":
        output = json.dumps(maintenance())
    elif command == "purge":
        try:
            output = json.dumps(purge(args.confirm, apply=args.apply))
        except Exception as exc:
            output = json.dumps({"error": str(exc)})
    else:
        print("usage: hermes journal COMMAND")
        raise SystemExit(2)
    parsed = json.loads(output)
    print(json.dumps(parsed, ensure_ascii=False, indent=2))
    if parsed.get("error") or parsed.get("ok") is False:
        raise SystemExit(1)
    return 0
