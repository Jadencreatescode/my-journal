"""Model tool schemas and fail closed handlers for the journal plugin."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

from .core import _safe_files, discover_entries, journal_status, read_validated_entries, resolve_date_range


def journal_root() -> Path:
    configured = os.environ.get("MY_JOURNAL_ROOT")
    if configured:
        return Path(configured).expanduser()
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return home / "journal"


def journal_timezone() -> str | None:
    value = os.environ.get("MY_JOURNAL_TIMEZONE", "").strip()
    if value:
        return value
    config_path = journal_root() / "config.json"
    if config_path.exists() or config_path.is_symlink():
        config = json.loads(
            _safe_files.safe_read_text(journal_root(), config_path, max_bytes=1_000_000)
        )
        if not isinstance(config, dict):
            raise ValueError("journal config must be a JSON object")
        configured = config.get("timezone")
        if configured is not None and (not isinstance(configured, str) or not configured.strip()):
            raise ValueError("journal config timezone must be a nonempty string")
        return configured.strip() if isinstance(configured, str) else None
    return None


def _result(fn, *args, **kwargs) -> str:
    try:
        return json.dumps(fn(*args, **kwargs), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


def handle_status(args: dict, **kwargs) -> str:
    return _result(journal_status, journal_root())


def handle_resolve_range(args: dict, **kwargs) -> str:
    def run():
        start, end = resolve_date_range(str(args.get("range", "")), timezone_name=journal_timezone())
        return {"start_date": start.isoformat(), "end_date": end.isoformat()}
    return _result(run)


def handle_read_entries(args: dict, **kwargs) -> str:
    def run():
        start, end = resolve_date_range(str(args.get("range", "")), timezone_name=journal_timezone())
        requested = int(args.get("max_chars", 120000))
        limit = min(max(requested, 1), 200000)
        return read_validated_entries(journal_root(), start, end, max_total_chars=limit)
    return _result(run)


def _missing_days(range_text: str) -> dict:
    start, end = resolve_date_range(range_text, timezone_name=journal_timezone())
    if (end - start).days > 3660:
        raise ValueError("backfill range may not exceed 3661 calendar days")
    existing = {item["date"] for item in discover_entries(journal_root())}
    missing = []
    cursor = start
    while cursor <= end:
        value = cursor.isoformat()
        if value not in existing:
            missing.append(value)
        cursor += timedelta(days=1)
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "existing_entry_count": (end - start).days + 1 - len(missing),
        "missing_entry_count": len(missing),
        "missing_dates": missing,
    }


def handle_find_gaps(args: dict, **kwargs) -> str:
    range_text = str(args.get("range", ""))
    if range_text:
        return _result(_missing_days, range_text)
    return _result(journal_status, journal_root())


def handle_plan_backfill(args: dict, **kwargs) -> str:
    return _result(_missing_days, str(args.get("range", "")))


STATUS_SCHEMA = {
    "name": "journal_status",
    "description": "Report available daily journal entries, coverage dates, and calendar gaps. Use before journal summaries or maintenance.",
    "parameters": {"type": "object", "properties": {}},
}
RANGE_SCHEMA = {
    "name": "journal_resolve_range",
    "description": "Resolve a journal date phrase into an inclusive start and end date.",
    "parameters": {"type": "object", "properties": {"range": {"type": "string"}}, "required": ["range"]},
}
READ_SCHEMA = {
    "name": "journal_read_entries",
    "description": "Read only evidence validated journal entries in a date range for semantic summarization. Rejected entries are returned with errors.",
    "parameters": {"type": "object", "properties": {"range": {"type": "string"}, "max_chars": {"type": "integer", "minimum": 1, "maximum": 200000}}, "required": ["range"]},
}
GAPS_SCHEMA = {
    "name": "journal_find_gaps",
    "description": "List missing daily journal note dates, optionally inside a requested range.",
    "parameters": {"type": "object", "properties": {"range": {"type": "string"}}},
}
BACKFILL_SCHEMA = {
    "name": "journal_plan_backfill",
    "description": "Plan a read only historical journal backfill by listing existing and missing dates. This never starts collection.",
    "parameters": {"type": "object", "properties": {"range": {"type": "string"}}, "required": ["range"]},
}
