#!/usr/bin/env python3
"""Validate a My Journal note and commit completion state only after success."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from atomic_files import atomic_write_text
from evidence_identity import canonical_evidence_sha256
from secret_redaction import contains_likely_secret, sensitive_finding_counts
from safe_files import safe_read_text


REQUIRED_HEADINGS = [
    "Overview",
    "Conversation Coverage",
    "Projects and Workstreams",
    "Decisions",
    "Changes and Verification",
    "Completed Work",
    "Blockers and Failures",
    "Corrections and Preference Changes",
    "Open Threads",
    "Context Index",
    "Automation Appendix",
    "Provenance",
]

def _has_exact_line(text: str, expected: str) -> bool:
    return expected in {line.strip() for line in text.splitlines()}


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _validate_delivery(manifest: dict[str, Any], session_refs: list[str]) -> list[str]:
    errors: list[str] = []
    delivery = manifest.get("delivery")
    if not isinstance(delivery, dict):
        return ["schema 2 delivery is required"]
    if delivery.get("schema_version") != 1:
        errors.append("delivery schema version is unsupported")
    chunks = delivery.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        return errors + ["delivery chunks must be a nonempty list"]
    if delivery.get("chunk_count") != len(chunks):
        errors.append("delivery chunk count is inconsistent")
    expected_mode = "single" if len(chunks) == 1 else "chunked"
    if delivery.get("mode") != expected_mode:
        errors.append("delivery mode is inconsistent with chunk count")

    normalized: list[dict[str, Any]] = []
    seen_owned: set[str] = set()
    all_owned: list[str] = []
    for expected_index, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, dict):
            errors.append("delivery chunk must be an object")
            continue
        if chunk.get("index") != expected_index:
            errors.append("delivery chunk indices are not contiguous")
        for field in ("chunk_id", "body_sha256", "sha256"):
            value = chunk.get(field)
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                errors.append(f"delivery chunk {field} must be 64 lowercase hex characters")
        if _nonnegative_int(chunk.get("bytes")) is None or chunk.get("bytes") == 0:
            errors.append("delivery chunk bytes must be a positive integer")
        owned = chunk.get("owned_session_refs")
        continued = chunk.get("continued_session_refs")
        if not isinstance(owned, list) or any(not isinstance(item, str) for item in owned):
            errors.append("delivery owned session references must be strings")
            owned = []
        if not isinstance(continued, list) or any(not isinstance(item, str) for item in continued):
            errors.append("delivery continued session references must be strings")
            continued = []
        if len(set(owned)) != len(owned) or len(set(continued)) != len(continued):
            errors.append("delivery session references must not repeat within a chunk")
        if set(owned) & set(continued):
            errors.append("delivery owned and continued session references overlap")
        if any(ref in seen_owned for ref in owned):
            errors.append("delivery session reference is owned by multiple chunks")
        if any(ref not in seen_owned for ref in continued):
            errors.append("delivery continues a session before its owning chunk")
        seen_owned.update(owned)
        all_owned.extend(owned)
        normalized.append({
            key: chunk.get(key)
            for key in (
                "index", "chunk_id", "body_sha256", "sha256", "bytes",
                "owned_session_refs", "continued_session_refs",
            )
        })
    if Counter(all_owned) != Counter(session_refs):
        errors.append("delivery ownership does not match manifest sessions")
    expected_index_hash = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if delivery.get("chunk_index_sha256") != expected_index_hash:
        errors.append("delivery chunk index hash is invalid")
    return errors


def _privacy_string_values(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _privacy_string_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _privacy_string_values(nested)


def validate_manifest(manifest: Any) -> list[str]:
    """Validate internal manifest identity, shape, and coverage consistency."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest root must be an object"]

    schema_version = manifest.get("schema_version")
    required_top = {
        "schema_version", "run_id", "evidence_sha256", "journal_date",
        "created_at", "window", "coverage", "databases", "sessions",
    }
    if schema_version == 2:
        required_top.add("timezone")
    for field in sorted(required_top - set(manifest)):
        errors.append(f"manifest missing required top-level field: {field}")
    if schema_version not in {1, 2}:
        errors.append("manifest schema_version must equal 1 or 2")
    journal_date = manifest.get("journal_date")
    parsed_date = None
    try:
        parsed_date = datetime.strptime(str(journal_date), "%Y-%m-%d").date()
        if parsed_date.isoformat() != journal_date:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("manifest journal_date must be an ISO calendar date")
    created_at = manifest.get("created_at")
    try:
        parsed_created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        if parsed_created.tzinfo is None or parsed_created.utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("manifest created_at must be a timezone-aware ISO timestamp")
    raw_window = manifest.get("window")
    if not isinstance(raw_window, dict):
        errors.append("manifest window must be an object")
        window: dict[str, Any] = {}
    else:
        window = raw_window
    start_ts = window.get("start_ts")
    end_ts = window.get("end_ts")
    numeric_window = (
        not isinstance(start_ts, bool)
        and not isinstance(end_ts, bool)
        and isinstance(start_ts, (int, float))
        and isinstance(end_ts, (int, float))
        and math.isfinite(float(start_ts))
        and math.isfinite(float(end_ts))
    )
    if not numeric_window or float(start_ts) >= float(end_ts):
        errors.append("manifest window must contain finite start_ts before end_ts")
    if schema_version == 2:
        timezone_name = manifest.get("timezone")
        zone = None
        if not isinstance(timezone_name, str) or not timezone_name.strip():
            errors.append("manifest timezone must be a nonempty IANA timezone")
        else:
            try:
                zone = ZoneInfo(timezone_name)
            except ZoneInfoNotFoundError:
                errors.append("manifest timezone must be a valid IANA timezone")
        if zone is not None and parsed_date is not None and numeric_window:
            expected_start = datetime.combine(parsed_date, datetime.min.time(), tzinfo=zone).timestamp()
            expected_end = datetime.combine(
                parsed_date.fromordinal(parsed_date.toordinal() + 1),
                datetime.min.time(),
                tzinfo=zone,
            ).timestamp()
            if float(str(start_ts)) != expected_start or float(str(end_ts)) != expected_end:
                errors.append("manifest window does not exactly match journal_date in timezone")
    expected_sha_value = manifest.get("evidence_sha256")
    if not isinstance(expected_sha_value, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha_value) is None:
        errors.append("evidence_sha256 must be 64 lowercase hexadecimal characters")
    run_id_value = manifest.get("run_id")
    if not isinstance(run_id_value, str) or re.fullmatch(r"[0-9a-f]{16}", run_id_value) is None:
        errors.append("run_id must be 16 lowercase hexadecimal characters")

    raw_coverage = manifest.get("coverage", {})
    if not isinstance(raw_coverage, dict):
        errors.append("manifest coverage must be an object")
        coverage: dict[str, Any] = {}
    else:
        coverage = raw_coverage

    raw_databases = manifest.get("databases", [])
    if not isinstance(raw_databases, list):
        errors.append("manifest databases must be a list")
        raw_databases = []
    databases: list[dict[str, Any]] = []
    for item in raw_databases:
        if isinstance(item, dict):
            databases.append(item)
            for field in ("profile", "path", "status"):
                if field not in item:
                    errors.append(f"database report missing required field: {field}")
            if not isinstance(item.get("profile"), str) or not item.get("profile"):
                errors.append("database profile must be a nonempty string")
            if not isinstance(item.get("path"), str) or not item.get("path"):
                errors.append("database path must be a nonempty string")
            status = item.get("status")
            if not isinstance(status, str) or status not in {"ok", "error"}:
                errors.append("database status must be ok or error")
        else:
            errors.append("every database report must be an object")

    raw_sessions = manifest.get("sessions", [])
    if not isinstance(raw_sessions, list):
        errors.append("manifest sessions must be a list")
        raw_sessions = []
    sessions: list[dict[str, Any]] = []
    for item in raw_sessions:
        if isinstance(item, dict):
            sessions.append(item)
            required_session = {
                "profile", "platform", "session_id", "title", "started_at",
                "chat_id", "thread_id", "display_name", "coverage_ref",
                "context_label", "messages",
            }
            for field in sorted(required_session - set(item)):
                errors.append(f"session field is missing: {field}")
            for field in ("profile", "platform", "session_id", "title", "display_name"):
                if not isinstance(item.get(field), str):
                    errors.append(f"session field {field} must be a string")
            context_label = item.get("context_label")
            if not isinstance(context_label, str) or context_label not in {
                "unclassified", "professional", "personal", "mixed", "unclear"
            }:
                errors.append("session field context_label is invalid")
            reference = item.get("coverage_ref")
            if not isinstance(reference, str) or re.fullmatch(r"[0-9a-f]{64}", reference) is None:
                errors.append("session field coverage_ref must be a SHA256 value")
            profile = item.get("profile")
            session_id = item.get("session_id")
            if isinstance(profile, str) and isinstance(session_id, str):
                policy = manifest.get("policy")
                masked = (
                    schema_version == 2
                    and isinstance(policy, dict)
                    and policy.get("pii_mode") == "mask"
                )
                if masked:
                    if session_id != reference:
                        errors.append("masked session_id must equal coverage_ref")
                else:
                    derived_ref = hashlib.sha256(
                        f"{profile}\0{session_id}".encode("utf-8")
                    ).hexdigest()
                    if reference != derived_ref:
                        errors.append("session field coverage_ref does not match profile and session_id")
            started_at = item.get("started_at")
            if started_at is not None and (
                isinstance(started_at, bool)
                or not isinstance(started_at, (int, float))
                or not math.isfinite(float(started_at))
            ):
                errors.append("session field started_at must be finite or null")
            for field in ("chat_id", "thread_id"):
                if item.get(field) is not None and not isinstance(item.get(field), (str, int)):
                    errors.append(f"session field {field} must be a string, integer, or null")
        else:
            errors.append("every session record must be an object")

    expected_sha = str(manifest.get("evidence_sha256", ""))
    actual_sha = canonical_evidence_sha256(manifest)
    if expected_sha != actual_sha:
        errors.append("evidence SHA256 does not match manifest content")
    if str(manifest.get("run_id", "")) != expected_sha[:16]:
        errors.append("run ID does not match the evidence SHA256")

    count_fields = (
        "database_count",
        "database_error_count",
        "session_count",
        "message_count",
        "retained_message_count",
    )
    if schema_version == 2:
        count_fields += ("retained_char_count",)
    counts: dict[str, int | None] = {}
    for field in count_fields:
        counts[field] = _nonnegative_int(coverage.get(field))
        if counts[field] is None:
            errors.append(f"coverage {field} must be a nonnegative integer")

    if counts["session_count"] is not None and counts["session_count"] != len(sessions):
        errors.append("session count does not match manifest sessions")

    readable_databases = sum(1 for item in databases if item.get("status") == "ok")
    failed_databases = len(databases) - readable_databases
    if counts["database_count"] is not None and counts["database_count"] != readable_databases:
        errors.append("database count does not match database reports")
    if counts["database_error_count"] is not None and counts["database_error_count"] != failed_databases:
        errors.append("database error count does not match database reports")

    database_message_counts = [_nonnegative_int(item.get("message_count")) for item in databases]
    if any(value is None for value in database_message_counts):
        errors.append("database message counts must be nonnegative integers")
    elif counts["message_count"] is not None and counts["message_count"] != sum(database_message_counts):
        errors.append("message count does not match database reports")

    database_session_counts = [_nonnegative_int(item.get("session_count")) for item in databases]
    if any(value is None for value in database_session_counts):
        errors.append("database session counts must be nonnegative integers")
    elif counts["session_count"] is not None and counts["session_count"] != sum(database_session_counts):
        errors.append("session count does not match database reports")

    retained_messages = 0
    retained_chars = 0
    for session in sessions:
        messages = session.get("messages", [])
        if not isinstance(messages, list):
            errors.append("every session messages field must be a list")
            continue
        retained_messages += len(messages)
        for message in messages:
            if not isinstance(message, dict):
                errors.append("every message record must be an object")
                continue
            required_message = {
                "message_id", "role", "timestamp", "content", "tool_name", "tool_calls"
            }
            for field in sorted(required_message - set(message)):
                errors.append(f"message field is missing: {field}")
            if _nonnegative_int(message.get("message_id")) is None:
                errors.append("message field message_id must be a nonnegative integer")
            if not isinstance(message.get("role"), str) or not message.get("role"):
                errors.append("message field role must be a nonempty string")
            timestamp = message.get("timestamp")
            if (
                isinstance(timestamp, bool)
                or not isinstance(timestamp, (int, float))
                or not math.isfinite(float(timestamp))
            ):
                errors.append("message field timestamp must be finite")
            elif numeric_window and not (float(start_ts) <= float(timestamp) < float(end_ts)):
                errors.append("message field timestamp is outside the manifest window")
            if not isinstance(message.get("content"), str):
                errors.append("message field content must be a string")
            else:
                retained_chars += len(message["content"])
            for field in ("tool_name", "tool_calls"):
                if message.get(field) is not None and not isinstance(message.get(field), str):
                    errors.append(f"message field {field} must be a string or null")
                elif isinstance(message.get(field), str):
                    retained_chars += len(message[field])
    if counts["retained_message_count"] is not None and counts["retained_message_count"] != retained_messages:
        errors.append("retained message count does not match manifest sessions")
    if (
        counts["retained_message_count"] is not None
        and counts["message_count"] is not None
        and counts["retained_message_count"] > counts["message_count"]
    ):
        errors.append("retained message count exceeds selected message count")
    if (
        schema_version == 2
        and counts.get("retained_char_count") is not None
        and counts["retained_char_count"] != retained_chars
    ):
        errors.append("retained character count does not match manifest sessions")

    raw_platforms = coverage.get("platforms", [])
    raw_profiles = coverage.get("profiles", [])
    if not isinstance(raw_platforms, list):
        errors.append("coverage platforms must be a list")
        raw_platforms = []
    if not isinstance(raw_profiles, list):
        errors.append("coverage profiles must be a list")
        raw_profiles = []
    derived_platforms = sorted({str(item.get("platform", "unknown")) for item in sessions})
    derived_profiles = sorted({str(item.get("profile", "default")) for item in sessions})
    if list(raw_platforms) != derived_platforms:
        errors.append("platform coverage does not match manifest sessions")
    if list(raw_profiles) != derived_profiles:
        errors.append("profile coverage does not match manifest sessions")

    references = [str(item.get("coverage_ref", "")) for item in sessions]
    if any(not reference for reference in references) or len(set(references)) != len(sessions):
        errors.append("session coverage references are missing or duplicated")
    if schema_version == 2:
        errors.extend(_validate_delivery(manifest, references))
        policy = manifest.get("policy")
        if not isinstance(policy, dict) or policy.get("redact_secrets") is not True:
            errors.append("schema 2 policy must require secret redaction")
        else:
            pii_mode = policy.get("pii_mode", "preserve")
            entropy_mode = policy.get("entropy_mode", "off")
            if pii_mode not in {"mask", "preserve"}:
                errors.append("schema 2 policy has invalid pii_mode")
                pii_mode = "mask"
            if entropy_mode not in {"report", "redact", "off"}:
                errors.append("schema 2 policy has invalid entropy_mode")
                entropy_mode = "report"
            evidence_text = "\n".join(
                _privacy_string_values({"databases": databases, "sessions": sessions})
            )
            findings = sensitive_finding_counts(
                evidence_text,
                pii_mode=pii_mode,
                entropy_mode=entropy_mode,
            )
            if findings:
                errors.append("manifest contains values forbidden by its privacy policy")
    return errors


