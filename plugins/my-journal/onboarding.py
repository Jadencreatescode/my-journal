"""Approval-bound guided first use for My Journal."""

from __future__ import annotations

import hashlib
import fcntl
import importlib.util
import json
import math
import os
import re
import secrets
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import _safe_files, validated_entry_dates


MAX_DATABASES = 128
MAX_DISCOVERY_ENTRIES = 1_024
DEFAULT_MAX_DATABASE_BYTES = 8 * 1024**3
DATABASE_APPROVAL_TIERS = (16 * 1024**3, 32 * 1024**3)
MAX_DATABASE_BYTES = 32 * 1024**3
MAX_SELECTED_MESSAGES = 100_000
MAX_SESSIONS = 10_000
MAX_ACTIVITY_DATES = 3_661
# This is the shipped per-day generation default, not a whole-history metadata ceiling.
PLANNING_MAX_SELECTED_MESSAGES = 25_000
MAX_RAW_IDENTIFIER_CHARS = 512
MAX_RAW_METADATA_CHARS = 512
MAX_RAW_TIMESTAMP_CHARS = 64
MAX_PROFILE_ALLOWLIST = 128
MAX_PLATFORM_ALLOWLIST = 256
MAX_EXCLUDED_SESSIONS = 10_000
DAILY_MESSAGE_TIERS = (25_000, 50_000, 100_000)
GUIDED_LIMITS = {
    "max_message_chars": 4000,
    "max_tool_chars": 1200,
    "max_selected_messages": 25000,
    "max_retained_chars": 4000000,
    "max_sessions": 2000,
    "packet_chunk_bytes": 120000,
    "max_packet_chunks": 64,
}

_CONFIG_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills" / "note-taking" / "my-journal" / "scripts" / "journal_config.py"
)
_config_spec = importlib.util.spec_from_file_location("my_journal_onboarding_config", _CONFIG_PATH)
if _config_spec is None or _config_spec.loader is None:
    raise RuntimeError(f"could not load journal configuration validator: {_CONFIG_PATH}")
_journal_config = importlib.util.module_from_spec(_config_spec)
sys.modules[_config_spec.name] = _journal_config
_config_spec.loader.exec_module(_journal_config)


def _bounded_values(
    value: list[str] | None, label: str, *, max_items: int, required: bool,
) -> tuple[str, ...]:
    if value is None:
        value = []
    if not isinstance(value, list) or (required and not value) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        requirement = "an explicit nonempty" if required else "a"
        raise ValueError(f"{label} must be {requirement} list of strings")
    if len(value) > max_items:
        raise ValueError(f"{label} item limit exceeded")
    if any(len(item.strip()) > MAX_RAW_METADATA_CHARS for item in value):
        raise ValueError(f"{label} value limit exceeded")
    normalized = tuple(sorted(set(item.strip() for item in value)))
    if len(normalized) != len(value):
        raise ValueError(f"{label} must not contain duplicates")
    return normalized


def _nonempty_allowlist(value: list[str], label: str) -> tuple[str, ...]:
    maximum = MAX_PROFILE_ALLOWLIST if label == "profiles" else MAX_PLATFORM_ALLOWLIST
    return _bounded_values(value, label, max_items=maximum, required=True)


def _timezone(value: str) -> ZoneInfo:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timezone must be a nonempty IANA timezone")
    try:
        return ZoneInfo(value.strip())
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc


def _privacy_mode(value: str, label: str, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{label} must equal " + " or ".join(sorted(allowed)))
    return value


def _candidate_sources(home: Path) -> list[tuple[str, Path]]:
    candidates = [("default", home / "state.db")]
    profiles_root = home / "profiles"
    if profiles_root.is_dir() and not profiles_root.is_symlink():
        with os.scandir(profiles_root) as entries:
            for count, entry in enumerate(entries, 1):
                if count > MAX_DISCOVERY_ENTRIES:
                    raise ValueError("discovery entry limit exceeded")
                if entry.is_dir(follow_symlinks=False):
                    candidates.append((entry.name, profiles_root / entry.name / "state.db"))
    return sorted(
        (profile, path) for profile, path in candidates
        if path.is_file() and not path.is_symlink()
    )


def _sources(home: Path, profiles: tuple[str, ...]) -> list[tuple[str, Path]]:
    wanted = set(profiles)
    sources = [item for item in _candidate_sources(home) if item[0] in wanted]
    if len(sources) > MAX_DATABASES:
        raise ValueError("database limit exceeded")
    return sources


def _database_confirmation(profile: str, tier: int) -> str:
    token = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:16]
    return f"APPROVE MY JOURNAL DATABASE TIER {tier // 1024**3} GIB FOR PROFILE {token}"


def _required_database_tier(size: int) -> int | None:
    return next((tier for tier in DATABASE_APPROVAL_TIERS if size <= tier), None)


def load_database_size_approvals(root: Path) -> dict[str, int]:
    trusted_root = Path(root).expanduser().absolute()
    try:
        raw = json.loads(_safe_files.safe_read_text(
            trusted_root, trusted_root / "database-size-approvals.json", max_bytes=100_000,
        ))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("database size approval receipt is malformed") from exc
    try:
        if not isinstance(raw, dict) or set(raw) != {"schema_version", "approvals"}:
            raise ValueError
        entries = raw["approvals"]
        if raw["schema_version"] != 1 or not isinstance(entries, dict) or len(entries) > MAX_DATABASES:
            raise ValueError
        approvals: dict[str, int] = {}
        for profile, entry in entries.items():
            if (
                not isinstance(profile, str) or not profile.strip() or len(profile) > MAX_RAW_METADATA_CHARS
                or not isinstance(entry, dict)
                or set(entry) != {"approved_max_bytes", "confirmation_sha256"}
            ):
                raise ValueError
            tier = entry["approved_max_bytes"]
            if isinstance(tier, bool) or tier not in DATABASE_APPROVAL_TIERS:
                raise ValueError
            expected_hash = hashlib.sha256(_database_confirmation(profile, tier).encode("utf-8")).hexdigest()
            if entry["confirmation_sha256"] != expected_hash:
                raise ValueError
            approvals[profile] = tier
        return approvals
    except (TypeError, ValueError) as exc:
        raise ValueError("database size approval receipt is malformed") from exc


