#!/usr/bin/env python3
"""Collect bounded, evidence backed journal inputs from Hermes session databases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from atomic_files import atomic_write_text
from evidence_identity import canonical_evidence_sha256
from journal_config import load_config
from secret_redaction import contains_likely_secret, redact_sensitive, redact_text
from safe_files import descriptor_sqlite_uri, safe_mkdir_tree, safe_open_regular_fd, safe_read_text


HARD_MAX_DATABASES = 128
HARD_MAX_DISCOVERY_ENTRIES = 1_024
DEFAULT_MAX_DATABASE_BYTES = 8 * 1024**3
DATABASE_APPROVAL_TIERS = {16 * 1024**3, 32 * 1024**3}
HARD_MAX_DATABASE_BYTES = 32 * 1024**3
HARD_MAX_SELECTED_MESSAGES = 100_000
HARD_MAX_SESSIONS = 10_000
HARD_MAX_RETAINED_CHARS = 4_000_000
HARD_MAX_MESSAGE_CHARS = 4_000
HARD_MAX_TOOL_CHARS = 1_200
HARD_MAX_RAW_BODY_CHARS = 1_000_000
HARD_MAX_RAW_METADATA_CHARS = 16_384
HARD_MAX_RAW_IDENTIFIER_CHARS = 4_096
HARD_MAX_PACKET_CHUNK_BYTES = 120_000
HARD_MAX_PACKET_CHUNKS = 64
HARD_MAX_PACKET_TOTAL_BYTES = 8_000_000
HARD_MAX_MANIFEST_BYTES = 8_000_000


def _database_confirmation(profile: str, tier: int) -> str:
    token = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:16]
    return f"APPROVE MY JOURNAL DATABASE TIER {tier // 1024**3} GIB FOR PROFILE {token}"


def _daily_confirmation(journal_date: str, tier: int) -> str:
    return f"APPROVE MY JOURNAL DAILY TIER {tier} TRIGGERED BY DATE {journal_date}"


def _plan_confirmation(plan: dict[str, Any]) -> str:
    immutable = {
        key: value for key, value in plan.items()
        if key not in {"confirmation_phrase", "completed_dates", "config_activated"}
    }
    digest = hashlib.sha256(
        json.dumps(immutable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"ENABLE MY JOURNAL PLAN {plan['plan_id']} SHA256 {digest}"


def _receipt_json(output: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(safe_read_text(output, output / name, max_bytes=100_000))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(f"required approval receipt is missing or malformed: {name}") from exc


def validate_capacity_receipts(output: Path, config: Any) -> None:
    output = Path(output).expanduser().absolute()
    database_approvals = dict(config.database_size_approvals)
    if database_approvals:
        receipt = _receipt_json(output, "database-size-approvals.json")
        entries = receipt.get("approvals")
        if receipt.get("schema_version") != 1 or not isinstance(entries, dict) or len(entries) > HARD_MAX_DATABASES:
            raise ValueError("database size approval receipt is malformed")
        validated: dict[str, int] = {}
        for profile, entry in entries.items():
            if not isinstance(profile, str) or not isinstance(entry, dict) or set(entry) != {
                "approved_max_bytes", "confirmation_sha256",
            }:
                raise ValueError("database size approval receipt is malformed")
            tier = entry["approved_max_bytes"]
            expected = hashlib.sha256(_database_confirmation(profile, tier).encode("utf-8")).hexdigest()
            if (
                isinstance(tier, bool) or tier not in DATABASE_APPROVAL_TIERS
                or entry["confirmation_sha256"] != expected
            ):
                raise ValueError("database size approval receipt is malformed")
            validated[profile] = tier
        if any(validated.get(profile) != tier for profile, tier in database_approvals.items()):
            raise ValueError("configured database tier lacks matching durable approval evidence")

    tier = config.max_selected_messages
    if tier == 25_000:
        return
    receipt = _receipt_json(output, "daily-workload-approval.json")
    if receipt.get("schema_version") != 1 or receipt.get("approved_tier") != tier:
        raise ValueError("configured daily message tier lacks matching durable approval evidence")
    if receipt.get("configuration_sha256") != config.daily_authorization_sha256():
        raise ValueError("daily workload approval receipt does not match configured scope and policy")
    kind = receipt.get("approval_kind")
    confirmation_hash = receipt.get("confirmation_sha256")
    if not isinstance(confirmation_hash, str) or re.fullmatch(r"[0-9a-f]{64}", confirmation_hash) is None:
        raise ValueError("daily workload approval receipt is malformed")
    if kind == "targeted_tier":
        journal_date = receipt.get("journal_date")
        if not isinstance(journal_date, str) or date.fromisoformat(journal_date).isoformat() != journal_date:
            raise ValueError("daily workload approval receipt is malformed")
        expected = hashlib.sha256(_daily_confirmation(journal_date, tier).encode("utf-8")).hexdigest()
        if confirmation_hash != expected:
            raise ValueError("daily workload approval receipt is malformed")
    elif kind == "guided_setup":
        plan_id = receipt.get("plan_id")
        if not isinstance(plan_id, str) or re.fullmatch(r"[0-9a-f]{32}", plan_id) is None:
            raise ValueError("daily workload approval receipt is malformed")
        plan = _receipt_json(output, f"approval-plans/{plan_id}.json")
        try:
            valid_plan = (
                plan.get("plan_id") == plan_id
                and plan.get("confirmation_phrase") == _plan_confirmation(plan)
                and plan.get("config_activated") is True
                and plan.get("limits", {}).get("max_selected_messages") == tier
                and hashlib.sha256(plan["confirmation_phrase"].encode("utf-8")).hexdigest()
                == confirmation_hash
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            valid_plan = False
        if not valid_plan:
            raise ValueError("daily workload approval receipt does not match its guided setup plan")
    else:
        raise ValueError("daily workload approval receipt is malformed")


@dataclass(frozen=True)
class DatabaseSource:
    profile: str
    path: Path


class CollectionLimitError(RuntimeError):
    """Raised when a global collection safety ceiling is exceeded."""


class UnsafePathError(ValueError):
    """Raised when a configured trust path contains a symlink."""


def reject_symlink_components(path: Path) -> None:
    """Reject any existing symlink component without resolving the trust anchor."""
    absolute = path.expanduser().absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode):
            raise UnsafePathError(f"configured path contains a symlink: {current}")


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(parent.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def discover_databases(home: Path) -> list[DatabaseSource]:
    """Find the default and profile Hermes state databases."""
    reject_symlink_components(home)
    found: list[DatabaseSource] = []
    default = home / "state.db"
    if not default.is_symlink() and default.is_file() and _inside(default, home):
        found.append(DatabaseSource("default", default))
    profiles = home / "profiles"
    if profiles.is_symlink():
        return found
    if profiles.is_dir():
        with os.scandir(profiles) as entries:
            for scanned_count, entry in enumerate(entries, start=1):
                if scanned_count > HARD_MAX_DISCOVERY_ENTRIES:
                    raise CollectionLimitError(
                        f"discovery entry limit exceeded: {HARD_MAX_DISCOVERY_ENTRIES}"
                    )
                if not entry.is_dir(follow_symlinks=False):
                    continue
                path = profiles / entry.name / "state.db"
                if path.is_symlink() or not path.is_file() or not _inside(path, home):
                    continue
                found.append(DatabaseSource(entry.name, path))
    return sorted(found, key=lambda item: (item.profile, str(item.path)))


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f'pragma table_info("{table}")')}


def _value(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    return row[name] if name in row.keys() else default


def _bounded_sql_text(column: str, alias: str, limit: int) -> str:
    return (
        f'substr(CAST({column} AS TEXT), 1, {limit + 1}) as "{alias}", '
        f'length(CAST({column} AS TEXT)) as "__len_{alias}"'
    )


def _reject_oversized_raw_fields(row: sqlite3.Row, limits: dict[str, int]) -> None:
    for field, limit in limits.items():
        raw_length = _value(row, f"__len_{field}")
        if raw_length is not None and int(raw_length) > limit:
            raise CollectionLimitError(f"raw {field} limit exceeded: {limit}")


def bound_text(
    text: str,
    limit: int,
    *,
    pii_mode: str = "preserve",
    entropy_mode: str = "off",
    finding_counts: Counter[str] | None = None,
) -> str:
    """Bound text with an explicit marker while preserving its beginning."""
    redaction = redact_sensitive(text, pii_mode=pii_mode, entropy_mode=entropy_mode)
    text = redaction.text
    if finding_counts is not None:
        finding_counts.update(redaction.finding_counts)
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}… [TRUNCATED {omitted} chars]"


def collect_range(
    home: Path,
    start_ts: float,
    end_ts: float,
    max_message_chars: int = HARD_MAX_MESSAGE_CHARS,
    max_tool_chars: int = HARD_MAX_TOOL_CHARS,
    max_selected_messages: int = HARD_MAX_SELECTED_MESSAGES,
    max_retained_chars: int = HARD_MAX_RETAINED_CHARS,
    max_sessions: int = HARD_MAX_SESSIONS,
    allowed_profiles: set[str] | None = None,
    allowed_platforms: set[str] | None = None,
    excluded_session_ids: set[str] | None = None,
    pii_mode: str = "preserve",
    entropy_mode: str = "off",
    database_size_approvals: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Collect every session with messages in the half open timestamp range."""
    requested_limits = (
        ("message characters", max_message_chars, HARD_MAX_MESSAGE_CHARS),
        ("tool characters", max_tool_chars, HARD_MAX_TOOL_CHARS),
        ("selected messages", max_selected_messages, HARD_MAX_SELECTED_MESSAGES),
        ("retained characters", max_retained_chars, HARD_MAX_RETAINED_CHARS),
        ("sessions", max_sessions, HARD_MAX_SESSIONS),
    )
    for label, value, ceiling in requested_limits:
        if value <= 0:
            raise ValueError(f"{label} limit must be positive")
        if value > ceiling:
            raise ValueError(f"{label} limit exceeds compiled ceiling {ceiling}")
    approvals = {} if database_size_approvals is None else database_size_approvals
    if not isinstance(approvals, dict) or len(approvals) > HARD_MAX_DATABASES:
        raise ValueError("database approvals exceed compiled database tier bounds")
    for profile, tier in approvals.items():
        if (
            not isinstance(profile, str) or not profile.strip() or len(profile) > 512
            or isinstance(tier, bool) or tier not in DATABASE_APPROVAL_TIERS
        ):
            raise ValueError("database approval exceeds compiled database tier bounds")
    sessions: list[dict[str, Any]] = []
    databases: list[dict[str, Any]] = []
    platforms: set[str] = set()
    profiles: set[str] = set()
    message_count = 0
    retained_message_count = 0
    retained_char_count = 0
    redaction_counts: Counter[str] = Counter()
    selected_session_count = 0

    discovered_sources = discover_databases(home)
    sources = [
        source for source in discovered_sources
        if allowed_profiles is None or source.profile in allowed_profiles
    ]
    if len(sources) > HARD_MAX_DATABASES:
        raise CollectionLimitError(
            f"database limit exceeded: {len(sources)} allowed-profile databases, "
            f"{HARD_MAX_DATABASES} allowed"
        )
    for source in sources:
        db_report: dict[str, Any] = {
            "profile": source.profile,
            "path": f"{source.profile}/state.db",
            "status": "ok",
        }
        con: sqlite3.Connection | None = None
        database_descriptor: int | None = None
        try:
            database_descriptor = safe_open_regular_fd(home, source.path)
            database_size = os.fstat(database_descriptor).st_size
            approved_limit = approvals.get(source.profile, DEFAULT_MAX_DATABASE_BYTES)
            if database_size > approved_limit:
                raise CollectionLimitError(
                    f"database byte limit exceeded: {database_size} bytes, "
                    f"{approved_limit} approved for profile {source.profile}"
                )
            database_uri = descriptor_sqlite_uri(database_descriptor)
            con = sqlite3.connect(database_uri, uri=True)
            con.row_factory = sqlite3.Row
            session_cols = _columns(con, "sessions")
            message_cols = _columns(con, "messages")
            required_sessions = {"id", "source"}
            required_messages = {"id", "session_id", "role", "content", "timestamp"}
            if not required_sessions.issubset(session_cols) or not required_messages.issubset(message_cols):
                raise RuntimeError("unsupported Hermes session database schema")

            optional_session_columns = ["title", "started_at", "chat_id", "thread_id", "display_name"]
            raw_limits = {
                "id": HARD_MAX_RAW_IDENTIFIER_CHARS,
                "session_id": HARD_MAX_RAW_IDENTIFIER_CHARS,
                "role": HARD_MAX_RAW_IDENTIFIER_CHARS,
                "content": HARD_MAX_RAW_BODY_CHARS,
                "timestamp": 128,
                "tool_calls": HARD_MAX_RAW_BODY_CHARS,
                "tool_name": HARD_MAX_RAW_METADATA_CHARS,
                "source": HARD_MAX_RAW_IDENTIFIER_CHARS,
                "title": HARD_MAX_RAW_METADATA_CHARS,
                "started_at": 128,
                "chat_id": HARD_MAX_RAW_IDENTIFIER_CHARS,
                "thread_id": HARD_MAX_RAW_IDENTIFIER_CHARS,
                "display_name": HARD_MAX_RAW_METADATA_CHARS,
            }
            select_fields = [
                _bounded_sql_text("m.id", "id", raw_limits["id"]),
                _bounded_sql_text("m.session_id", "session_id", raw_limits["session_id"]),
                _bounded_sql_text("m.role", "role", raw_limits["role"]),
                _bounded_sql_text("m.content", "content", raw_limits["content"]),
                _bounded_sql_text("m.timestamp", "timestamp", raw_limits["timestamp"]),
                (
                    _bounded_sql_text("m.tool_calls", "tool_calls", raw_limits["tool_calls"])
                    if "tool_calls" in message_cols else
                    'NULL as "tool_calls", NULL as "__len_tool_calls"'
                ),
                (
                    _bounded_sql_text("m.tool_name", "tool_name", raw_limits["tool_name"])
                    if "tool_name" in message_cols else
                    'NULL as "tool_name", NULL as "__len_tool_name"'
                ),
                _bounded_sql_text("s.source", "source", raw_limits["source"]),
            ]
            for name in optional_session_columns:
                if name == "started_at":
                    select_fields.append(
                        _bounded_sql_text('s."started_at"', "started_at", raw_limits["started_at"])
                        if name in session_cols
                        else 'NULL as "started_at", NULL as "__len_started_at"'
                    )
                elif name in session_cols:
                    select_fields.append(
                        _bounded_sql_text(f's."{name}"', name, raw_limits[name])
                    )
                else:
                    select_fields.append(
                        f'NULL as "{name}", NULL as "__len_{name}"'
                    )
            where = [
                "m.timestamp >= ?", "m.timestamp < ?",
                "lower(coalesce(m.role, '')) not in ('system', 'developer')",
            ]
            parameters: list[Any] = [start_ts, end_ts]
            if allowed_platforms is not None:
                if not allowed_platforms:
                    where.append("0")
                else:
                    values = sorted(allowed_platforms)
                    where.append(f"s.source in ({','.join('?' for _ in values)})")
                    parameters.extend(values)
            if excluded_session_ids:
                values = sorted(excluded_session_ids)
                where.append(f"m.session_id not in ({','.join('?' for _ in values)})")
                parameters.extend(values)
            rows = con.execute(
                f"""select {', '.join(select_fields)}
                   from messages m
                   join sessions s on s.id = m.session_id
                   where {' and '.join(where)}
                   order by m.session_id, m.timestamp, m.id""",
                parameters,
            )
            grouped: dict[str, dict[str, Any]] = {}
            selected_in_database = 0
            for row in rows:
                _reject_oversized_raw_fields(row, raw_limits)
                platform = str(_value(row, "source", "unknown") or "unknown")
                session_id = str(_value(row, "session_id", ""))
                message_count += 1
                selected_in_database += 1
                if message_count > max_selected_messages:
                    raise CollectionLimitError(
                        f"selected message limit exceeded: {max_selected_messages}"
                    )
                if session_id not in grouped:
                    selected_session_count += 1
                    if selected_session_count > max_sessions:
                        raise CollectionLimitError(
                            f"session limit exceeded: {max_sessions}"
                        )
                item = grouped.setdefault(
                    session_id,
                    {
                        "profile": source.profile,
                        "platform": platform,
                        "session_id": session_id,
                        "title": bound_text(
                            str(_value(row, "title", "") or "Untitled session"), 300,
                            pii_mode=pii_mode, entropy_mode=entropy_mode,
                            finding_counts=redaction_counts,
                        ),
                        "started_at": (
                            float(_value(row, "started_at"))
                            if _value(row, "started_at") is not None else None
                        ),
                        "chat_id": _value(row, "chat_id"),
                        "thread_id": _value(row, "thread_id"),
                        "display_name": bound_text(
                            str(_value(row, "display_name", "") or ""), 300,
                            pii_mode=pii_mode, entropy_mode=entropy_mode,
                            finding_counts=redaction_counts,
                        ),
                        "context_label": "unclassified",
                        "messages": [],
                    },
                )
                role = str(_value(row, "role", "unknown"))
                platforms.add(platform)
                profiles.add(source.profile)
                if role in {"system", "developer"}:
                    continue
                content_limit = max_tool_chars if role == "tool" else max_message_chars
                raw_tool_calls = str(_value(row, "tool_calls", "") or "")
                content = bound_text(
                    str(_value(row, "content", "") or ""), content_limit,
                    pii_mode=pii_mode, entropy_mode=entropy_mode,
                    finding_counts=redaction_counts,
                )
                tool_name = bound_text(
                    str(_value(row, "tool_name", "") or ""), 120,
                    pii_mode=pii_mode, entropy_mode=entropy_mode,
                    finding_counts=redaction_counts,
                ) or None
                tool_calls = bound_text(
                    raw_tool_calls, max_tool_chars,
                    pii_mode=pii_mode, entropy_mode=entropy_mode,
                    finding_counts=redaction_counts,
                ) if raw_tool_calls else None
                retained_char_count += len(content) + len(tool_name or "") + len(tool_calls or "")
                if retained_char_count > max_retained_chars:
                    raise CollectionLimitError(
                        f"retained character limit exceeded: {max_retained_chars}"
                    )
                item["messages"].append(
                    {
                        "message_id": int(_value(row, "id", 0)),
                        "role": role,
                        "timestamp": float(_value(row, "timestamp", 0.0)),
                        "content": content,
                        "tool_name": tool_name,
                        "tool_calls": tool_calls,
                    }
                )
                retained_message_count += 1
            sessions.extend(grouped.values())
            db_report["message_count"] = selected_in_database
            db_report["session_count"] = len(grouped)
        except CollectionLimitError:
            raise
        except Exception as exc:
            db_report["status"] = "error"
            db_report["error"] = str(exc)
        finally:
            if con is not None:
                con.close()
            if database_descriptor is not None:
                os.close(database_descriptor)
        databases.append(db_report)

    sessions.sort(key=lambda item: (item.get("started_at") or 0.0, item["profile"], item["session_id"]))
    for session in sessions:
        identity = f"{session['profile']}\0{session['session_id']}".encode("utf-8")
        session["coverage_ref"] = hashlib.sha256(identity).hexdigest()
        if pii_mode == "mask":
            session["session_id"] = session["coverage_ref"]
            if session.get("chat_id") is not None:
                redaction_counts["identifier"] += 1
            if session.get("thread_id") is not None:
                redaction_counts["identifier"] += 1
            session["chat_id"] = None
            session["thread_id"] = None
    return {
        "schema_version": 2,
        "window": {"start_ts": start_ts, "end_ts": end_ts},
        "coverage": {
            "database_count": sum(1 for item in databases if item["status"] == "ok"),
            "database_error_count": sum(1 for item in databases if item["status"] != "ok"),
            "session_count": len(sessions),
            "message_count": message_count,
            "retained_message_count": retained_message_count,
            "retained_char_count": retained_char_count,
            "redaction_counts": dict(sorted(redaction_counts.items())),
            "platforms": sorted(platforms),
            "profiles": sorted(profiles),
        },
        "databases": databases,
        "sessions": sessions,
    }