def validate_digest_bindings(manifest: Any, digest_texts: list[str]) -> list[str]:
    """Require each digest to identify the exact evidence run and chunk it summarizes."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["digest bindings require a manifest object"]
    run_id = str(manifest.get("run_id", ""))
    evidence_sha256 = str(manifest.get("evidence_sha256", ""))
    if manifest.get("schema_version") != 2:
        for index, digest in enumerate(digest_texts, start=1):
            if not _has_exact_line(digest, f"Digest Run ID: {run_id}"):
                errors.append(f"digest {index} does not contain the current run ID")
            if not _has_exact_line(digest, f"Digest Evidence SHA256: {evidence_sha256}"):
                errors.append(f"digest {index} does not contain the current evidence SHA256")
        return errors

    delivery = manifest.get("delivery")
    chunks = delivery.get("chunks", []) if isinstance(delivery, dict) else []
    if not isinstance(chunks, list):
        return ["schema 2 digest validation requires a valid delivery index"]
    if len(digest_texts) != len(chunks):
        errors.append("digest receipt count does not match delivery chunk count")
    for index, chunk in enumerate(chunks, start=1):
        if index > len(digest_texts) or not isinstance(chunk, dict):
            continue
        digest = digest_texts[index - 1]
        try:
            header, body = digest.split("\n---\n", 1)
        except ValueError:
            errors.append(f"digest {index} is missing its provenance boundary")
            continue
        values: dict[str, str] = {}
        session_refs: list[str] = []
        for line in header.splitlines():
            if line.startswith("Session Ref: "):
                session_refs.append(line.removeprefix("Session Ref: "))
            elif ": " in line:
                label, value = line.split(": ", 1)
                values[label] = value
        if values.get("Digest Schema") != "2":
            errors.append(f"digest {index} schema is unsupported")
        if values.get("Run ID") != run_id:
            errors.append(f"digest {index} does not contain the current run ID")
        if values.get("Evidence SHA256") != evidence_sha256:
            errors.append(f"digest {index} does not contain the current evidence SHA256")
        if values.get("Chunk Index") != f"{index}/{len(chunks)}":
            errors.append(f"digest {index} chunk index does not match delivery index")
        if values.get("Chunk ID") != chunk.get("chunk_id"):
            errors.append(f"digest {index} chunk identity does not match delivery index")
        if values.get("Chunk File SHA256") != chunk.get("sha256"):
            errors.append(f"digest {index} source file hash does not match delivery index")
        if values.get("Digest Body SHA256") != hashlib.sha256(body.encode("utf-8")).hexdigest():
            errors.append(f"digest {index} body hash is invalid")
        owned = chunk.get("owned_session_refs", [])
        if not isinstance(owned, list) or session_refs != owned:
            errors.append(f"digest {index} session ownership does not match delivery index")
    return errors


def validate_note(
    manifest: dict[str, Any],
    note: str,
    session_evidence: str = "",
    manifest_path: Path | None = None,
    digest_dir: Path | None = None,
) -> list[str]:
    """Return validation errors. An empty list means the note is acceptable."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        manifest = {}
        errors.append("note validation requires a manifest object")
    run_id = str(manifest.get("run_id", ""))
    journal_date = str(manifest.get("journal_date", ""))
    raw_coverage = manifest.get("coverage", {})
    if isinstance(raw_coverage, dict):
        coverage = raw_coverage
    else:
        coverage = {}
        errors.append("note validation requires object shaped coverage")

    if not _has_exact_line(note, f"# My Journal: {journal_date}"):
        errors.append("journal title does not contain the manifest date")
    for heading in REQUIRED_HEADINGS:
        if not _has_exact_line(note, f"## {heading}"):
            errors.append(f"missing required heading: {heading}")
    if not _has_exact_line(note, f"Run ID: {run_id}"):
        errors.append("provenance does not contain the run ID")
    expected_counts = {
        "Databases": coverage.get("database_count"),
        "Sessions": coverage.get("session_count"),
        "Messages": coverage.get("message_count"),
    }
    for label, value in expected_counts.items():
        if not _has_exact_line(note, f"{label}: {value}"):
            errors.append(f"provenance does not contain exact {label.lower()} coverage")

    raw_database_errors = coverage.get("database_error_count", 0)
    parsed_database_errors = _nonnegative_int(raw_database_errors)
    if parsed_database_errors is None:
        errors.append("database error coverage must be a nonnegative integer")
        database_errors = 0
    else:
        database_errors = parsed_database_errors
    if database_errors:
        errors.append("manifest reports database errors")
    if not _has_exact_line(note, f"Database Errors: {database_errors}"):
        errors.append("provenance does not contain exact database error coverage")

    evidence_sha256 = str(manifest.get("evidence_sha256", ""))
    if not evidence_sha256 or not _has_exact_line(note, f"Evidence SHA256: {evidence_sha256}"):
        errors.append("provenance does not contain the evidence SHA256")

    raw_platforms = coverage.get("platforms", [])
    raw_profiles = coverage.get("profiles", [])
    if not isinstance(raw_platforms, list):
        errors.append("note validation requires a platform list")
        raw_platforms = []
    if not isinstance(raw_profiles, list):
        errors.append("note validation requires a profile list")
        raw_profiles = []
    platforms = ", ".join(sorted(str(value) for value in raw_platforms))
    profiles = ", ".join(sorted(str(value) for value in raw_profiles))
    if not _has_exact_line(note, f"Platforms: {platforms}"):
        errors.append("provenance does not contain exact platforms")
    if not _has_exact_line(note, f"Profiles: {profiles}"):
        errors.append("provenance does not contain exact profiles")
    if manifest_path is not None and not _has_exact_line(note, f"Evidence manifest: {manifest_path}"):
        errors.append("provenance does not contain the evidence manifest path")
    if digest_dir is not None and not _has_exact_line(note, f"Digest directory: {digest_dir}"):
        errors.append("provenance does not contain the digest directory")

    raw_sessions = manifest.get("sessions", [])
    if not isinstance(raw_sessions, list):
        errors.append("note validation requires a session list")
        raw_sessions = []
    expected_refs = {
        str(session.get("coverage_ref"))
        for session in raw_sessions
        if isinstance(session, dict) and session.get("coverage_ref")
    }
    if expected_refs:
        found_refs = [
            line.removeprefix("Session Ref: ").strip()
            for line in (note + "\n" + session_evidence).splitlines()
            if line.startswith("Session Ref: ")
        ]
        expected_counts_by_ref = Counter({reference: 1 for reference in expected_refs})
        if Counter(found_refs) != expected_counts_by_ref:
            errors.append("session coverage references do not exactly match the manifest")
    if contains_likely_secret(note) or contains_likely_secret(session_evidence):
        errors.append("note contains a likely unredacted secret")
    return errors


