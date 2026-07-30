"""Deterministic journal paths, date ranges, and entry discovery."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_DATE_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
_EXPLICIT_RANGE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})\s+(?:to|through)\s+(\d{4}-\d{2}-\d{2})\s*$",
    re.IGNORECASE,
)
_LAST_DAYS = re.compile(r"^\s*last\s+(\d+)\s+days?\s*$", re.IGNORECASE)
_MAX_NOTE_BYTES = 8_000_000
_MAX_MANIFEST_BYTES = 8_000_000
_MAX_DIGEST_BYTES = 1_000_000
_RUN_ID = re.compile(r"^[0-9a-f]{16}$")
_PENDING_RECEIPT_FILE = re.compile(r"^[0-9a-f]{16}\.json$")
_MAX_PENDING_RUNS = 4096
_PENDING_RECEIPT_FIELDS = frozenset(
    {
        "run_id",
        "journal_date",
        "manifest_path",
        "packet_path",
        "packet_paths",
        "packet_plan_path",
        "status",
    }
)


def validate_pending_receipt(value: Any, *, expected_run_id: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("pending run receipt is malformed")
    missing = sorted(_PENDING_RECEIPT_FIELDS - set(value))
    if missing:
        raise ValueError(f"pending run receipt is missing {missing[0]}")
    unexpected = sorted(set(value) - _PENDING_RECEIPT_FIELDS)
    if unexpected:
        raise ValueError(f"pending run receipt has unexpected fields: {', '.join(unexpected)}")
    run_id = value.get("run_id")
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("pending run receipt has an invalid run_id")
    if expected_run_id is not None and run_id != expected_run_id:
        raise ValueError("pending run receipt does not match its filename")
    journal_date = value.get("journal_date")
    if not isinstance(journal_date, str):
        raise ValueError("pending run receipt is missing journal_date")
    parsed = date.fromisoformat(journal_date)
    if parsed.isoformat() != journal_date:
        raise ValueError("pending run receipt has an invalid journal_date")
    for field in ("manifest_path", "packet_path", "packet_plan_path"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise ValueError(f"pending run receipt is missing {field}")
    if value.get("status") != "pending_note_validation":
        raise ValueError("pending run receipt has an invalid status")
    packet_paths = value.get("packet_paths")
    if not isinstance(packet_paths, list) or not packet_paths or any(
        not isinstance(path, str) or not path for path in packet_paths
    ):
        raise ValueError("pending run receipt has invalid packet_paths")
    if value["packet_path"] != packet_paths[0]:
        raise ValueError("pending run receipt packet_path must equal first packet_paths item")
    return value


def validate_completed_state(value: Any, receipt: dict[str, Any], root: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("completed run state is malformed")
    run_id = receipt["run_id"]
    journal_date = receipt["journal_date"]
    if value.get("status") != "completed":
        raise ValueError("completed run state has an invalid status")
    if value.get("run_id") != run_id or value.get("journal_date") != journal_date:
        raise ValueError("completed run state does not match its receipt")
    if value.get("manifest_path") != receipt["manifest_path"]:
        raise ValueError("completed run state does not match its manifest")
    year, month, _ = journal_date.split("-")
    expected_note = root / "notes" / year / month / f"{journal_date}.md"
    if value.get("note_path") != str(expected_note):
        raise ValueError("completed run state does not match its canonical note")
    expected_digest_dir = root / "runs" / run_id / "digests"
    if value.get("digest_dir") != str(expected_digest_dir):
        raise ValueError("completed run state does not match its digest directory")
    if not isinstance(value.get("coverage"), dict):
        raise ValueError("completed run state is missing coverage")
    validated_at = value.get("validated_at")
    if not isinstance(validated_at, str) or not validated_at:
        raise ValueError("completed run state is missing validated_at")
    timestamp = datetime.fromisoformat(validated_at)
    if timestamp.tzinfo is None:
        raise ValueError("completed run state validated_at must include a timezone")
    return value

_SAFE_FILES_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills" / "note-taking" / "my-journal" / "scripts" / "safe_files.py"
)
_safe_spec = importlib.util.spec_from_file_location("my_journal_safe_files", _SAFE_FILES_PATH)
if _safe_spec is None or _safe_spec.loader is None:
    raise RuntimeError(f"could not load safe file operations: {_SAFE_FILES_PATH}")
_safe_files = importlib.util.module_from_spec(_safe_spec)
sys.modules[_safe_spec.name] = _safe_files
_safe_spec.loader.exec_module(_safe_files)


def _read_descriptor_json(descriptor: int, name: str, *, max_bytes: int) -> dict[str, Any]:
    file_descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        dir_fd=descriptor,
    )
    try:
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
            raise ValueError("pending run receipt is unsafe")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_descriptor, min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("pending run receipt exceeds compiled size ceiling")
        value = json.loads(b"".join(chunks).decode("utf-8"))
    finally:
        os.close(file_descriptor)
    if not isinstance(value, dict):
        raise ValueError("pending run receipt is malformed")
    return value


def read_pending_receipts(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    root_descriptor: int | None = None
    pending_descriptor: int | None = None
    try:
        root_descriptor = _safe_files.open_directory_fd(root)
        try:
            pending_descriptor = os.open(
                "pending",
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=root_descriptor,
            )
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise ValueError("journal pending directory is unsafe") from exc
        names = sorted(os.listdir(pending_descriptor))
        if len(names) > _MAX_PENDING_RUNS:
            raise ValueError("pending run count exceeds compiled ceiling")
        receipts: list[tuple[Path, dict[str, Any]]] = []
        for name in names:
            if _PENDING_RECEIPT_FILE.fullmatch(name) is None:
                raise ValueError("pending run directory contains an unsafe receipt")
            value = _read_descriptor_json(pending_descriptor, name, max_bytes=100_000)
            receipts.append((root / "pending" / name, value))
        return receipts
    finally:
        if pending_descriptor is not None:
            os.close(pending_descriptor)
        if root_descriptor is not None:
            os.close(root_descriptor)


class UnsafePathError(ValueError):
    """Raised when a configured journal trust path contains a symlink."""


def reject_symlink_components(path: Path) -> None:
    absolute = _safe_files.canonical_descriptor_path(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode):
            raise UnsafePathError(f"configured path contains a symlink: {current}")


def resolve_date_range(
    value: str,
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> tuple[date, date]:
    """Resolve a deliberately small, predictable journal date grammar."""
    if timezone_name:
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"time zone is unavailable: {timezone_name}") from exc
        current = now.astimezone(timezone) if now is not None else datetime.now(timezone)
    else:
        current = now if now is not None else datetime.now().astimezone()

    text = value.strip()
    lowered = text.lower()

    if lowered in {"today", "this day"}:
        return current.date(), current.date()
    if lowered == "yesterday":
        day = current.date() - timedelta(days=1)
        return day, day

    today = current.date()
    week_start = today - timedelta(days=today.weekday())
    if lowered == "this week":
        return week_start, today
    if lowered == "last week":
        return week_start - timedelta(days=7), week_start - timedelta(days=1)
    month_start = today.replace(day=1)
    if lowered == "this month":
        return month_start, today
    if lowered == "last month":
        previous_end = month_start - timedelta(days=1)
        return previous_end.replace(day=1), previous_end
    if lowered.startswith("since "):
        anchor = text[6:].strip()
        weekdays = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }
        if anchor.lower() in weekdays:
            start = today - timedelta(
                days=(today.weekday() - weekdays[anchor.lower()]) % 7
            )
        else:
            try:
                start = date.fromisoformat(anchor)
            except ValueError as exc:
                raise ValueError("since requires an ISO date or weekday name") from exc
        if start > today:
            raise ValueError("start date must not follow end date")
        return start, today

    match = _LAST_DAYS.fullmatch(text)
    if match:
        count = int(match.group(1))
        if count <= 0:
            raise ValueError("day count must be positive")
        end = current.date()
        return end - timedelta(days=count - 1), end

    match = _EXPLICIT_RANGE.fullmatch(text)
    if match:
        start = date.fromisoformat(match.group(1))
        end = date.fromisoformat(match.group(2))
        if start > end:
            raise ValueError("start date must not follow end date")
        return start, end

    try:
        day = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            "unsupported date range; use today, yesterday, last N days, "
            "YYYY-MM-DD, or YYYY-MM-DD to YYYY-MM-DD"
        ) from exc
    return day, day


def discover_entries(root: Path) -> list[dict[str, str]]:
    """Discover date named Markdown notes beneath the journal notes directory."""
    reject_symlink_components(root)
    notes = root / "notes"
    if notes.is_symlink() or not notes.is_dir() or not _inside(notes, root):
        return []
    entries: list[dict[str, str]] = []
    for path in notes.rglob("*.md"):
        if path.is_symlink() or not path.is_file() or not _inside(path, notes):
            continue
        match = _DATE_NAME.fullmatch(path.name)
        if not match:
            continue
        try:
            day = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        expected = notes / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.isoformat()}.md"
        if path.resolve() != expected.resolve():
            continue
        entries.append({"date": day.isoformat(), "path": str(path)})
    return sorted(entries, key=lambda item: (item["date"], item["path"]))


def _calendar_gaps(days: list[date], limit: int) -> tuple[list[str], int]:
    if len(days) < 2:
        return [], 0
    ordered = sorted(set(days))
    gaps: list[str] = []
    total = 0
    for previous, current in zip(ordered, ordered[1:]):
        count = (current - previous).days - 1
        if count <= 0:
            continue
        total += count
        cursor = previous + timedelta(days=1)
        while cursor < current and len(gaps) < limit:
            gaps.append(cursor.isoformat())
            cursor += timedelta(days=1)
    return gaps, total


def _line_value(text: str, label: str) -> str | None:
    prefix = f"{label}: "
    values = [line[len(prefix):] for line in text.splitlines() if line.startswith(prefix)]
    return values[0] if len(values) == 1 and values[0] else None


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _load_validator_module(path: Path) -> types.ModuleType:
    """Compile one anchored validator source snapshot with trusted import context."""
    source = _safe_files.safe_read_text(path.parent, path, max_bytes=1_000_000)
    name = f"my_journal_validator_{abs(hash(source))}"
    module = types.ModuleType(name)
    module.__file__ = str(_SAFE_FILES_PATH.parent / f".{name}.py")
    module.__package__ = ""
    sys.modules[name] = module
    try:
        exec(compile(source, str(path), "exec"), module.__dict__)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def validate_entry(
    path: Path,
    *,
    root: Path | None = None,
    validator_path: Path | None = None,
    _text: str | None = None,
) -> tuple[bool, list[str]]:
    """Validate provenance paths before delegating to the journal validator."""
    journal_root = root or path.expanduser().absolute().parents[3]
    if _text is None:
        try:
            text = _safe_files.safe_read_text(journal_root, path, max_bytes=_MAX_NOTE_BYTES)
        except (OSError, UnicodeError, ValueError) as exc:
            return False, [f"could not read journal entry: {exc}"]
    else:
        text = _text
    manifest_value = _line_value(text, "Evidence manifest")
    digest_value = _line_value(text, "Digest directory")
    errors: list[str] = []
    if not manifest_value:
        errors.append("missing unique Evidence manifest provenance line")
    if not digest_value:
        errors.append("missing unique Digest directory provenance line")
    if errors:
        return False, errors
    manifest_path = Path(manifest_value).expanduser()
    digest_dir = Path(digest_value).expanduser()
    evidence_root = journal_root / "evidence"
    if evidence_root.is_symlink() or not _inside(evidence_root, journal_root):
        errors.append("journal evidence root is unsafe")
    if not _inside(manifest_path, evidence_root):
        errors.append("evidence manifest is outside journal evidence directory")
    runs_root = journal_root / "runs"
    if runs_root.is_symlink() or not _inside(runs_root, journal_root):
        errors.append("journal runs root is unsafe")
    if not _inside(digest_dir, runs_root):
        errors.append("digest directory is outside journal runs directory")
    if errors:
        return False, errors

    configured_validator = validator_path
    if configured_validator is None:
        configured = os.environ.get("MY_JOURNAL_VALIDATOR")
        if configured:
            configured_validator = Path(configured).expanduser()
        else:
            hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
            configured_validator = (
                hermes_home
                / "skills"
                / "note-taking"
                / "my-journal"
                / "scripts"
                / "validate_journal.py"
            )
    if not configured_validator.is_file():
        return False, [f"journal validator not found: {configured_validator}"]
    if not manifest_path.is_file():
        return False, [f"evidence manifest not found: {manifest_path}"]
    if not digest_dir.is_dir():
        return False, [f"digest directory not found: {digest_dir}"]
    try:
        manifest = json.loads(
            _safe_files.safe_read_text(
                journal_root, manifest_path, max_bytes=_MAX_MANIFEST_BYTES
            )
        )
        if not isinstance(manifest, dict):
            return False, ["evidence manifest root must be an object"]
        if manifest.get("journal_date") != path.stem:
            return False, ["manifest journal date does not match note filename"]
        digest_texts: list[str] = []
        for item in sorted(digest_dir.glob("*.md")):
            if item.is_symlink() or not item.is_file() or not _inside(item, digest_dir):
                return False, [f"digest file is outside the declared digest directory: {item}"]
            digest_texts.append(
                _safe_files.safe_read_text(
                    journal_root, item, max_bytes=_MAX_DIGEST_BYTES
                )
            )
        validator = _load_validator_module(configured_validator)
        validation_errors: list[str] = []
        validation_errors.extend(validator.validate_manifest(manifest))
        validation_errors.extend(
            validator.validate_digest_bindings(manifest, digest_texts)
        )
        validation_errors.extend(
            validator.validate_note(
                manifest,
                text,
                session_evidence="\n".join(digest_texts),
                manifest_path=manifest_path,
                digest_dir=digest_dir,
            )
        )
        return not validation_errors, [str(error) for error in validation_errors]
    except Exception as exc:
        return False, [f"journal validation failed safely: {exc}"]


def validate_entry_content(
    path: Path,
    *,
    root: Path | None = None,
    validator_path: Path | None = None,
) -> tuple[bool, list[str], str]:
    """Validate and return the same in-memory note snapshot."""
    try:
        journal_root = root or path.expanduser().absolute().parents[3]
        text = _safe_files.safe_read_text(journal_root, path, max_bytes=_MAX_NOTE_BYTES)
    except (OSError, UnicodeError, ValueError) as exc:
        return False, [f"could not read journal entry: {exc}"], ""
    valid, errors = validate_entry(
        path,
        root=root,
        validator_path=validator_path,
        _text=text,
    )
    return valid, errors, text


def _bounded_errors(errors: list[Any], limit: int = 500) -> list[str]:
    return [str(error)[:limit] for error in errors[:5]]


def read_validated_entries(
    root: Path,
    start: date,
    end: date,
    *,
    validate_fn=None,
    max_total_chars: int = 200_000,
    max_records: int = 100,
) -> dict[str, Any]:
    """Read bounded entries only after validation succeeds."""
    if start > end:
        raise ValueError("start date must not follow end date")
    if max_total_chars <= 0 or max_records <= 0:
        raise ValueError("output limits must be positive")
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    rejected_count = 0
    accepted_count = 0
    used = 0
    for item in discover_entries(root):
        day = date.fromisoformat(item["date"])
        if day < start or day > end:
            continue
        path = Path(item["path"])
        if validate_fn is None:
            valid, errors, content = validate_entry_content(path, root=root)
        else:
            valid, errors = validate_fn(path)
            content = (
                _safe_files.safe_read_text(root, path, max_bytes=_MAX_NOTE_BYTES)
                if valid
                else ""
            )
        if not valid:
            rejected_count += 1
            if len(rejected) < max_records:
                rejected.append({**item, "errors": _bounded_errors(errors)})
            continue
        accepted_count += 1
        if len(accepted) >= max_records:
            rejected_count += 1
            if len(rejected) < max_records:
                rejected.append({**item, "errors": ["range output record limit reached"]})
            continue
        remaining = max_total_chars - used
        if remaining <= 0:
            rejected_count += 1
            if len(rejected) < max_records:
                rejected.append({**item, "errors": ["range output character limit reached"]})
            continue
        if len(content) > remaining:
            rejected_count += 1
            if len(rejected) < max_records:
                rejected.append({**item, "errors": ["entry exceeds remaining range output character limit"]})
            continue
        accepted.append({**item, "content": content})
        used += len(content)
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "entry_count": len(accepted),
        "validated_entry_count": accepted_count,
        "entries_truncated": accepted_count > len(accepted),
        "total_chars": used,
        "entries": accepted,
        "rejected_count": rejected_count,
        "rejected": rejected,
        "rejected_truncated": rejected_count > len(rejected),
    }


def validated_entry_dates(root: Path, start: date, end: date) -> set[str]:
    """Return canonical dates whose notes pass complete evidence validation."""
    if start > end:
        raise ValueError("start date must not follow end date")
    accepted: set[str] = set()
    for item in discover_entries(root):
        day = date.fromisoformat(item["date"])
        if day < start or day > end:
            continue
        valid, _, _ = validate_entry_content(Path(item["path"]), root=root)
        if valid:
            accepted.add(item["date"])
    return accepted


def journal_status(
    root: Path,
    *,
    max_entries: int = 100,
    max_gaps: int = 1000,
) -> dict[str, Any]:
    """Report existing entry coverage without opening unrelated Markdown files."""
    if max_entries < 0 or max_gaps < 0:
        raise ValueError("status limits must be nonnegative")
    entries = discover_entries(root)
    days = [date.fromisoformat(item["date"]) for item in entries]
    gaps, gap_count = _calendar_gaps(days, max_gaps)
    return {
        "journal_root": str(root),
        "entry_count": len(entries),
        "earliest_entry": entries[0]["date"] if entries else None,
        "latest_entry": entries[-1]["date"] if entries else None,
        "calendar_gap_count": gap_count,
        "calendar_gaps": gaps,
        "calendar_gaps_truncated": gap_count > len(gaps),
        "entries": entries[:max_entries],
        "entries_truncated": len(entries) > max_entries,
    }
