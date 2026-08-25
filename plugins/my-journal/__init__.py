"""My Journal plugin registration."""

from __future__ import annotations

import sys


if sys.platform == "win32":
    from .windows_bridge import journal_command, register_cli, register_windows as register
else:
    from .cli import journal_command, register_cli
    from . import onboarding
    from .tools import (
        BACKFILL_SCHEMA,
        DAILY_WORKLOAD_APPROVE_SCHEMA,
        DAILY_WORKLOAD_CHECK_SCHEMA,
        GAPS_SCHEMA,
        GENERATION_COLLECT_SCHEMA,
        GENERATION_COMPLETE_SCHEMA,
        GENERATION_GET_CHUNK_SCHEMA,
        GENERATION_RECORD_DIGEST_SCHEMA,
        GENERATION_RESUME_SCHEMA,
        GENERATION_TOOLSET,
        RANGE_SCHEMA,
        READ_SCHEMA,
        SETUP_APPROVE_SCHEMA,
        SETUP_DATABASE_APPROVE_SCHEMA,
        SETUP_INVENTORY_SCHEMA,
        SETUP_PLAN_SCHEMA,
        STATUS_SCHEMA,
        handle_daily_workload_approve,
        handle_daily_workload_check,
        handle_find_gaps,
        handle_generation_collect,
        handle_generation_complete,
        handle_generation_get_chunk,
        handle_generation_record_digest,
        handle_generation_resume,
        handle_plan_backfill,
        handle_read_entries,
        handle_resolve_range,
        handle_setup_approve,
        handle_setup_database_approve,
        handle_setup_inventory,
        handle_setup_plan,
        handle_status,
    )

    def register(ctx) -> None:
        for name, schema, handler in (
            ("journal_status", STATUS_SCHEMA, handle_status),
            ("journal_resolve_range", RANGE_SCHEMA, handle_resolve_range),
            ("journal_read_entries", READ_SCHEMA, handle_read_entries),
            ("journal_find_gaps", GAPS_SCHEMA, handle_find_gaps),
            ("journal_plan_backfill", BACKFILL_SCHEMA, handle_plan_backfill),
            ("journal_setup_inventory", SETUP_INVENTORY_SCHEMA, handle_setup_inventory),
            ("journal_setup_database_approve", SETUP_DATABASE_APPROVE_SCHEMA, handle_setup_database_approve),
            ("journal_daily_workload_check", DAILY_WORKLOAD_CHECK_SCHEMA, handle_daily_workload_check),
            ("journal_daily_workload_approve", DAILY_WORKLOAD_APPROVE_SCHEMA, handle_daily_workload_approve),
            ("journal_setup_plan", SETUP_PLAN_SCHEMA, handle_setup_plan),
            ("journal_setup_approve", SETUP_APPROVE_SCHEMA, handle_setup_approve),
        ):
            ctx.register_tool(
                name=name,
                toolset="journal",
                schema=schema,
                handler=handler,
                emoji="📓",
            )
        for name, schema, handler in (
            ("journal_generation_collect", GENERATION_COLLECT_SCHEMA, handle_generation_collect),
            ("journal_generation_resume", GENERATION_RESUME_SCHEMA, handle_generation_resume),
            ("journal_generation_get_chunk", GENERATION_GET_CHUNK_SCHEMA, handle_generation_get_chunk),
            ("journal_generation_record_digest", GENERATION_RECORD_DIGEST_SCHEMA, handle_generation_record_digest),
            ("journal_generation_complete", GENERATION_COMPLETE_SCHEMA, handle_generation_complete),
        ):
            ctx.register_tool(
                name=name,
                toolset=GENERATION_TOOLSET,
                schema=schema,
                handler=handler,
                emoji="🔒",
            )
        ctx.register_cli_command(
            name="journal",
            help="Inspect and plan the evidence backed My Journal",
            setup_fn=register_cli,
            handler_fn=journal_command,
            description="Status, gaps, deterministic date ranges, and safe backfill planning.",
        )