def validate_and_commit(
    manifest_path: Path,
    note_path: Path,
    state_path: Path,
    digest_dir: Path | None = None,
) -> dict[str, Any]:
    """Validate a note and atomically write completed state after every check passes."""
    base_result = {
        "valid": False,
        "run_id": None,
        "journal_date": None,
        "manifest_path": str(manifest_path),
        "note_path": str(note_path),
        "digest_dir": str(digest_dir) if digest_dir is not None else None,
    }
    path_set = [manifest_path, note_path, state_path]
    if digest_dir is not None:
        path_set.append(digest_dir)
    journal_root = Path(os.path.commonpath([
        str(path.expanduser().absolute()) for path in path_set
    ]))
    if journal_root in {
        manifest_path.expanduser().absolute(),
        note_path.expanduser().absolute(),
        state_path.expanduser().absolute(),
    }:
        journal_root = journal_root.parent
    try:
        manifest = json.loads(
            safe_read_text(journal_root, manifest_path, max_bytes=8_000_000)
        )
    except json.JSONDecodeError:
        return {**base_result, "errors": ["manifest is not valid JSON"]}
    except (OSError, UnicodeError, ValueError) as exc:
        return {**base_result, "errors": [f"manifest could not be read: {exc}"]}
    try:
        note = safe_read_text(journal_root, note_path, max_bytes=8_000_000)
    except (OSError, UnicodeError, ValueError) as exc:
        return {**base_result, "errors": [f"note could not be read: {exc}"]}
    digest_texts: list[str] = []
    digest_error: str | None = None
    if digest_dir is not None:
        if not digest_dir.is_dir():
            digest_error = "digest directory does not exist"
        else:
            for path in sorted(digest_dir.glob("*.md")):
                try:
                    path.resolve().relative_to(digest_dir.resolve())
                except (OSError, ValueError):
                    digest_error = f"digest file is outside the declared digest directory: {path}"
                    break
                if path.is_symlink() or not path.is_file():
                    digest_error = f"digest file is outside the declared digest directory: {path}"
                    break
                try:
                    digest_texts.append(
                        safe_read_text(journal_root, path, max_bytes=1_000_000)
                    )
                except (OSError, UnicodeError, ValueError) as exc:
                    digest_error = f"digest file could not be read safely: {path}: {exc}"
                    break
            if digest_error is None and not digest_texts:
                digest_error = "digest directory contains no Markdown digests"
    errors = validate_manifest(manifest)
    errors.extend(validate_digest_bindings(manifest, digest_texts))
    errors.extend(validate_note(
        manifest,
        note,
        "\n".join(digest_texts),
        manifest_path=manifest_path,
        digest_dir=digest_dir,
    ))
    if digest_error:
        errors.append(digest_error)
    manifest_info = manifest if isinstance(manifest, dict) else {}
    result = {
        "valid": not errors,
        "run_id": manifest_info.get("run_id"),
        "journal_date": manifest_info.get("journal_date"),
        "manifest_path": str(manifest_path),
        "note_path": str(note_path),
        "digest_dir": str(digest_dir) if digest_dir is not None else None,
        "errors": errors,
    }
    if errors:
        return result

    state = {
        "status": "completed",
        "run_id": manifest_info["run_id"],
        "journal_date": manifest_info["journal_date"],
        "manifest_path": str(manifest_path),
        "note_path": str(note_path),
        "digest_dir": str(digest_dir) if digest_dir is not None else None,
        "coverage": manifest_info["coverage"],
        "validated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    atomic_write_text(
        state_path,
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        trusted_root=journal_root,
    )
    result["state_path"] = str(state_path)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--note", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--digest-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = validate_and_commit(args.manifest, args.note, args.state, args.digest_dir)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    sys.exit(main())