def _iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def render_packet(manifest: dict[str, Any]) -> str:
    """Render a model readable packet while representing every selected session."""
    coverage = manifest["coverage"]
    lines = [
        f"# My Journal Evidence Packet for {manifest['journal_date']}",
        "",
        f"Run ID: {manifest['run_id']}",
        f"Evidence SHA256: {manifest['evidence_sha256']}",
        f"UTC window: {_iso_utc(manifest['window']['start_ts'])} to {_iso_utc(manifest['window']['end_ts'])}",
        f"Databases: {coverage['database_count']} readable, {coverage['database_error_count']} errors",
        f"Sessions: {coverage['session_count']}",
        f"Messages selected: {coverage['message_count']}",
        f"Platforms: {', '.join(coverage['platforms']) or 'none'}",
        f"Profiles: {', '.join(coverage['profiles']) or 'none'}",
        "",
        "All context classes remain included. Label each session professional, personal, mixed, or unclear without filtering it out.",
        "Internal reasoning is excluded. Text marked REDACTED must never be reconstructed.",
        "",
    ]
    for index, session in enumerate(manifest["sessions"], start=1):
        lines.extend(
            [
                f"## Session {index}: {session['title']}",
                f"Profile: {session['profile']}",
                f"Platform: {session['platform']}",
                f"Session ID: {session['session_id']}",
                f"Session Ref: {session['coverage_ref']}",
                f"Context label: {session['context_label']}",
                "",
            ]
        )
        if not session["messages"]:
            lines.append("No retained conversational messages in this range.")
        for message in session["messages"]:
            stamp = _iso_utc(message["timestamp"])
            tool = f" tool={message['tool_name']}" if message.get("tool_name") else ""
            lines.append(f"### {message['role']} message {message['message_id']} at {stamp}{tool}")
            if message.get("content"):
                lines.append(message["content"])
            if message.get("tool_calls"):
                lines.append(f"Tool calls: {message['tool_calls']}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _chunk_header(
    manifest: dict[str, Any],
    index: int,
    count: int,
    chunk_id: str,
    body_sha256: str,
) -> str:
    coverage = manifest["coverage"]
    return "\n".join([
        f"# My Journal Evidence Chunk for {manifest['journal_date']}",
        "",
        f"Run ID: {manifest['run_id']}",
        f"Evidence SHA256: {manifest['evidence_sha256']}",
        f"Chunk: {index:06d} of {count:06d}",
        f"Chunk ID: {chunk_id}",
        f"Chunk Body SHA256: {body_sha256}",
        f"Timezone: {manifest['timezone']}",
        f"UTC window: {_iso_utc(manifest['window']['start_ts'])} to {_iso_utc(manifest['window']['end_ts'])}",
        f"Sessions in run: {coverage['session_count']}",
        f"Messages selected: {coverage['message_count']}",
        "Internal reasoning is excluded. Text marked REDACTED must never be reconstructed.",
        "A session reference may repeat across chunks. Emit each Session Ref exactly once in final digests.",
        "---",
        "",
    ])