def _enabled_config_database_approvals(root: Path) -> dict[str, int]:
    trusted_root = Path(root).expanduser().absolute()
    try:
        raw = json.loads(_safe_files.safe_read_text(
            trusted_root, trusted_root / "config.json", max_bytes=100_000,
        ))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("journal configuration is malformed") from exc
    config = _journal_config.validate_config_payload(raw)
    if not config.enabled:
        return {}
    return dict(config.database_size_approvals)


def _validated_database_approvals(root: Path) -> dict[str, int]:
    try:
        durable = load_database_size_approvals(root)
        malformed = False
    except ValueError:
        durable = {}
        malformed = True
    configured = _enabled_config_database_approvals(root)
    for profile, tier in configured.items():
        if tier > DEFAULT_MAX_DATABASE_BYTES and durable.get(profile) != tier:
            raise ValueError(
                f"database size approval evidence required for profile {profile}; repeat setup inventory"
            )
    if malformed:
        raise ValueError("database size approval receipt is malformed")
    return durable


def approve_database_size(
    *, home: Path, journal_root: Path, profile: str, confirmation: str,
) -> dict[str, Any]:
    normalized = _bounded_values([profile], "profile", max_items=1, required=True)[0]
    trusted_home = Path(home).expanduser().absolute()
    root = Path(journal_root).expanduser().absolute()
    sources = dict(_candidate_sources(trusted_home))
    if normalized not in sources:
        raise ValueError("profile database is unavailable")

    def current_requirement() -> tuple[int, int, str, bool]:
        descriptor = _safe_files.safe_open_regular_fd(trusted_home, sources[normalized])
        try:
            size = os.fstat(descriptor).st_size
        finally:
            os.close(descriptor)
        tier = _required_database_tier(size)
        if size > MAX_DATABASE_BYTES or tier is None:
            raise ValueError("database exceeds the absolute 32 GiB limit; bounded sharding is required")
        evidence_recovery = False
        try:
            raw_config = json.loads(_safe_files.safe_read_text(
                root, root / "config.json", max_bytes=100_000,
            ))
            enabled_config = _journal_config.validate_config_payload(raw_config)
            configured_tier = dict(enabled_config.database_size_approvals).get(
                normalized, DEFAULT_MAX_DATABASE_BYTES,
            )
            if enabled_config.enabled and normalized in enabled_config.profiles and configured_tier > DEFAULT_MAX_DATABASE_BYTES:
                try:
                    durable = load_database_size_approvals(root)
                except ValueError:
                    durable = {}
                if durable.get(normalized) != configured_tier:
                    tier = configured_tier
                    evidence_recovery = True
        except FileNotFoundError:
            pass
        phrase = _database_confirmation(normalized, tier)
        return size, tier, phrase, evidence_recovery

    size, tier, phrase, evidence_recovery = current_requirement()
    if size <= DEFAULT_MAX_DATABASE_BYTES and not evidence_recovery:
        raise ValueError("database does not require an adaptive size approval")
    if confirmation != phrase:
        raise ValueError("database size approval requires the exact confirmation phrase")
    _safe_files.safe_mkdir_tree(root.parent, root, mode=0o700)
    descriptor = _safe_files._open_directory(root)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        size, tier, phrase, evidence_recovery = current_requirement()
        if confirmation != phrase:
            raise ValueError("database size changed; use the newly required confirmation phrase")
        try:
            existing = load_database_size_approvals(root)
        except ValueError:
            existing = {}
        receipt_already_present = existing.get(normalized) == tier
        if not receipt_already_present:
            existing[normalized] = tier
            receipt = {
                "schema_version": 1,
                "approvals": {
                    name: {
                        "approved_max_bytes": approved,
                        "confirmation_sha256": hashlib.sha256(
                            _database_confirmation(name, approved).encode("utf-8")
                        ).hexdigest(),
                    }
                    for name, approved in sorted(existing.items())
                },
            }
            _safe_files.safe_atomic_write_text(
                root, root / "database-size-approvals.json",
                json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            )
        config_updated = _update_enabled_database_tier(root, normalized, tier)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    return {
        "ok": True,
        "profile": normalized,
        "database_bytes": size,
        "approved_tier_gib": tier // 1024**3,
        "approved_max_bytes": tier,
        "receipt_already_present": receipt_already_present,
        "config_updated": config_updated,
    }


