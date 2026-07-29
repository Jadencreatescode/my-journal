"""Validated consent, scope, privacy, and limit configuration for My Journal."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DATABASE_APPROVAL_TIERS = {16 * 1024**3, 32 * 1024**3}
DAILY_MESSAGE_TIERS = (25_000, 50_000, 100_000)

@dataclass(frozen=True)
class JournalConfig:
    enabled: bool
    timezone: str
    profiles: frozenset[str]
    platforms: frozenset[str]
    excluded_session_ids: frozenset[str]
    database_size_approvals: tuple[tuple[str, int], ...]
    pii_mode: str
    entropy_mode: str
    max_message_chars: int
    max_tool_chars: int
    max_selected_messages: int
    max_retained_chars: int
    max_sessions: int
    packet_chunk_bytes: int
    max_packet_chunks: int

    def require_enabled(self) -> None:
        if not self.enabled:
            raise ValueError("journal collection is disabled; explicit consent is required")
        if not self.profiles:
            raise ValueError("enabled journal collection requires at least one profile")
        if not self.platforms:
            raise ValueError("enabled journal collection requires at least one platform")

    def manifest_policy(self) -> dict[str, Any]:
        return {
            "policy_id": "my-journal-safety-v1",
            "enabled": self.enabled,
            "profiles": sorted(self.profiles),
            "platforms": sorted(self.platforms),
            "excluded_session_count": len(self.excluded_session_ids),
            "database_size_approvals": dict(self.database_size_approvals),
            "pii_mode": self.pii_mode,
            "entropy_mode": self.entropy_mode,
            "redact_secrets": True,
            "limits": {
                "max_message_chars": self.max_message_chars,
                "max_tool_chars": self.max_tool_chars,
                "max_selected_messages": self.max_selected_messages,
                "max_retained_chars": self.max_retained_chars,
                "max_sessions": self.max_sessions,
                "packet_chunk_bytes": self.packet_chunk_bytes,
                "max_packet_chunks": self.max_packet_chunks,
            },
        }

    def daily_authorization_sha256(self) -> str:
        payload = {
            "schema_version": 1,
            "enabled": self.enabled,
            "timezone": self.timezone,
            "profiles": sorted(self.profiles),
            "platforms": sorted(self.platforms),
            "excluded_session_ids": sorted(self.excluded_session_ids),
            "privacy": {
                "redact_secrets": True,
                "pii_mode": self.pii_mode,
                "entropy_mode": self.entropy_mode,
            },
            "limits": {
                "max_message_chars": self.max_message_chars,
                "max_tool_chars": self.max_tool_chars,
                "max_selected_messages": self.max_selected_messages,
                "max_retained_chars": self.max_retained_chars,
                "max_sessions": self.max_sessions,
                "packet_chunk_bytes": self.packet_chunk_bytes,
                "max_packet_chunks": self.max_packet_chunks,
            },
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


def _string_set(value: Any, label: str) -> frozenset[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{label} must be a list of nonempty strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{label} must not contain duplicates")
    return frozenset(value)


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _database_approvals(value: Any, profiles: frozenset[str]) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, dict) or len(value) > 128:
        raise ValueError("database_size_approvals must be a bounded object")
    approvals: list[tuple[str, int]] = []
    for profile, tier in value.items():
        if (
            not isinstance(profile, str) or not profile.strip() or len(profile) > 512
            or profile not in profiles or isinstance(tier, bool) or tier not in DATABASE_APPROVAL_TIERS
        ):
            raise ValueError("database_size_approvals must bind selected profiles to compiled tiers")
        approvals.append((profile, tier))
    return tuple(sorted(approvals))


def validate_config_payload(raw: Any) -> JournalConfig:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("journal config schema_version must equal 1")
    allowed = {
        "schema_version", "enabled", "timezone", "profiles", "platforms",
        "excluded_session_ids", "database_size_approvals", "privacy", "limits",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError("journal config contains unsupported fields: " + ", ".join(sorted(unknown)))
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    timezone_name = raw.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ValueError("timezone must be a nonempty IANA timezone")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc
    profiles = _string_set(raw.get("profiles", []), "profiles")
    platforms = _string_set(raw.get("platforms", []), "platforms")
    excluded = _string_set(raw.get("excluded_session_ids", []), "excluded_session_ids")

    privacy = raw.get("privacy")
    if not isinstance(privacy, dict):
        raise ValueError("privacy must be an object")
    if privacy.get("redact_secrets") is not True:
        raise ValueError("secret redaction is mandatory")
    pii_mode = privacy.get("pii_mode", "mask")
    entropy_mode = privacy.get("entropy_mode", "report")
    if pii_mode not in {"mask", "preserve"}:
        raise ValueError("pii_mode must equal mask or preserve")
    if entropy_mode not in {"report", "redact", "off"}:
        raise ValueError("entropy_mode must equal report, redact, or off")

    limits = raw.get("limits")
    if not isinstance(limits, dict):
        raise ValueError("limits must be an object")
    max_selected_messages = _positive_int(
        limits.get("max_selected_messages", 25000), "max_selected_messages"
    )
    if max_selected_messages > 100_000:
        raise ValueError("max_selected_messages exceeds the compiled ceiling")
    if max_selected_messages not in DAILY_MESSAGE_TIERS:
        raise ValueError("max_selected_messages must equal a compiled tier")
    config = JournalConfig(
        enabled=enabled,
        timezone=timezone_name,
        profiles=profiles,
        platforms=platforms,
        excluded_session_ids=excluded,
        database_size_approvals=_database_approvals(raw.get("database_size_approvals", {}), profiles),
        pii_mode=pii_mode,
        entropy_mode=entropy_mode,
        max_message_chars=_positive_int(limits.get("max_message_chars", 4000), "max_message_chars"),
        max_tool_chars=_positive_int(limits.get("max_tool_chars", 1200), "max_tool_chars"),
        max_selected_messages=max_selected_messages,
        max_retained_chars=_positive_int(limits.get("max_retained_chars", 4000000), "max_retained_chars"),
        max_sessions=_positive_int(limits.get("max_sessions", 2000), "max_sessions"),
        packet_chunk_bytes=_positive_int(limits.get("packet_chunk_bytes", 120000), "packet_chunk_bytes"),
        max_packet_chunks=_positive_int(limits.get("max_packet_chunks", 64), "max_packet_chunks"),
    )
    if config.enabled:
        config.require_enabled()
    return config


def load_config(path: Path) -> JournalConfig:
    path = path.expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"journal config is not a regular file: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"journal config could not be read: {exc}") from exc
    return validate_config_payload(raw)