def _packet_units(manifest: dict[str, Any]) -> list[str]:
    units: list[str] = []
    for session_index, session in enumerate(manifest["sessions"], start=1):
        context = [
            f"## Session {session_index}: {session['title']}",
            f"Profile: {session['profile']}",
            f"Platform: {session['platform']}",
            f"Session ID: {session['session_id']}",
            f"Session Ref: {session['coverage_ref']}",
            f"Context label: {session['context_label']}",
            "",
        ]
        messages = session["messages"]
        if not messages:
            units.append("\n".join(context + ["No retained conversational messages in this range.", ""]))
            continue
        for message in messages:
            stamp = _iso_utc(message["timestamp"])
            tool = f" tool={message['tool_name']}" if message.get("tool_name") else ""
            lines = context + [
                f"### {message['role']} message {message['message_id']} at {stamp}{tool}",
            ]
            if message.get("content"):
                lines.append(message["content"])
            if message.get("tool_calls"):
                lines.append(f"Tool calls: {message['tool_calls']}")
            lines.append("")
            units.append("\n".join(lines))
    return units or ["No selected sessions in this run.\n"]


def render_packet_chunks(
    manifest: dict[str, Any],
    target_bytes: int = 120_000,
    max_chunks: int = 64,
) -> list[str]:
    """Pack evidence deterministically at message boundaries under a byte ceiling."""
    if target_bytes < 1024:
        raise ValueError("packet chunk target must be at least 1024 bytes")
    if target_bytes > HARD_MAX_PACKET_CHUNK_BYTES:
        raise ValueError(
            f"packet chunk target exceeds compiled ceiling {HARD_MAX_PACKET_CHUNK_BYTES}"
        )
    if max_chunks <= 0:
        raise ValueError("maximum packet chunks must be positive")
    if max_chunks > HARD_MAX_PACKET_CHUNKS:
        raise ValueError(
            f"maximum packet chunks exceeds compiled ceiling {HARD_MAX_PACKET_CHUNKS}"
        )
    header_budget = len(
        _chunk_header(manifest, 999999, 999999, "0" * 64, "0" * 64).encode("utf-8")
    )
    payload_budget = target_bytes - header_budget
    payloads: list[str] = []
    current = ""
    for unit in _packet_units(manifest):
        encoded_size = len(unit.encode("utf-8"))
        if encoded_size > payload_budget:
            raise CollectionLimitError(
                f"single packet unit exceeds chunk target: {encoded_size} bytes"
            )
        candidate = unit if not current else current + "\n" + unit
        if len(candidate.encode("utf-8")) > payload_budget:
            payloads.append(current)
            current = unit
        else:
            current = candidate
    if current:
        payloads.append(current)
    if len(payloads) > max_chunks:
        raise CollectionLimitError(
            f"packet chunk limit exceeded: {len(payloads)} required, {max_chunks} allowed"
        )
    chunks = []
    for index, payload in enumerate(payloads, start=1):
        body = payload.rstrip() + "\n"
        body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        chunk_id = hashlib.sha256(
            b"my-journal-chunk-v1\0"
            + bytes.fromhex(manifest["evidence_sha256"])
            + b"\0"
            + str(index).encode("ascii")
            + b"\0"
            + bytes.fromhex(body_sha256)
        ).hexdigest()
        chunks.append(
            _chunk_header(manifest, index, len(payloads), chunk_id, body_sha256) + body
        )
    if any(len(chunk.encode("utf-8")) > target_bytes for chunk in chunks):
        raise AssertionError("packet chunk exceeded its validated byte ceiling")
    total_bytes = sum(len(chunk.encode("utf-8")) for chunk in chunks)
    if total_bytes > HARD_MAX_PACKET_TOTAL_BYTES:
        raise CollectionLimitError(
            f"packet total exceeds compiled ceiling {HARD_MAX_PACKET_TOTAL_BYTES} bytes"
        )
    return chunks