def discover_setup_inventory(*, home: Path, journal_root: Path | None = None) -> dict[str, Any]:
    """List available profile and platform labels without selecting message rows."""
    trusted_home = Path(home).expanduser().absolute()
    sources = _candidate_sources(trusted_home)
    if len(sources) > MAX_DATABASES:
        raise ValueError("database limit exceeded")
    profiles: list[str] = []
    ready_profiles: list[str] = []
    blocked_databases: list[dict[str, Any]] = []
    platforms: set[str] = set()
    receipt_malformed = False
    configured_approvals: dict[str, int] = {}
    if journal_root is not None:
        try:
            approvals = load_database_size_approvals(journal_root)
        except ValueError:
            approvals = {}
            receipt_malformed = True
        configured_approvals = _enabled_config_database_approvals(journal_root)
        if receipt_malformed and not configured_approvals:
            raise ValueError("database size approval receipt is malformed")
    else:
        approvals = {}
    for profile, path in sources:
        descriptor = _safe_files.safe_open_regular_fd(trusted_home, path)
        try:
            metadata = os.fstat(descriptor)
            profiles.append(profile)
            approved_limit = approvals.get(profile, DEFAULT_MAX_DATABASE_BYTES)
            if metadata.st_size > MAX_DATABASE_BYTES:
                blocked_databases.append({
                    "profile": profile,
                    "database_bytes": metadata.st_size,
                    "reason": "absolute_database_limit_exceeded",
                    "absolute_max_gib": MAX_DATABASE_BYTES // 1024**3,
                })
                continue
            configured_limit = configured_approvals.get(profile, DEFAULT_MAX_DATABASE_BYTES)
            if configured_limit > DEFAULT_MAX_DATABASE_BYTES and approvals.get(profile) != configured_limit:
                blocked_databases.append({
                    "profile": profile,
                    "database_bytes": metadata.st_size,
                    "reason": "database_size_approval_required",
                    "current_approved_tier_gib": approvals.get(
                        profile, DEFAULT_MAX_DATABASE_BYTES,
                    ) // 1024**3,
                    "required_tier_gib": configured_limit // 1024**3,
                    "confirmation_phrase": _database_confirmation(profile, configured_limit),
                    "evidence_missing": True,
                })
                continue
            if metadata.st_size > approved_limit:
                required_tier = _required_database_tier(metadata.st_size)
                if required_tier is None:
                    raise ValueError("database exceeds the absolute database limit")
                blocked_databases.append({
                    "profile": profile,
                    "database_bytes": metadata.st_size,
                    "reason": "database_size_approval_required",
                    "current_approved_tier_gib": approved_limit // 1024**3,
                    "required_tier_gib": required_tier // 1024**3,
                    "confirmation_phrase": _database_confirmation(profile, required_tier),
                })
                continue
            con = sqlite3.connect(_safe_files.descriptor_sqlite_uri(descriptor), uri=True)
            try:
                rows = con.execute(
                    "select distinct substr(cast(source as text),1,513), "
                    "length(cast(source as text)) from sessions order by source"
                )
                for source, source_length in rows:
                    if source_length is None or int(source_length) > MAX_RAW_METADATA_CHARS:
                        raise ValueError("raw platform metadata limit exceeded")
                    platforms.add(str(source))
            finally:
                con.close()
            ready_profiles.append(profile)
        finally:
            os.close(descriptor)
    return {
        "ok": True,
        "profiles": sorted(profiles),
        "ready_profiles": sorted(ready_profiles),
        "platforms": sorted(platforms),
        "database_count": len(sources),
        "blocked_databases": blocked_databases,
        "message_bodies_read": False,
    }


def discover_activity_metadata(
    *, home: Path, profiles: list[str], platforms: list[str], timezone_name: str,
    excluded_session_ids: list[str] | None = None,
    database_size_approvals: dict[str, int] | None = None,
    activity_date_filter: str | None = None,
) -> dict[str, Any]:
    """Read eligible timestamp metadata only; message bodies are never selected."""
    selected_profiles = _nonempty_allowlist(profiles, "profiles")
    selected_platforms = _nonempty_allowlist(platforms, "platforms")
    zone = _timezone(timezone_name)
    timestamp_window: tuple[float, float] | None = None
    if activity_date_filter is not None:
        try:
            target_day = date.fromisoformat(activity_date_filter)
        except (TypeError, ValueError) as exc:
            raise ValueError("journal_date must be a canonical ISO date") from exc
        if target_day.isoformat() != activity_date_filter:
            raise ValueError("journal_date must be a canonical ISO date")
        start = datetime.combine(target_day, time.min, tzinfo=zone)
        end = datetime.combine(target_day + timedelta(days=1), time.min, tzinfo=zone)
        timestamp_window = (start.timestamp(), end.timestamp())
    excluded = set(_bounded_values(
        excluded_session_ids, "excluded_session_ids",
        max_items=MAX_EXCLUDED_SESSIONS, required=False,
    ))
    approvals = {} if database_size_approvals is None else database_size_approvals
    if not isinstance(approvals, dict) or any(
        not isinstance(profile, str) or isinstance(tier, bool) or tier not in DATABASE_APPROVAL_TIERS
        for profile, tier in approvals.items()
    ):
        raise ValueError("database size approvals are malformed")
    workload_count = 0
    date_counts: Counter[str] = Counter()
    session_ids: set[tuple[str, str]] = set()
    fingerprint = hashlib.sha256()
    sources = _sources(Path(home).expanduser().absolute(), selected_profiles)
    for profile, path in sources:
        descriptor = _safe_files.safe_open_regular_fd(Path(home).expanduser().absolute(), path)
        try:
            stat_result = os.fstat(descriptor)
            approved_limit = approvals.get(profile, DEFAULT_MAX_DATABASE_BYTES)
            if stat_result.st_size > approved_limit:
                raise ValueError(
                    f"database size approval required for profile {profile}: "
                    f"{stat_result.st_size} bytes exceeds {approved_limit} approved"
                )
            con = sqlite3.connect(_safe_files.descriptor_sqlite_uri(descriptor), uri=True)
            try:
                placeholders = ",".join("?" for _ in selected_platforms)
                where = [
                    f"s.source in ({placeholders})",
                    "lower(coalesce(m.role,'')) not in ('system','developer')",
                ]
                params: list[Any] = list(selected_platforms)
                if timestamp_window is not None:
                    where.extend(("m.timestamp >= ?", "m.timestamp < ?"))
                    params.extend(timestamp_window)
                if excluded:
                    values = sorted(excluded)
                    where.append(f"m.session_id not in ({','.join('?' for _ in values)})")
                    params.extend(values)
                rows = con.execute(
                    "select "
                    "substr(cast(m.timestamp as text),1,65), length(cast(m.timestamp as text)), "
                    "substr(cast(m.session_id as text),1,513), length(cast(m.session_id as text)), "
                    "substr(cast(s.source as text),1,513), length(cast(s.source as text)), "
                    "substr(coalesce(cast(m.role as text),''),1,513), "
                    "length(coalesce(cast(m.role as text),'')) "
                    "from messages m "
                    "join sessions s on s.id=m.session_id where " + " and ".join(where)
                    + " order by m.timestamp, m.session_id, m.id", params
                )
                for timestamp_text, timestamp_length, session_id, session_length, source, source_length, role, role_length in rows:
                    if timestamp_length is None or int(timestamp_length) > MAX_RAW_TIMESTAMP_CHARS:
                        raise ValueError("raw timestamp metadata limit exceeded")
                    if session_length is None or int(session_length) > MAX_RAW_IDENTIFIER_CHARS:
                        raise ValueError("raw session identifier limit exceeded")
                    if source_length is None or int(source_length) > MAX_RAW_METADATA_CHARS:
                        raise ValueError("raw platform metadata limit exceeded")
                    if role_length is None or int(role_length) > MAX_RAW_METADATA_CHARS:
                        raise ValueError("raw role metadata limit exceeded")
                    timestamp = float(timestamp_text)
                    if not math.isfinite(timestamp):
                        raise ValueError("message timestamp must be finite")
                    session_id = str(session_id)
                    source = str(source)
                    role = str(role)
                    try:
                        activity_date = datetime.fromtimestamp(timestamp, zone).date().isoformat()
                    except (OSError, OverflowError, ValueError) as exc:
                        raise ValueError("message timestamp is outside the supported range") from exc
                    workload_count += 1
                    date_counts[activity_date] += 1
                    session_ids.add((profile, session_id))
                    fingerprint.update(
                        json.dumps(
                            (profile, source, session_id, timestamp, role),
                            separators=(",", ":"), ensure_ascii=True,
                        ).encode("utf-8")
                    )
                    fingerprint.update(b"\n")
                    if len(session_ids) > MAX_SESSIONS:
                        raise ValueError("eligible history exceeds session compiled ceiling")
            finally:
                con.close()
        finally:
            os.close(descriptor)
    dates = sorted(date_counts)
    if len(dates) > MAX_ACTIVITY_DATES:
        raise ValueError("eligible history exceeds activity date compiled ceiling")
    return {
        "activity_dates": dates,
        "earliest_activity_date": dates[0] if dates else None,
        "latest_activity_date": dates[-1] if dates else None,
        "workload_count": workload_count,
        "session_count": len(session_ids),
        "database_count": len(sources),
        "activity_date_counts": dict(sorted(date_counts.items())),
        "metadata_fingerprint": fingerprint.hexdigest(),
    }


