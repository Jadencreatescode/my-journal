"""The scriptable `hermes journal` command tree."""

from __future__ import annotations

import argparse
import json

from .tools import (
    handle_daily_workload_approve,
    handle_daily_workload_check,
    handle_find_gaps,
    handle_plan_backfill,
    handle_resolve_range,
    handle_setup_approve,
    handle_setup_database_approve,
    handle_setup_inventory,
    handle_setup_plan,
    handle_status,
)
from .operations import (
    maintenance,
    preview,
    purge,
    reset_failed_pending,
    run_backfill,
    run_generation,
    schedule_create,
    schedule_remove,
)


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
    subs.add_parser("setup-inventory", help="List available profile and platform labels without reading messages")
    setup_database = subs.add_parser("setup-database-approve", help="Approve the exact next database size tier")
    setup_database.add_argument("profile")
    setup_database.add_argument("--confirm", required=True)
    workload_check = subs.add_parser("workload-check", help="Check one date against the approved daily message tier")
    workload_check.add_argument("journal_date")
    workload_approve = subs.add_parser("workload-approve", help="Approve the exact next daily message tier")
    workload_approve.add_argument("journal_date")
    workload_approve.add_argument("--confirm", required=True)
    setup_plan = subs.add_parser("setup-plan", help="Discover eligible retained history and create an approval plan")
    setup_plan.add_argument("--profile", action="append", required=True, dest="profiles")
    setup_plan.add_argument("--platform", action="append", required=True, dest="platforms")
    setup_plan.add_argument("--timezone", required=True)
    setup_plan.add_argument("--exclude-session", action="append", default=[], dest="excluded_session_ids")
    setup_plan.add_argument("--start-date")
    setup_plan.add_argument("--end-date")
    setup_plan.add_argument("--pii-mode", choices=("mask", "preserve"), required=True)
    setup_plan.add_argument("--entropy-mode", choices=("report", "redact", "off"), required=True)
    setup_approve = subs.add_parser("setup-approve", help="Approve and execute one exact guided setup plan")
    setup_approve.add_argument("plan_id")
    setup_approve.add_argument("--confirm", required=True, dest="confirmation")
    preview_parser = subs.add_parser("preview", help="Preview dates without writing")
    preview_parser.add_argument("range")
    cron_setup = subs.add_parser("cron-setup", help="Create the daily Hermes journal job")
    cron_setup.add_argument("--schedule", default="0 11 * * *")
    cron_setup.add_argument("--deliver", default="local")
    subs.add_parser("cron-remove", help="Remove the daily Hermes journal job")
    subs.add_parser("maintenance", help="Inspect gaps and pending runs")
    pending_reset = subs.add_parser("pending-reset", help="Reset one exact failed pending run")
    pending_reset.add_argument("journal_date")
    pending_reset.add_argument("run_id")
    pending_reset.add_argument("--apply", action="store_true")
    pending_reset.add_argument("--confirm", default="")
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
    elif command == "setup-inventory":
        output = handle_setup_inventory({})
    elif command == "setup-database-approve":
        output = handle_setup_database_approve({
            "profile": args.profile,
            "confirmation": args.confirm,
        })
    elif command == "workload-check":
        output = handle_daily_workload_check({"journal_date": args.journal_date})
    elif command == "workload-approve":
        output = handle_daily_workload_approve({
            "journal_date": args.journal_date,
            "confirmation": args.confirm,
        })
    elif command == "setup-plan":
        output = handle_setup_plan({
            "profiles": args.profiles,
            "platforms": args.platforms,
            "timezone": args.timezone,
            "excluded_session_ids": args.excluded_session_ids,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "pii_mode": args.pii_mode,
            "entropy_mode": args.entropy_mode,
        })
    elif command == "setup-approve":
        output = handle_setup_approve({
            "plan_id": args.plan_id,
            "confirmation": args.confirmation,
        })
    elif command == "preview":
        output = json.dumps(preview(args.range))
    elif command == "cron-setup":
        output = json.dumps(schedule_create(args.schedule, args.deliver))
    elif command == "cron-remove":
        output = json.dumps(schedule_remove())
    elif command == "maintenance":
        output = json.dumps(maintenance())
    elif command == "pending-reset":
        try:
            output = json.dumps(
                reset_failed_pending(
                    args.journal_date,
                    args.run_id,
                    args.confirm,
                    apply=args.apply,
                )
            )
        except Exception as exc:
            output = json.dumps({"error": str(exc)})
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