def write_run(
    home: Path,
    output_dir: Path,
    journal_date: str,
    start_ts: float,
    end_ts: float,
    *,
    max_message_chars: int = HARD_MAX_MESSAGE_CHARS,
    max_tool_chars: int = HARD_MAX_TOOL_CHARS,
    timezone_name: str = "UTC",
    max_selected_messages: int = HARD_MAX_SELECTED_MESSAGES,
    max_retained_chars: int = HARD_MAX_RETAINED_CHARS,
    max_sessions: int = HARD_MAX_SESSIONS,
    allowed_profiles: set[str] | None = None,
    allowed_platforms: set[str] | None = None,
    excluded_session_ids: set[str] | None = None,
    pii_mode: str = "preserve",
    entropy_mode: str = "off",
    database_size_approvals: dict[str, int] | None = None,
    policy_metadata: dict[str, Any] | None = None,
    allow_database_errors: bool = True,
    packet_chunk_chars: int = HARD_MAX_PACKET_CHUNK_BYTES,
    max_packet_chunks: int = HARD_MAX_PACKET_CHUNKS,
) -> dict[str, Any]:
    """Collect a range and atomically write its evidence manifest and model packet."""
    reject_symlink_components(output_dir)
    for subtree in ("evidence", "packets", "pending", "runs", "state", "notes"):
        reject_symlink_components(output_dir / subtree)
    expected_start, expected_end = date_window(journal_date, timezone_name)
    if start_ts != expected_start or end_ts != expected_end:
        raise ValueError("timestamp window does not match journal date and timezone")
    manifest = collect_range(
        home,
        start_ts,
        end_ts,
        max_message_chars=max_message_chars,
        max_tool_chars=max_tool_chars,
        max_selected_messages=max_selected_messages,
        max_retained_chars=max_retained_chars,
        max_sessions=max_sessions,
        allowed_profiles=allowed_profiles,
        allowed_platforms=allowed_platforms,
        excluded_session_ids=excluded_session_ids,
        pii_mode=pii_mode,
        entropy_mode=entropy_mode,
        database_size_approvals=database_size_approvals,
    )
    manifest["journal_date"] = journal_date
    manifest["timezone"] = timezone_name
    manifest["window"] = {
        "start_ts": start_ts,
        "end_ts": end_ts,
        "start_utc": _iso_utc(start_ts),
        "end_utc": _iso_utc(end_ts),
    }
    manifest["policy"] = policy_metadata or {
        "policy_id": "direct-api-v1",
        "redact_secrets": True,
    }
    if manifest["coverage"]["database_error_count"] and not allow_database_errors:
        raise RuntimeError("database errors blocked collection before artifact creation")
    evidence_sha256 = canonical_evidence_sha256(manifest)
    run_id = evidence_sha256[:16]
    manifest["run_id"] = run_id
    manifest["evidence_sha256"] = evidence_sha256
    manifest["created_at"] = datetime.now(tz=timezone.utc).isoformat()
    manifest_bytes = len(json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
    if manifest_bytes > HARD_MAX_MANIFEST_BYTES:
        raise CollectionLimitError(
            f"manifest exceeds compiled ceiling {HARD_MAX_MANIFEST_BYTES} bytes"
        )
    packet_chunks = render_packet_chunks(manifest, packet_chunk_chars, max_packet_chunks)

    year, month, _ = journal_date.split("-")
    evidence_dir = output_dir / "evidence" / year / month
    packet_dir = output_dir / "packets" / year / month / f"{journal_date}-{run_id}"
    pending_dir = output_dir / "pending"
    for directory in (evidence_dir, packet_dir, pending_dir):
        safe_mkdir_tree(output_dir, directory)
    manifest_path = evidence_dir / f"{journal_date}-{run_id}.json"
    packet_paths = [packet_dir / f"chunk-{index:06d}.md" for index in range(1, len(packet_chunks) + 1)]
    packet_plan_path = packet_dir / "plan.json"
    pending_path = pending_dir / f"{run_id}.json"

    plan_chunks = []
    owned_session_refs: set[str] = set()
    for index, (packet_path, packet_text) in enumerate(zip(packet_paths, packet_chunks), start=1):
        atomic_write_text(packet_path, packet_text, trusted_root=output_dir)
        packet_sha256 = hashlib.sha256(packet_text.encode("utf-8")).hexdigest()
        header, body = packet_text.split("\n---\n", 1)
        header_values = {
            line.split(": ", 1)[0]: line.split(": ", 1)[1]
            for line in header.splitlines()
            if ": " in line
        }
        present_refs = [
            session["coverage_ref"]
            for session in manifest["sessions"]
            if f"Session Ref: {session['coverage_ref']}" in body
        ]
        newly_owned = [ref for ref in present_refs if ref not in owned_session_refs]
        continued = [ref for ref in present_refs if ref in owned_session_refs]
        owned_session_refs.update(newly_owned)
        plan_chunks.append({
            "index": index,
            "chunk_id": header_values["Chunk ID"],
            "body_sha256": header_values["Chunk Body SHA256"],
            "sha256": packet_sha256,
            "path": str(packet_path),
            "bytes": len(packet_text.encode("utf-8")),
            "owned_session_refs": newly_owned,
            "continued_session_refs": continued,
        })
    index_chunks = [
        {
            key: chunk[key]
            for key in (
                "index", "chunk_id", "body_sha256", "sha256",
                "bytes", "owned_session_refs", "continued_session_refs",
            )
        }
        for chunk in plan_chunks
    ]
    chunk_index_sha256 = hashlib.sha256(
        json.dumps(index_chunks, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    packet_plan = {
        "schema_version": 1,
        "run_id": run_id,
        "evidence_sha256": evidence_sha256,
        "chunk_target_bytes": packet_chunk_chars,
        "chunk_count": len(plan_chunks),
        "chunk_index_sha256": chunk_index_sha256,
        "chunks": plan_chunks,
    }
    manifest["delivery"] = {
        "schema_version": 1,
        "mode": "single" if len(index_chunks) == 1 else "chunked",
        "chunk_count": len(index_chunks),
        "chunk_index_sha256": chunk_index_sha256,
        "chunks": index_chunks,
    }
    final_manifest_text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    if len(final_manifest_text.encode("utf-8")) > HARD_MAX_MANIFEST_BYTES:
        raise CollectionLimitError(
            f"manifest exceeds compiled ceiling {HARD_MAX_MANIFEST_BYTES} bytes"
        )
    atomic_write_text(packet_plan_path, json.dumps(packet_plan, indent=2) + "\n", trusted_root=output_dir)
    atomic_write_text(manifest_path, final_manifest_text, trusted_root=output_dir)
    pending = {
        "run_id": run_id,
        "journal_date": journal_date,
        "manifest_path": str(manifest_path),
        "packet_path": str(packet_paths[0]) if len(packet_paths) == 1 else None,
        "packet_paths": [str(path) for path in packet_paths],
        "packet_plan_path": str(packet_plan_path),
        "status": "pending_note_validation",
    }
    atomic_write_text(pending_path, json.dumps(pending, indent=2) + "\n", trusted_root=output_dir)
    return {**pending, "pending_path": str(pending_path), "coverage": manifest["coverage"]}


def date_window(journal_date: str, timezone_name: str) -> tuple[float, float]:
    """Convert a local calendar date into a half open UTC timestamp range."""
    zone = ZoneInfo(timezone_name)
    day = date.fromisoformat(journal_date)
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start.timestamp(), end.timestamp()


def _default_date(timezone_name: str) -> str:
    zone = ZoneInfo(timezone_name)
    return (datetime.now(zone).date() - timedelta(days=1)).isoformat()


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")))
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--date", dest="journal_date")
    parser.add_argument("--timezone", default=None)
    parser.add_argument("--max-message-chars", type=_positive_int, default=None)
    parser.add_argument("--max-tool-chars", type=_positive_int, default=None)
    parser.add_argument("--allow-database-errors", action="store_true")
    return parser


def _configured_limit(requested: int | None, configured: int, label: str) -> int:
    if requested is None:
        return configured
    if requested > configured:
        raise ValueError(f"{label} cannot raise the configured limit")
    return requested


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.output or args.home / "journal"
    config_path = args.config or output / "config.json"
    try:
        config = load_config(config_path)
        config.require_enabled()
        validate_capacity_receipts(output, config)
        timezone_name = (
            os.getenv("MY_JOURNAL_TIMEZONE")
            or args.timezone
            or config.timezone
        )
        journal_date = args.journal_date or _default_date(timezone_name)
        start_ts, end_ts = date_window(journal_date, timezone_name)
        result = write_run(
            home=args.home,
            output_dir=output,
            journal_date=journal_date,
            start_ts=start_ts,
            end_ts=end_ts,
            max_message_chars=_configured_limit(
                args.max_message_chars, config.max_message_chars, "max_message_chars"
            ),
            max_tool_chars=_configured_limit(
                args.max_tool_chars, config.max_tool_chars, "max_tool_chars"
            ),
            timezone_name=timezone_name,
            max_selected_messages=config.max_selected_messages,
            max_retained_chars=config.max_retained_chars,
            max_sessions=config.max_sessions,
            allowed_profiles=set(config.profiles),
            allowed_platforms=set(config.platforms),
            excluded_session_ids=set(config.excluded_session_ids),
            pii_mode=config.pii_mode,
            entropy_mode=config.entropy_mode,
            database_size_approvals=dict(config.database_size_approvals),
            policy_metadata=config.manifest_policy(),
            allow_database_errors=args.allow_database_errors,
            packet_chunk_chars=config.packet_chunk_bytes,
            max_packet_chunks=config.max_packet_chunks,
        )
    except (OSError, RuntimeError, ValueError, ZoneInfoNotFoundError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