def _load_enabled_config(root: Path) -> tuple[dict[str, Any], Any]:
    try:
        raw = json.loads(_safe_files.safe_read_text(
            root, root / "config.json", max_bytes=100_000,
        ))
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("enabled journal configuration is unavailable or malformed") from exc
    try:
        validated = _journal_config.validate_config_payload(raw)
        validated.require_enabled()
    except ValueError as exc:
        raise ValueError("enabled journal configuration is unavailable or malformed") from exc
    return raw, validated


def _update_enabled_database_tier(root: Path, profile: str, tier: int) -> bool:
    try:
        raw = json.loads(_safe_files.safe_read_text(
            root, root / "config.json", max_bytes=100_000,
        ))
    except FileNotFoundError:
        return False
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("journal configuration is malformed; database approval receipt is recoverable") from exc
    try:
        validated = _journal_config.validate_config_payload(raw)
    except ValueError as exc:
        raise ValueError("journal configuration is malformed; database approval receipt is recoverable") from exc
    if not validated.enabled or profile not in validated.profiles:
        return False
    approvals = dict(validated.database_size_approvals)
    if approvals.get(profile, DEFAULT_MAX_DATABASE_BYTES) >= tier:
        return False
    approvals[profile] = tier
    updated = json.loads(json.dumps(raw))
    updated["database_size_approvals"] = dict(sorted(approvals.items()))
    _journal_config.validate_config_payload(updated).require_enabled()
    _safe_files.safe_atomic_write_text(
        root, root / "config.json", json.dumps(updated, indent=2, sort_keys=True) + "\n",
    )
    return True


def _daily_confirmation(journal_date: str, tier: int) -> str:
    return f"APPROVE MY JOURNAL DAILY TIER {tier} TRIGGERED BY DATE {journal_date}"


def _selected_database_capacity(root: Path, home: Path, config: Any) -> dict[str, Any] | None:
    sources = dict(_candidate_sources(Path(home).expanduser().absolute()))
    approvals = dict(config.database_size_approvals)
    try:
        durable = load_database_size_approvals(root)
    except ValueError:
        durable = {}
    for profile in sorted(config.profiles):
        path = sources.get(profile)
        if path is None:
            continue
        descriptor = _safe_files.safe_open_regular_fd(Path(home).expanduser().absolute(), path)
        try:
            size = os.fstat(descriptor).st_size
        finally:
            os.close(descriptor)
        approved = approvals.get(profile, DEFAULT_MAX_DATABASE_BYTES)
        if size > MAX_DATABASE_BYTES:
            return {
                "ok": False,
                "reason": "absolute_database_limit_exceeded",
                "profile": profile,
                "database_bytes": size,
                "approved_tier_gib": approved // 1024**3,
                "absolute_max_gib": MAX_DATABASE_BYTES // 1024**3,
            }
        if approved > DEFAULT_MAX_DATABASE_BYTES and durable.get(profile) != approved:
            return {
                "ok": False,
                "reason": "database_size_approval_required",
                "profile": profile,
                "database_bytes": size,
                "approved_tier_gib": durable.get(profile, DEFAULT_MAX_DATABASE_BYTES) // 1024**3,
                "required_tier_gib": approved // 1024**3,
                "confirmation_phrase": _database_confirmation(profile, approved),
                "evidence_missing": True,
            }
        if size <= approved:
            continue
        required = _required_database_tier(size)
        if required is None:
            return {
                "ok": False,
                "reason": "absolute_database_limit_exceeded",
                "profile": profile,
                "database_bytes": size,
                "approved_tier_gib": approved // 1024**3,
                "absolute_max_gib": MAX_DATABASE_BYTES // 1024**3,
            }
        return {
            "ok": False,
            "reason": "database_size_approval_required",
            "profile": profile,
            "database_bytes": size,
            "approved_tier_gib": approved // 1024**3,
            "required_tier_gib": required // 1024**3,
            "confirmation_phrase": _database_confirmation(profile, required),
        }
    return None


