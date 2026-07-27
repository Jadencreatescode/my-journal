"""My Journal plugin registration."""

from __future__ import annotations

from .cli import journal_command, register_cli
from .tools import (
    BACKFILL_SCHEMA,
    GAPS_SCHEMA,
    GENERATION_COLLECT_SCHEMA,
    GENERATION_COMPLETE_SCHEMA,
    GENERATION_GET_CHUNK_SCHEMA,
    GENERATION_RECORD_DIGEST_SCHEMA,
    GENERATION_TOOLSET,
    RANGE_SCHEMA,
    READ_SCHEMA,
    STATUS_SCHEMA,
    handle_find_gaps,
    handle_generation_collect,
    handle_generation_complete,
    handle_generation_get_chunk,
    handle_generation_record_digest,
    handle_plan_backfill,
    handle_read_entries,
    handle_resolve_range,
    handle_status,
)


def register(ctx) -> None:
    for name, schema, handler in (
        ("journal_status", STATUS_SCHEMA, handle_status),
        ("journal_resolve_range", RANGE_SCHEMA, handle_resolve_range),
        ("journal_read_entries", READ_SCHEMA, handle_read_entries),
        ("journal_find_gaps", GAPS_SCHEMA, handle_find_gaps),
        ("journal_plan_backfill", BACKFILL_SCHEMA, handle_plan_backfill),
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