def _daily_receipt_supports_tier(root: Path, tier: int, config: Any) -> bool:
    if tier == DAILY_MESSAGE_TIERS[0]:
        return True
    try:
        receipt = json.loads(_safe_files.safe_read_text(
            root, root / "daily-workload-approval.json", max_bytes=100_000,
        ))
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema_version") != 1
            or receipt.get("approved_tier") != tier
            or receipt.get("configuration_sha256") != config.daily_authorization_sha256()
        ):
            return False
        confirmation_hash = receipt.get("confirmation_sha256")
        if not isinstance(confirmation_hash, str) or re.fullmatch(r"[0-9a-f]{64}", confirmation_hash) is None:
            return False
        if receipt.get("approval_kind") == "targeted_tier":
            journal_date = receipt.get("journal_date")
            return (
                isinstance(journal_date, str)
                and date.fromisoformat(journal_date).isoformat() == journal_date
                and hashlib.sha256(_daily_confirmation(journal_date, tier).encode("utf-8")).hexdigest()
                == confirmation_hash
            )
        if receipt.get("approval_kind") == "guided_setup":
            plan_id = receipt.get("plan_id")
            if not isinstance(plan_id, str) or re.fullmatch(r"[0-9a-f]{32}", plan_id) is None:
                return False
            plan = json.loads(_safe_files.safe_read_text(
                root, root / "approval-plans" / f"{plan_id}.json", max_bytes=100_000,
            ))
            return (
                isinstance(plan, dict)
                and plan.get("plan_id") == plan_id
                and plan.get("confirmation_phrase") == _plan_confirmation(plan)
                and plan.get("config_activated") is True
                and plan.get("limits", {}).get("max_selected_messages") == tier
                and hashlib.sha256(plan["confirmation_phrase"].encode("utf-8")).hexdigest()
                == confirmation_hash
            )
    except (AttributeError, FileNotFoundError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return False


def _database_receipts_support_config(root: Path, approvals: dict[str, int]) -> bool:
    if not approvals:
        return True
    try:
        durable = load_database_size_approvals(root)
    except (OSError, ValueError):
        return False
    return all(durable.get(profile) == tier for profile, tier in approvals.items())


def _pending_daily_tier_receipt(
    root: Path, raw: dict[str, Any], current_tier: int,
) -> dict[str, Any] | None:
    try:
        receipt = json.loads(_safe_files.safe_read_text(
            root, root / "daily-workload-approval.json", max_bytes=100_000,
        ))
        tier = receipt.get("approved_tier")
        journal_date = receipt.get("journal_date")
        if (
            receipt.get("schema_version") != 1
            or receipt.get("approval_kind") != "targeted_tier"
            or tier not in DAILY_MESSAGE_TIERS
            or tier <= current_tier
            or not isinstance(journal_date, str)
            or date.fromisoformat(journal_date).isoformat() != journal_date
        ):
            return None
        phrase = _daily_confirmation(journal_date, tier)
        if hashlib.sha256(phrase.encode("utf-8")).hexdigest() != receipt.get("confirmation_sha256"):
            return None
        proposed = json.loads(json.dumps(raw))
        proposed["limits"]["max_selected_messages"] = tier
        proposed_config = _journal_config.validate_config_payload(proposed)
        if proposed_config.daily_authorization_sha256() != receipt.get("configuration_sha256"):
            return None
        return {"tier": tier, "journal_date": journal_date, "confirmation_phrase": phrase}
    except (AttributeError, FileNotFoundError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _activated_plan_config_is_compatible(
    root: Path, current_raw: dict[str, Any], expected_raw: dict[str, Any],
) -> bool:
    try:
        current = _journal_config.validate_config_payload(current_raw)
        current.require_enabled()
        current_cmp = json.loads(json.dumps(current_raw))
        expected_cmp = json.loads(json.dumps(expected_raw))
        current_daily = current_cmp["limits"].pop("max_selected_messages")
        expected_daily = expected_cmp["limits"].pop("max_selected_messages")
        current_databases = current_cmp.pop("database_size_approvals")
        expected_databases = expected_cmp.pop("database_size_approvals")
        if current_cmp != expected_cmp or current_daily < expected_daily:
            return False
        if any(current_databases.get(profile, 0) < tier for profile, tier in expected_databases.items()):
            return False
        if not _database_receipts_support_config(root, current_databases):
            return False
        if current_daily > expected_daily and not _daily_receipt_supports_tier(root, current_daily, current):
            return False
        return True
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def inspect_daily_workload_capacity(
    *, home: Path, journal_root: Path, journal_date: str,
) -> dict[str, Any]:
    root = Path(journal_root).expanduser().absolute()
    raw, config = _load_enabled_config(root)
    config_digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    database_block = _selected_database_capacity(root, home, config)
    if database_block is not None:
        return database_block
    try:
        metadata = discover_activity_metadata(
            home=home,
            profiles=sorted(config.profiles),
            platforms=sorted(config.platforms),
            timezone_name=config.timezone,
            excluded_session_ids=sorted(config.excluded_session_ids),
            database_size_approvals=dict(config.database_size_approvals),
            activity_date_filter=journal_date,
        )
    except ValueError:
        database_block = _selected_database_capacity(root, home, config)
        if database_block is not None:
            return database_block
        raise
    count = metadata["activity_date_counts"].get(journal_date, 0)
    current = config.max_selected_messages
    pending = _pending_daily_tier_receipt(root, raw, current)
    if pending is not None and pending["journal_date"] == journal_date:
        return {
            "ok": False,
            "reason": "daily_workload_approval_interrupted",
            "journal_date": journal_date,
            "eligible_message_count": count,
            "approved_tier": current,
            "required_tier": pending["tier"],
            "configuration_sha256": config_digest,
            "confirmation_phrase": pending["confirmation_phrase"],
        }
    if not _daily_receipt_supports_tier(root, current, config):
        return {
            "ok": False,
            "reason": "daily_workload_evidence_missing",
            "journal_date": journal_date,
            "eligible_message_count": count,
            "approved_tier": current,
            "required_tier": current,
            "configuration_sha256": config_digest,
            "confirmation_phrase": _daily_confirmation(journal_date, current),
        }
    if count <= current:
        return {
            "ok": True,
            "journal_date": journal_date,
            "eligible_message_count": count,
            "approved_tier": current,
            "configuration_sha256": config_digest,
        }
    required = next((tier for tier in DAILY_MESSAGE_TIERS if count <= tier and tier > current), None)
    response: dict[str, Any] = {
        "ok": False,
        "journal_date": journal_date,
        "eligible_message_count": count,
        "approved_tier": current,
        "configuration_sha256": config_digest,
    }
    if required is None:
        response.update({
            "reason": "absolute_daily_message_limit_exceeded",
            "absolute_tier": DAILY_MESSAGE_TIERS[-1],
        })
    else:
        response.update({
            "reason": "daily_workload_approval_required",
            "required_tier": required,
            "confirmation_phrase": _daily_confirmation(journal_date, required),
        })
    return response


def approve_daily_workload(
    *, home: Path, journal_root: Path, journal_date: str, confirmation: str,
) -> dict[str, Any]:
    first = inspect_daily_workload_capacity(
        home=home, journal_root=journal_root, journal_date=journal_date,
    )
    if first["ok"]:
        raise ValueError("daily workload already fits the approved tier")
    if first.get("reason") not in {
        "daily_workload_approval_required", "daily_workload_evidence_missing",
        "daily_workload_approval_interrupted",
        "absolute_daily_message_limit_exceeded",
    }:
        raise ValueError("database capacity must be resolved before daily workload approval")
    if first.get("required_tier") is None:
        raise ValueError("daily workload exceeds the absolute 100000 message ceiling")
    if confirmation != first["confirmation_phrase"]:
        raise ValueError("daily workload approval requires the exact confirmation phrase")
    root = Path(journal_root).expanduser().absolute()
    descriptor = _safe_files._open_directory(root)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        current = inspect_daily_workload_capacity(
            home=home, journal_root=root, journal_date=journal_date,
        )
        if current.get("confirmation_phrase") != confirmation:
            raise ValueError("daily workload changed; use the newly required confirmation phrase")
        tier = current["required_tier"]
        raw, validated = _load_enabled_config(root)
        loaded_digest = hashlib.sha256(
            json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if loaded_digest != current["configuration_sha256"]:
            raise ValueError("journal configuration changed during daily workload approval; retry")
        evidence_recovery = current.get("reason") == "daily_workload_evidence_missing"
        if validated.max_selected_messages > tier:
            raise ValueError("daily workload tier is already active")
        if validated.max_selected_messages == tier and not evidence_recovery:
            raise ValueError("daily workload tier is already active")
        config_updated = validated.max_selected_messages < tier
        if config_updated:
            updated = json.loads(json.dumps(raw))
            updated["limits"]["max_selected_messages"] = tier
            approved_config = _journal_config.validate_config_payload(updated)
            approved_config.require_enabled()
        else:
            updated = raw
            approved_config = validated
        receipt = {
            "schema_version": 1,
            "approval_kind": "targeted_tier",
            "journal_date": journal_date,
            "approved_tier": tier,
            "eligible_message_count": current["eligible_message_count"],
            "confirmation_sha256": hashlib.sha256(confirmation.encode("utf-8")).hexdigest(),
            "configuration_sha256": approved_config.daily_authorization_sha256(),
        }
        _safe_files.safe_atomic_write_text(
            root, root / "daily-workload-approval.json",
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        )
        latest_raw, _ = _load_enabled_config(root)
        latest_digest = hashlib.sha256(
            json.dumps(latest_raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if latest_digest != loaded_digest:
            raise ValueError("journal configuration changed during daily workload approval; retry")
        if config_updated:
            _safe_files.safe_atomic_write_text(
                root, root / "config.json", json.dumps(updated, indent=2, sort_keys=True) + "\n",
            )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    return {
        "ok": True,
        "journal_date": journal_date,
        "eligible_message_count": current["eligible_message_count"],
        "approved_tier": tier,
        "config_updated": config_updated,
        "evidence_recovered": evidence_recovery,
        "retry_ready": True,
    }


def _plan_confirmation(plan: dict[str, Any]) -> str:
    immutable = {
        key: value for key, value in plan.items()
        if key not in {"confirmation_phrase", "completed_dates", "config_activated"}
    }
    digest = hashlib.sha256(
        json.dumps(immutable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"ENABLE MY JOURNAL PLAN {plan['plan_id']} SHA256 {digest}"


def plan_guided_setup(
    *, home: Path, journal_root: Path, profiles: list[str], platforms: list[str],
    timezone_name: str, excluded_session_ids: list[str] | None = None,
    start_date: str | None = None, end_date: str | None = None,
    pii_mode: str = "mask", entropy_mode: str = "report",
) -> dict[str, Any]:
    pii_mode = _privacy_mode(pii_mode, "pii_mode", {"mask", "preserve"})
    entropy_mode = _privacy_mode(entropy_mode, "entropy_mode", {"report", "redact", "off"})
    root = Path(journal_root).expanduser().absolute()
    size_approvals = _validated_database_approvals(root)
    metadata = discover_activity_metadata(
        home=home, profiles=profiles, platforms=platforms,
        timezone_name=timezone_name, excluded_session_ids=excluded_session_ids,
        database_size_approvals=size_approvals,
    )
    if not metadata["activity_dates"]:
        return {"ok": False, "reason": "no_eligible_activity", **metadata}
    first = date.fromisoformat(start_date) if start_date else date.fromisoformat(metadata["earliest_activity_date"])
    last = date.fromisoformat(end_date) if end_date else date.fromisoformat(metadata["latest_activity_date"])
    if first > last:
        raise ValueError("start_date must not follow end_date")
    selected_dates = [value for value in metadata["activity_dates"] if first <= date.fromisoformat(value) <= last]
    if not selected_dates:
        raise ValueError("date overrides select no eligible activity")
    workload_count = sum(metadata["activity_date_counts"][value] for value in selected_dates)
    maximum_daily_message_count = max(metadata["activity_date_counts"][value] for value in selected_dates)
    selected_message_tier = next(
        (tier for tier in DAILY_MESSAGE_TIERS if maximum_daily_message_count <= tier), None
    )
    if selected_message_tier is None:
        raise ValueError("an activity date exceeds the absolute daily message ceiling")
    plan_limits = dict(GUIDED_LIMITS)
    plan_limits["max_selected_messages"] = selected_message_tier

    existing = sorted(validated_entry_dates(root, first, last) & set(selected_dates))
    missing = [value for value in selected_dates if value not in set(existing)]
    normalized_profiles = list(_nonempty_allowlist(profiles, "profiles"))
    normalized_platforms = list(_nonempty_allowlist(platforms, "platforms"))
    exclusions = list(_bounded_values(
        excluded_session_ids, "excluded_session_ids",
        max_items=MAX_EXCLUDED_SESSIONS, required=False,
    ))
    plan_id = secrets.token_hex(16)
    plan = {
        "schema_version": 1,
        "plan_id": plan_id,
        "confirmation_phrase": "",
        "hermes_home": str(Path(home).expanduser().absolute()),
        "timezone": timezone_name.strip(),
        "profiles": normalized_profiles,
        "platforms": normalized_platforms,
        "excluded_session_ids": exclusions,
        "database_size_approvals": {
            profile: tier for profile, tier in size_approvals.items()
            if profile in normalized_profiles
        },
        "start_date": first.isoformat(),
        "end_date": last.isoformat(),
        "activity_dates": selected_dates,
        "existing_dates": existing,
        "missing_dates": missing,
        "workload_count": workload_count,
        "maximum_daily_message_count": maximum_daily_message_count,
        "metadata_fingerprint": metadata["metadata_fingerprint"],
        "privacy": {
            "redact_secrets": True,
            "pii_mode": pii_mode,
            "entropy_mode": entropy_mode,
        },
        "completed_dates": [],
        "config_activated": False,
        "limits": plan_limits,
    }
    plan["confirmation_phrase"] = _plan_confirmation(plan)
    _safe_files.safe_mkdir_tree(root.parent, root, mode=0o700)
    plans = root / "approval-plans"
    _safe_files.safe_mkdir_tree(root, plans, mode=0o700)
    plan_path = plans / f"{plan_id}.json"
    _safe_files.safe_atomic_write_text(root, plan_path, json.dumps(plan, indent=2, sort_keys=True) + "\n")
    return {"ok": True, "plan_path": str(plan_path), **plan}


def _validate_date_list(value: Any, *, required: bool) -> list[str]:
    if not isinstance(value, list) or (required and not value) or len(value) > MAX_ACTIVITY_DATES:
        raise ValueError
    if any(
        not isinstance(item, str)
        or date.fromisoformat(item).isoformat() != item
        for item in value
    ):
        raise ValueError
    if value != sorted(set(value)):
        raise ValueError
    return value


def _validate_plan(value: dict[str, Any], plan_id: str) -> dict[str, Any]:
    expected_keys = {
        "schema_version", "plan_id", "confirmation_phrase", "hermes_home",
        "timezone", "profiles", "platforms", "excluded_session_ids",
        "database_size_approvals",
        "start_date", "end_date", "activity_dates", "existing_dates",
        "missing_dates", "workload_count", "maximum_daily_message_count",
        "metadata_fingerprint", "privacy",
        "completed_dates", "config_activated", "limits",
    }
    try:
        if set(value) != expected_keys or value["schema_version"] != 1:
            raise ValueError
        if value["plan_id"] != plan_id or value["confirmation_phrase"] != _plan_confirmation(value):
            raise ValueError
        home = value["hermes_home"]
        if (
            not isinstance(home, str) or not home or len(home) > 4096
            or "\x00" in home or not Path(home).is_absolute()
        ):
            raise ValueError
        _timezone(value["timezone"])
        if list(_nonempty_allowlist(value["profiles"], "profiles")) != value["profiles"]:
            raise ValueError
        if list(_nonempty_allowlist(value["platforms"], "platforms")) != value["platforms"]:
            raise ValueError
        exclusions = list(_bounded_values(
            value["excluded_session_ids"], "excluded_session_ids",
            max_items=MAX_EXCLUDED_SESSIONS, required=False,
        ))
        if exclusions != value["excluded_session_ids"]:
            raise ValueError
        approvals = value["database_size_approvals"]
        if (
            not isinstance(approvals, dict) or len(approvals) > MAX_DATABASES
            or any(
                profile not in value["profiles"] or isinstance(tier, bool)
                or tier not in DATABASE_APPROVAL_TIERS
                for profile, tier in approvals.items()
            )
        ):
            raise ValueError
        start = date.fromisoformat(value["start_date"])
        end = date.fromisoformat(value["end_date"])
        if start.isoformat() != value["start_date"] or end.isoformat() != value["end_date"] or start > end:
            raise ValueError
        activity = _validate_date_list(value["activity_dates"], required=True)
        existing = _validate_date_list(value["existing_dates"], required=False)
        missing = _validate_date_list(value["missing_dates"], required=False)
        completed = _validate_date_list(value["completed_dates"], required=False)
        activity_set = set(activity)
        if (
            set(existing) & set(missing)
            or set(existing) | set(missing) != activity_set
            or not set(completed) <= activity_set
            or any(not (start <= date.fromisoformat(item) <= end) for item in activity)
        ):
            raise ValueError
        workload = value["workload_count"]
        if isinstance(workload, bool) or not isinstance(workload, int) or workload < len(activity):
            raise ValueError
        maximum_daily = value["maximum_daily_message_count"]
        if (
            isinstance(maximum_daily, bool) or not isinstance(maximum_daily, int)
            or maximum_daily <= 0 or maximum_daily > workload
        ):
            raise ValueError
        if re.fullmatch(r"[0-9a-f]{64}", value["metadata_fingerprint"]) is None:
            raise ValueError
        privacy = value["privacy"]
        if not isinstance(privacy, dict) or set(privacy) != {"redact_secrets", "pii_mode", "entropy_mode"}:
            raise ValueError
        if privacy["redact_secrets"] is not True:
            raise ValueError
        _privacy_mode(privacy["pii_mode"], "pii_mode", {"mask", "preserve"})
        _privacy_mode(privacy["entropy_mode"], "entropy_mode", {"report", "redact", "off"})
        limits = value["limits"]
        expected_limits = dict(GUIDED_LIMITS)
        if not isinstance(limits, dict) or set(limits) != set(expected_limits):
            raise ValueError
        selected_tier = limits.get("max_selected_messages")
        if isinstance(selected_tier, bool) or not isinstance(selected_tier, int):
            raise ValueError
        expected_limits["max_selected_messages"] = selected_tier
        if (
            selected_tier not in DAILY_MESSAGE_TIERS or limits != expected_limits
            or maximum_daily > selected_tier
            or not isinstance(value["config_activated"], bool)
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("approval plan is malformed") from exc
    return value


def _load_plan(root: Path, plan_id: str) -> dict[str, Any]:
    if not isinstance(plan_id, str) or re.fullmatch(r"[0-9a-f]{32}", plan_id) is None:
        raise ValueError("plan_id must be 32 lowercase hexadecimal characters")
    try:
        value = json.loads(
            _safe_files.safe_read_text(root, root / "approval-plans" / f"{plan_id}.json", max_bytes=100_000)
        )
        if not isinstance(value, dict):
            raise ValueError
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("approval plan is malformed") from exc
    return _validate_plan(value, plan_id)


def _planned_config(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "enabled": True,
        "timezone": plan["timezone"],
        "profiles": plan["profiles"],
        "platforms": plan["platforms"],
        "excluded_session_ids": plan["excluded_session_ids"],
        "database_size_approvals": plan["database_size_approvals"],
        "privacy": plan["privacy"],
        "limits": plan["limits"],
    }


def approve_guided_setup(
    *, journal_root: Path, plan_id: str, confirmation: str,
    generation_fn=None,
) -> dict[str, Any]:
    root = Path(journal_root).expanduser().absolute()
    root_descriptor = _safe_files._open_directory(root)
    fcntl.flock(root_descriptor, fcntl.LOCK_EX)
    try:
        plan = _load_plan(root, plan_id)
        if confirmation != plan.get("confirmation_phrase"):
            raise ValueError("approval requires the exact confirmation phrase")
        current = discover_activity_metadata(
            home=Path(plan["hermes_home"]),
            profiles=plan["profiles"],
            platforms=plan["platforms"],
            timezone_name=plan["timezone"],
            excluded_session_ids=plan["excluded_session_ids"],
            database_size_approvals=plan["database_size_approvals"],
        )
        if current["metadata_fingerprint"] != plan.get("metadata_fingerprint"):
            raise ValueError("retained conversation source metadata changed after planning; create a new plan")
        config = _planned_config(plan)
        if not _database_receipts_support_config(root, config["database_size_approvals"]):
            raise ValueError("configured database tier lacks matching durable approval evidence")
        was_activated = bool(plan.get("config_activated"))
        if was_activated:
            try:
                current_config = json.loads(
                    _safe_files.safe_read_text(root, root / "config.json", max_bytes=100_000)
                )
            except (FileNotFoundError, json.JSONDecodeError) as exc:
                raise ValueError("approved journal configuration changed after activation") from exc
            if not _activated_plan_config_is_compatible(root, current_config, config):
                raise ValueError("approved journal configuration changed after activation")
            config = current_config
        else:
            _safe_files.safe_atomic_write_text(
                root, root / "config.json", json.dumps(config, indent=2, sort_keys=True) + "\n"
            )
            plan["config_activated"] = True
            _safe_files.safe_atomic_write_text(
                root, root / "approval-plans" / f"{plan_id}.json",
                json.dumps(plan, indent=2, sort_keys=True) + "\n",
            )
        current_daily_tier = config["limits"]["max_selected_messages"]
        if (
            current_daily_tier > DAILY_MESSAGE_TIERS[0]
            and (not was_activated or current_daily_tier == plan["limits"]["max_selected_messages"])
        ):
            tier_receipt = {
                "schema_version": 1,
                "approval_kind": "guided_setup",
                "plan_id": plan_id,
                "approved_tier": current_daily_tier,
                "confirmation_sha256": hashlib.sha256(
                    plan["confirmation_phrase"].encode("utf-8")
                ).hexdigest(),
                "configuration_sha256": _journal_config.validate_config_payload(
                    config
                ).daily_authorization_sha256(),
            }
            _safe_files.safe_atomic_write_text(
                root, root / "daily-workload-approval.json",
                json.dumps(tier_receipt, indent=2, sort_keys=True) + "\n",
            )
        if generation_fn is None:
            from .operations import run_generation
            generation_fn = run_generation
        first = date.fromisoformat(plan["start_date"])
        last = date.fromisoformat(plan["end_date"])
        activity_dates = set(plan["activity_dates"])
        completed_dates = validated_entry_dates(root, first, last) & activity_dates
        if sorted(completed_dates) != plan.get("completed_dates"):
            plan["completed_dates"] = sorted(completed_dates)
            _safe_files.safe_atomic_write_text(
                root, root / "approval-plans" / f"{plan_id}.json",
                json.dumps(plan, indent=2, sort_keys=True) + "\n",
            )
        failed_dates: list[str] = []
        results: list[dict[str, Any]] = []
        for journal_date in sorted(activity_dates - completed_dates):
            try:
                result = generation_fn(
                    f"Generate approved guided setup journal date {journal_date}.",
                    expected_dates=[journal_date],
                )
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            results.append({"journal_date": journal_date, **result})
            if result.get("ok"):
                completed_dates.add(journal_date)
                plan["completed_dates"] = sorted(completed_dates)
                _safe_files.safe_atomic_write_text(
                    root, root / "approval-plans" / f"{plan_id}.json",
                    json.dumps(plan, indent=2, sort_keys=True) + "\n",
                )
            else:
                failed_dates.append(journal_date)
        outstanding = sorted(activity_dates - completed_dates)
        response: dict[str, Any] = {
            "ok": not outstanding,
            "plan_id": plan_id,
            "completed_dates": sorted(completed_dates),
            "failed_dates": sorted(set(failed_dates) | set(outstanding)),
            "results": results,
        }
        if not outstanding:
            response["schedule_offer"] = {
                "offered": True,
                "scheduled": False,
                "message": "Backlog complete. Enable native daily cron now?",
                "command": "hermes journal cron-setup",
            }
        return response
    finally:
        fcntl.flock(root_descriptor, fcntl.LOCK_UN)
        os.close(root_descriptor)
