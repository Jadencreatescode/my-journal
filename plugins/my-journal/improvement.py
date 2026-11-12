"""Read only recurring issue alerts and an append only lesson ledger.

Everything here is deterministic and stdlib only. Canonical notes are read
only after full evidence validation, and no function writes beneath ``notes/``.
The only writer is :func:`record_lesson`, which appends one event to
``<journal root>/improvements/lessons.jsonl`` through descriptor anchored
primitives.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn

try:  # POSIX only; the plugin runtime never imports this module on native Windows.
    import fcntl
except ImportError:  # pragma: no cover - native Windows forwards to WSL
    fcntl = None  # type: ignore[assignment]

from .core import (
    _bounded_errors,
    _safe_files,
    discover_entries,
    reject_symlink_components,
    validate_entry_content,
)


# Sections scanned for issues, in canonical note order.
ISSUE_SECTIONS = (
    ("blockers_and_failures", "Blockers and Failures"),
    ("corrections_and_preference_changes", "Corrections and Preference Changes"),
)
_SECTION_KEYS = tuple(key for key, _ in ISSUE_SECTIONS)

# Grouping: two items are similar when the Jaccard similarity of their
# significant term sets is at least SIMILARITY_THRESHOLD and they share at
# least MIN_SHARED_TERMS terms (identical one term sets also match).
SIMILARITY_THRESHOLD = 0.5
MIN_SHARED_TERMS = 2
DEFAULT_MIN_DATES = 3
MIN_MIN_DATES = 2
MAX_MIN_DATES = 366
DEFAULT_MAX_GROUPS = 10
MAX_GROUPS = 50
DEFAULT_MAX_CITATIONS = 10
MAX_CITATIONS = 25
MAX_RANGE_DAYS = 366
MAX_EXCERPT_CHARS = 240
# Grouping cost bound: at most MAX_ITEMS issue items are grouped per call
# (earliest first); later items are dropped and items_truncated is set.
MAX_ITEMS = 5_000
# Excluded date details: at most MAX_EXCLUDED_LISTED dates, each with at most
# MAX_EXCLUDED_ERRORS errors of MAX_EXCLUDED_ERROR_CHARS characters.
MAX_EXCLUDED_LISTED = 31
MAX_EXCLUDED_ERRORS = 3
MAX_EXCLUDED_ERROR_CHARS = 200
MAX_LABEL_TERMS = 5

# Improvement tracking windows.
WINDOW_CAP_DAYS = 90
MIN_VALIDATED_DATES = 7
MAX_REPORT_LESSONS = 50
MAX_REPORT_CITATIONS = 20
CORRELATION_NOTICE = (
    "This compares recorded journal evidence before and since adoption. It shows "
    "correlation over the recorded period, not proof of cause."
)
RECURRING_NOTICE = (
    "Candidate recurring issues for review. Grouping is lexical and deterministic, "
    "may merge or split items imperfectly, and changes nothing. Confirm with the "
    "user before recording any lesson."
)

# Lesson ledger.
LEDGER_DIRECTORY = "improvements"
LEDGER_NAME = "lessons.jsonl"
LEDGER_SCHEMA_VERSION = 1
MAX_LEDGER_BYTES = 4_000_000
MAX_LEDGER_RECORDS = 5_000
MAX_TITLE_CHARS = 200
MAX_TERM_CHARS = 80
MAX_TERMS = 20
MAX_SOURCE_DATES = 50
MAX_REASON_CHARS = 500
_LESSON_ID = re.compile(r"[0-9a-f]{16}")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_ADOPT_FIELDS = frozenset({
    "action", "title", "adopted_on", "any_terms", "all_terms", "sections", "source_dates",
})
_RETIRE_FIELDS = frozenset({"action", "lesson_id", "retired_on", "reason"})
_ADOPT_RECORD_FIELDS = frozenset({
    "schema_version", "event", "lesson_id", "title", "adopted_on", "match",
    "sections", "source_dates", "recorded_at",
})
_RETIRE_RECORD_FIELDS = frozenset({
    "schema_version", "event", "lesson_id", "retired_on", "reason", "recorded_at",
})

_STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are as at be because
    been before being below between both but by can could did do does doing done
    down during each either few for from further had has have having he her here
    hers him his how however i if in into is it its itself just let me more most
    my no nor not now of off on once only or other our ours out over own same she
    should so some still such than that the their theirs them then there these
    they this those through to too under until up upon us very was we were what
    when where which while who whom why will with would yet you your yours
    """.split()
)
_PLACEHOLDERS = frozenset({
    "none", "na", "n a", "nil", "nothing", "nothing to report", "nothing noted",
    "none recorded", "none noted", "none observed", "none reported", "not applicable",
    "no blockers", "no failures", "no blockers or failures", "no corrections",
    "no preference changes", "no corrections or preference changes", "no issues",
    "tbd", "todo", "unknown",
})
_WORD = re.compile(r"[a-z0-9]+")
_LIST_ITEM = re.compile(r"^(\s*)(?:[-*+]|[0-9]{1,3}[.)])\s+(.*\S)\s*$")


# ---------------------------------------------------------------------------
# Normalization and similarity


def _stem(token: str) -> str:
    """Light deterministic suffix stemming; not a linguistic stemmer."""
    if len(token) > 4 and token.endswith("ies"):
        token = token[:-3] + "y"
    elif len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        token = token[:-1]
    for suffix in ("ing", "ed", "ment", "ure", "ly"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            token = token[: -len(suffix)]
            break
    if len(token) > 3 and token.endswith("e"):
        token = token[:-1]
    if len(token) > 3 and token[-1] == token[-2] and token[-1] not in "aeiou":
        token = token[:-1]
    return token


def _surface_words(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for word in _WORD.findall(text.lower()):
        if word in _STOPWORDS or word.isdigit():
            continue
        stem = _stem(word)
        if len(stem) < 3 or stem in _STOPWORDS:
            continue
        pairs.append((stem, word))
    return pairs


def significant_terms(text: str) -> frozenset[str]:
    """Lowercase, strip punctuation and stopwords, and lightly stem."""
    return frozenset(stem for stem, _ in _surface_words(text))


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 0.0
    return len(left & right) / len(left | right)


def _similar(left: frozenset[str], right: frozenset[str]) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    return len(left & right) >= MIN_SHARED_TERMS and jaccard(left, right) >= SIMILARITY_THRESHOLD


def _is_placeholder(text: str) -> bool:
    collapsed = " ".join(_WORD.findall(text.lower()))
    return not collapsed or collapsed in _PLACEHOLDERS


def _excerpt(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= MAX_EXCERPT_CHARS:
        return collapsed
    return collapsed[: MAX_EXCERPT_CHARS - 3].rstrip() + "..."


# ---------------------------------------------------------------------------
# Validated note reading


def _section_bodies(text: str) -> dict[str, list[str]]:
    wanted = {f"## {heading}": key for key, heading in ISSUE_SECTIONS}
    bodies: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("## ") or line.startswith("# "):
            current = wanted.get(line.rstrip())
            if current is not None and current in bodies:
                current = None  # validated notes contain each heading once
            elif current is not None:
                bodies[current] = []
            continue
        if current is not None:
            bodies[current].append(line)
    return bodies


def _section_items(lines: list[str]) -> list[str]:
    """Return list items; fall back to paragraphs when a section has no list."""
    items: list[str] = []
    current: list[str] | None = None
    for line in lines:
        match = _LIST_ITEM.match(line)
        if match:
            current = [match.group(2)]
            items.append(match.group(2))
            continue
        if current is not None and line.strip() and line[:1].isspace():
            current.append(line.strip())
            items[-1] = " ".join(current)
            continue
        if not line.strip():
            continue
        current = None
    if items:
        return items
    paragraphs: list[str] = []
    buffer: list[str] = []
    for line in [*lines, ""]:
        if line.strip():
            buffer.append(line.strip())
        elif buffer:
            paragraphs.append(" ".join(buffer))
            buffer = []
    return paragraphs


def extract_issue_items(text: str, day: str) -> list[dict[str, Any]]:
    bodies = _section_bodies(text)
    items: list[dict[str, Any]] = []
    for key in _SECTION_KEYS:
        for raw in _section_items(bodies.get(key, [])):
            if _is_placeholder(raw):
                continue
            pairs = _surface_words(raw)
            terms = frozenset(stem for stem, _ in pairs)
            if not terms:
                continue
            items.append({
                "date": day,
                "section": key,
                "excerpt": _excerpt(raw),
                "terms": terms,
                "surface": pairs,
            })
    return items


class _ValidatedReader:
    """Validate each canonical date at most once per call and cache its items."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._paths = {item["date"]: Path(item["path"]) for item in discover_entries(root)}
        self._cache: dict[str, tuple[str, Any]] = {}

    def day(self, day: str) -> tuple[str, Any]:
        if day not in self._cache:
            path = self._paths.get(day)
            if path is None:
                self._cache[day] = ("missing", None)
            else:
                valid, errors, text = validate_entry_content(path, root=self.root)
                if valid:
                    self._cache[day] = ("validated", extract_issue_items(text, day))
                else:
                    self._cache[day] = ("excluded", _bounded_errors(errors or ["validation failed"]))
        return self._cache[day]

    def window(self, start: date, end: date) -> dict[str, Any]:
        validated: list[str] = []
        missing: list[str] = []
        excluded: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        cursor = start
        while cursor <= end:
            day = cursor.isoformat()
            state, value = self.day(day)
            if state == "validated":
                validated.append(day)
                items.extend(value)
            elif state == "missing":
                missing.append(day)
            else:
                excluded.append({"date": day, "errors": value})
            cursor += timedelta(days=1)
        return {
            "validated": validated,
            "missing": missing,
            "excluded": excluded,
            "items": items,
            "calendar_days": (end - start).days + 1,
        }


def _bounded_excluded(excluded: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    truncated = len(excluded) > MAX_EXCLUDED_LISTED
    listed: list[dict[str, Any]] = []
    for entry in excluded[:MAX_EXCLUDED_LISTED]:
        errors = [str(error) for error in entry.get("errors") or []]
        kept = errors[:MAX_EXCLUDED_ERRORS]
        if len(errors) > MAX_EXCLUDED_ERRORS or any(len(error) > MAX_EXCLUDED_ERROR_CHARS for error in kept):
            truncated = True
        listed.append({"date": entry["date"], "errors": [error[:MAX_EXCLUDED_ERROR_CHARS] for error in kept]})
    return listed, truncated


def _coverage(window: dict[str, Any]) -> dict[str, Any]:
    excluded, excluded_truncated = _bounded_excluded(window["excluded"])
    return {
        "calendar_days": window["calendar_days"],
        "validated_count": len(window["validated"]),
        "validated_dates": window["validated"],
        "missing_count": len(window["missing"]),
        "missing_dates": window["missing"],
        "excluded_count": len(window["excluded"]),
        "excluded": excluded,
        "excluded_truncated": excluded_truncated,
    }


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int, name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _citation(item: dict[str, Any]) -> dict[str, str]:
    return {"date": item["date"], "section": item["section"], "excerpt": item["excerpt"]}


def _bounded_citations(items: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, str]], bool]:
    """Keep the most recent citations, presented in chronological order."""
    ordered = [_citation(item) for item in items]
    return ordered[-limit:] if limit else [], len(ordered) > limit


# ---------------------------------------------------------------------------
# Feature 1: recurring issue alerts


def _label(group_items: list[dict[str, Any]]) -> tuple[str, list[str]]:
    counts: Counter[str] = Counter()
    surfaces: dict[str, Counter[str]] = {}
    for item in group_items:
        counts.update(item["terms"])
        for stem, word in item["surface"]:
            surfaces.setdefault(stem, Counter())[word] += 1
    total = len(group_items)
    shared = [stem for stem, count in counts.items() if count == total]
    if not shared:
        shared = [stem for stem, count in counts.items() if count * 2 >= total]
    shared.sort(key=lambda stem: (-counts[stem], stem))
    chosen = shared[:MAX_LABEL_TERMS]
    words = [min(surfaces[stem].items(), key=lambda pair: (-pair[1], pair[0]))[0] for stem in chosen]
    return " ".join(sorted(words)), sorted(chosen)


def _cluster(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Deterministic leader clustering.

    Items arrive in (date, section, position) order; each joins the earliest most
    similar group seed, else starts a group. Similar seeds always share a term,
    so only seeds found through the term index are compared, in seed order,
    which gives exactly the same groups as comparing every seed.
    """
    seeds: list[frozenset[str]] = []
    members: list[list[dict[str, Any]]] = []
    postings: dict[str, list[int]] = {}
    for item in items:
        terms = item["terms"]
        candidates: set[int] = set()
        for term in terms:
            candidates.update(postings.get(term, ()))
        best_index: int | None = None
        best_score = -1.0
        for index in sorted(candidates):
            seed = seeds[index]
            if not _similar(seed, terms):
                continue
            score = jaccard(seed, terms)
            if score > best_score:
                best_index, best_score = index, score
        if best_index is None:
            for term in terms:
                postings.setdefault(term, []).append(len(seeds))
            seeds.append(terms)
            members.append([item])
        else:
            members[best_index].append(item)
    return members


def recurring_issues(
    root: Path,
    start: date,
    end: date,
    *,
    min_dates: Any = None,
    max_groups: Any = None,
    max_citations: Any = None,
) -> dict[str, Any]:
    if start > end:
        raise ValueError("start date must not follow end date")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"recurring issue range may not exceed {MAX_RANGE_DAYS} calendar days")
    threshold = _bounded_int(min_dates, default=DEFAULT_MIN_DATES, minimum=MIN_MIN_DATES, maximum=MAX_MIN_DATES, name="min_dates")
    group_limit = _bounded_int(max_groups, default=DEFAULT_MAX_GROUPS, minimum=1, maximum=MAX_GROUPS, name="max_groups")
    citation_limit = _bounded_int(max_citations, default=DEFAULT_MAX_CITATIONS, minimum=1, maximum=MAX_CITATIONS, name="max_citations")
    reader = _ValidatedReader(root)
    window = reader.window(start, end)
    items = window["items"]
    items_truncated = len(items) > MAX_ITEMS
    items = items[:MAX_ITEMS]

    members = _cluster(items)

    candidates: list[dict[str, Any]] = []
    for group_items in members:
        dates = sorted({item["date"] for item in group_items})
        if len(dates) < threshold:
            continue
        label, terms = _label(group_items)
        citations, truncated = _bounded_citations(group_items, citation_limit)
        candidates.append({
            "label": label,
            "terms": terms,
            "distinct_date_count": len(dates),
            "occurrence_count": len(group_items),
            "first_date": dates[0],
            "last_date": dates[-1],
            "sections": sorted({item["section"] for item in group_items}),
            "citations": citations,
            "citations_truncated": truncated,
        })
    candidates.sort(key=lambda group: (
        -group["distinct_date_count"], -group["occurrence_count"], group["first_date"], group["label"],
    ))
    return {
        "kind": "candidate_recurring_issues",
        "notice": RECURRING_NOTICE,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "min_dates": threshold,
        "sections": list(_SECTION_KEYS),
        "similarity": {
            "method": "jaccard over normalized significant terms",
            "threshold": SIMILARITY_THRESHOLD,
            "min_shared_terms": MIN_SHARED_TERMS,
        },
        "coverage": _coverage(window),
        "item_count": len(items),
        "items_truncated": items_truncated,
        "max_items": MAX_ITEMS,
        "candidate_group_count": len(candidates),
        "groups": candidates[:group_limit],
        "groups_truncated": len(candidates) > group_limit,
        "max_groups": group_limit,
        "max_citations": citation_limit,
    }


# ---------------------------------------------------------------------------
# Feature 2: lesson ledger


def ledger_path(root: Path) -> Path:
    return root / LEDGER_DIRECTORY / LEDGER_NAME


def _iso_date(value: Any, name: str) -> date:
    if not isinstance(value, str) or _ISO_DATE.fullmatch(value) is None:
        raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD)")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid calendar date") from exc
    return parsed


def _is_forbidden_char(char: str) -> bool:
    """Control, format (including bidirectional overrides), and surrogate code points."""
    if char in "\t\n\r":
        return False  # ordinary whitespace, collapsed to single spaces below
    return unicodedata.category(char) in {"Cc", "Cf", "Cs"}


def _clean_text(value: Any, name: str, maximum: int, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    cleaned = " ".join(value.split())
    if required and not cleaned:
        raise ValueError(f"{name} must be nonempty")
    if len(cleaned) > maximum or any(_is_forbidden_char(char) for char in value):
        raise ValueError(f"{name} is malformed or exceeds {maximum} characters")
    return cleaned


def _clean_terms(value: Any, name: str, *, required: bool) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or (required and not value) or len(value) > MAX_TERMS:
        raise ValueError(f"{name} must be a list of 1 to {MAX_TERMS} terms" if required else f"{name} must be a list of at most {MAX_TERMS} terms")
    terms: list[str] = []
    for raw in value:
        term = _clean_text(raw, name, MAX_TERM_CHARS)
        if not significant_terms(term):
            raise ValueError(f"{name} term has no significant words: {term[:40]}")
        if term.casefold() not in {existing.casefold() for existing in terms}:
            terms.append(term)
    return terms


def lesson_id_for(title: str, adopted_on: str) -> str:
    material = f"{' '.join(title.split()).casefold()}\n{adopted_on}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def _utc_now(now: datetime | None) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    return current.isoformat().replace("+00:00", "Z")


def _validate_record(value: Any, lessons: dict[str, dict[str, Any]], line_number: int) -> None:
    def fail(reason: str) -> NoReturn:
        raise ValueError(f"lesson ledger is malformed at line {line_number}: {reason}")

    if not isinstance(value, dict) or value.get("schema_version") != LEDGER_SCHEMA_VERSION:
        fail("unsupported record")
    event = value.get("event")
    lesson_id = value.get("lesson_id")
    if not isinstance(lesson_id, str) or _LESSON_ID.fullmatch(lesson_id) is None:
        fail("invalid lesson_id")
    recorded_at = value.get("recorded_at")
    if not isinstance(recorded_at, str) or not recorded_at.endswith("Z"):
        fail("invalid recorded_at")
    try:
        if event == "adopt":
            if set(value) != _ADOPT_RECORD_FIELDS:
                fail("unexpected adopt fields")
            title = _clean_text(value["title"], "title", MAX_TITLE_CHARS)
            adopted = _iso_date(value["adopted_on"], "adopted_on").isoformat()
            match = value["match"]
            if not isinstance(match, dict) or set(match) != {"any_terms", "all_terms"}:
                fail("invalid match")
            any_terms = _clean_terms(match["any_terms"], "any_terms", required=True)
            all_terms = _clean_terms(match["all_terms"], "all_terms", required=False)
            sections = value["sections"]
            if not isinstance(sections, list) or not sections or any(item not in _SECTION_KEYS for item in sections):
                fail("invalid sections")
            sources = value["source_dates"]
            if not isinstance(sources, list) or len(sources) > MAX_SOURCE_DATES:
                fail("invalid source_dates")
            for item in sources:
                _iso_date(item, "source_dates")
            if lesson_id != lesson_id_for(title, adopted) or lesson_id in lessons:
                fail("duplicate or inconsistent lesson_id")
            lessons[lesson_id] = {
                "lesson_id": lesson_id,
                "title": title,
                "adopted_on": adopted,
                "any_terms": any_terms,
                "all_terms": all_terms,
                "sections": list(sections),
                "source_dates": list(sources),
                "recorded_at": recorded_at,
                "retired_on": None,
                "retire_reason": None,
            }
        elif event == "retire":
            if set(value) != _RETIRE_RECORD_FIELDS:
                fail("unexpected retire fields")
            lesson = lessons.get(lesson_id)
            if lesson is None or lesson["retired_on"] is not None:
                fail("retire event without an active lesson")
            retired = _iso_date(value["retired_on"], "retired_on").isoformat()
            if retired < lesson["adopted_on"]:
                fail("retired_on precedes adopted_on")
            reason = value["reason"]
            if reason is not None:
                _clean_text(reason, "reason", MAX_REASON_CHARS)
            lesson["retired_on"] = retired
            lesson["retire_reason"] = reason
        else:
            fail("unknown event")
    except (KeyError, TypeError) as exc:
        raise ValueError(f"lesson ledger is malformed at line {line_number}") from exc


def _parse_ledger(text: str) -> dict[str, dict[str, Any]]:
    if text and not text.endswith("\n"):
        raise ValueError("lesson ledger is malformed: incomplete final record")
    lines = text.splitlines()
    if len(lines) > MAX_LEDGER_RECORDS:
        raise ValueError("lesson ledger exceeds its record ceiling")
    lessons: dict[str, dict[str, Any]] = {}
    for number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"lesson ledger is malformed at line {number}") from exc
        _validate_record(value, lessons, number)
    return lessons


def _checked_root(root: Path) -> Path:
    reject_symlink_components(root)
    if not root.is_dir():
        raise ValueError("journal root does not exist")
    return _safe_files.canonical_descriptor_path(root)


def _read_ledger_text(root: Path) -> str:
    try:
        return _safe_files.safe_read_text(root, ledger_path(root), max_bytes=MAX_LEDGER_BYTES)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ValueError("lesson ledger path is unsafe") from exc


def load_lessons(root: Path) -> dict[str, dict[str, Any]]:
    checked = _checked_root(root)
    directory = checked / LEDGER_DIRECTORY
    if directory.is_symlink() or ledger_path(checked).is_symlink():
        raise ValueError("lesson ledger path must not be a symlink")
    return _parse_ledger(_read_ledger_text(checked))


def _adopt_record(args: dict[str, Any], today: date, now: datetime | None) -> dict[str, Any]:
    unexpected = sorted(set(args) - _ADOPT_FIELDS)
    if unexpected:
        raise ValueError(f"unexpected lesson fields: {', '.join(unexpected)}")
    title = _clean_text(args.get("title"), "title", MAX_TITLE_CHARS)
    adopted = _iso_date(args.get("adopted_on"), "adopted_on")
    if adopted > today:
        raise ValueError("adopted_on must not be in the future")
    sections = args.get("sections")
    if sections is None:
        sections = list(_SECTION_KEYS)
    if not isinstance(sections, list) or not sections or any(item not in _SECTION_KEYS for item in sections):
        raise ValueError(f"sections must be a nonempty subset of {', '.join(_SECTION_KEYS)}")
    sections = [key for key in _SECTION_KEYS if key in sections]
    sources = args.get("source_dates")
    if sources is None:
        sources = []
    if not isinstance(sources, list) or len(sources) > MAX_SOURCE_DATES:
        raise ValueError(f"source_dates must be a list of at most {MAX_SOURCE_DATES} dates")
    parsed_sources = sorted({_iso_date(item, "source_dates") for item in sources})
    if any(item > today for item in parsed_sources):
        raise ValueError("source_dates must not be in the future")
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "event": "adopt",
        "lesson_id": lesson_id_for(title, adopted.isoformat()),
        "title": title,
        "adopted_on": adopted.isoformat(),
        "match": {
            "any_terms": _clean_terms(args.get("any_terms"), "any_terms", required=True),
            "all_terms": _clean_terms(args.get("all_terms"), "all_terms", required=False),
        },
        "sections": sections,
        "source_dates": [item.isoformat() for item in parsed_sources],
        "recorded_at": _utc_now(now),
    }


def _retire_record(args: dict[str, Any], today: date, now: datetime | None) -> dict[str, Any]:
    unexpected = sorted(set(args) - _RETIRE_FIELDS)
    if unexpected:
        raise ValueError(f"unexpected retire fields: {', '.join(unexpected)}")
    lesson_id = args.get("lesson_id")
    if not isinstance(lesson_id, str) or _LESSON_ID.fullmatch(lesson_id) is None:
        raise ValueError("lesson_id must be 16 lowercase hexadecimal characters")
    retired = _iso_date(args.get("retired_on"), "retired_on")
    if retired > today:
        raise ValueError("retired_on must not be in the future")
    reason = args.get("reason")
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "event": "retire",
        "lesson_id": lesson_id,
        "retired_on": retired.isoformat(),
        "reason": _clean_text(reason, "reason", MAX_REASON_CHARS) if reason is not None else None,
        "recorded_at": _utc_now(now),
    }


def record_lesson(
    root: Path,
    args: Any,
    *,
    today: date,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append one adopt or retire event. Existing records are never edited."""
    if not isinstance(args, dict):
        raise ValueError("lesson arguments must be an object")
    action = args.get("action", "adopt")
    if action == "adopt":
        record = _adopt_record(args, today, now)
    elif action == "retire":
        record = _retire_record(args, today, now)
    else:
        raise ValueError("action must be adopt or retire")
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    warning: str | None = None
    checked = _checked_root(root)
    directory = checked / LEDGER_DIRECTORY
    if directory.is_symlink():
        raise ValueError("lesson ledger directory must not be a symlink")
    _safe_files.safe_mkdir_tree(checked, directory)
    descriptor = _safe_files.open_directory_fd(directory)
    try:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        if ledger_path(checked).is_symlink():
            raise ValueError("lesson ledger must not be a symlink")
        existing = _read_ledger_text(checked)
        lessons = _parse_ledger(existing)
        if record["event"] == "adopt" and record["lesson_id"] in lessons:
            raise ValueError(f"duplicate lesson refused: {record['lesson_id']}")
        if record["event"] == "retire":
            lesson = lessons.get(record["lesson_id"])
            if lesson is None:
                raise ValueError("unknown lesson_id")
            if lesson["retired_on"] is not None:
                raise ValueError("lesson is already retired")
            if record["retired_on"] < lesson["adopted_on"]:
                raise ValueError("retired_on must not precede adopted_on")
        if len(existing.encode("utf-8")) + len(line.encode("utf-8")) > MAX_LEDGER_BYTES:
            raise ValueError("lesson ledger would exceed its byte ceiling")
        if existing.count("\n") + 1 > MAX_LEDGER_RECORDS:
            raise ValueError("lesson ledger would exceed its record ceiling")
        _safe_files.safe_atomic_write_text(checked, ledger_path(checked), existing + line)
        # The append is durable at this point; a failed verification read must
        # not be reported as a failed write, so it becomes a warning instead.
        try:
            if not _read_ledger_text(checked).startswith(existing + line):
                warning = "the record was written durably, but the ledger changed outside the lock; review lessons.jsonl"
        except ValueError:
            warning = "the record was written durably, but the verification read failed; review lessons.jsonl"
    finally:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    result = {
        "ok": True,
        "event": record["event"],
        "lesson_id": record["lesson_id"],
        "record": record,
        "ledger_path": str(ledger_path(checked)),
        "notice": "Lesson recorded in the append only ledger. Notes, memory, and agent behavior were not changed.",
    }
    if warning is not None:
        result["warning"] = warning
    return result


# ---------------------------------------------------------------------------
# Feature 2: improvement report


def _item_matches(lesson: dict[str, Any], item: dict[str, Any]) -> bool:
    if item["section"] not in lesson["sections"]:
        return False
    terms = item["terms"]
    if not any(significant_terms(term) <= terms for term in lesson["any_terms"]):
        return False
    return all(significant_terms(term) <= terms for term in lesson["all_terms"])


def _window_summary(
    reader: _ValidatedReader,
    lesson: dict[str, Any],
    start: date,
    end: date,
    *,
    cite: bool,
) -> dict[str, Any]:
    window = reader.window(start, end)
    matches = [item for item in window["items"] if _item_matches(lesson, item)]
    dates = sorted({item["date"] for item in matches})
    summary = {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "days": window["calendar_days"],
        "validated_count": len(window["validated"]),
        "missing_count": len(window["missing"]),
        "excluded_count": len(window["excluded"]),
        "excluded_dates": [item["date"] for item in window["excluded"]],
        "matching_item_count": len(matches),
        "matching_date_count": len(dates),
        "matching_dates": dates,
    }
    if cite:
        citations, truncated = _bounded_citations(matches, MAX_REPORT_CITATIONS)
        summary["citations"] = citations
        summary["citations_truncated"] = truncated
    return summary


def _lesson_report(reader: _ValidatedReader, lesson: dict[str, Any], as_of: date) -> dict[str, Any]:
    adopted = date.fromisoformat(lesson["adopted_on"])
    end = as_of
    if lesson["retired_on"] is not None:
        end = min(end, date.fromisoformat(lesson["retired_on"]))
    base = {
        "lesson_id": lesson["lesson_id"],
        "title": lesson["title"],
        "adopted_on": lesson["adopted_on"],
        "status": "retired" if lesson["retired_on"] is not None else "active",
        "retired_on": lesson["retired_on"],
        "match": {"any_terms": lesson["any_terms"], "all_terms": lesson["all_terms"]},
        "sections": lesson["sections"],
        "source_dates": lesson["source_dates"],
        "interpretation": CORRELATION_NOTICE,
    }
    if end < adopted:
        return {
            **base,
            "windows": None,
            "verdict": "insufficient evidence",
            "verdict_reason": "the lesson was not adopted as of the requested date",
        }
    days = min((end - adopted).days + 1, WINDOW_CAP_DAYS)
    since_start = end - timedelta(days=days - 1)
    before_end = adopted - timedelta(days=1)
    before_start = adopted - timedelta(days=days)
    before = _window_summary(reader, lesson, before_start, before_end, cite=False)
    since = _window_summary(reader, lesson, since_start, end, cite=True)
    b_valid, s_valid = before["validated_count"], since["validated_count"]
    b_dates, s_dates = before["matching_date_count"], since["matching_date_count"]
    if b_valid < MIN_VALIDATED_DATES or s_valid < MIN_VALIDATED_DATES:
        verdict = "insufficient evidence"
        reason = (
            f"each window needs at least {MIN_VALIDATED_DATES} validated dates; "
            f"before has {b_valid}, since has {s_valid}"
        )
    elif b_dates == 0:
        verdict = "no baseline occurrences"
        reason = (
            f"no validated date before adoption matched (0 of {b_valid}), so improvement cannot be assessed; "
            f"{s_dates} of {s_valid} validated dates matched since adoption"
        )
    elif s_dates == 0:
        verdict = "no recurrence observed since adoption"
        reason = f"0 of {s_valid} validated dates since adoption matched; {b_dates} of {b_valid} before"
    elif s_dates * b_valid < b_dates * s_valid:
        verdict = "reduced"
        reason = f"{s_dates} of {s_valid} validated dates matched since adoption versus {b_dates} of {b_valid} before"
    else:
        verdict = "not improved"
        reason = f"{s_dates} of {s_valid} validated dates matched since adoption versus {b_dates} of {b_valid} before"
    return {**base, "windows": {"before": before, "since": since}, "verdict": verdict, "verdict_reason": reason}


def improvement_report(
    root: Path,
    *,
    as_of: date,
    today: date,
    lesson_id: str | None = None,
) -> dict[str, Any]:
    if as_of > today:
        raise ValueError("as_of must not be in the future")
    if lesson_id is not None and (not isinstance(lesson_id, str) or _LESSON_ID.fullmatch(lesson_id) is None):
        raise ValueError("lesson_id must be 16 lowercase hexadecimal characters")
    lessons = load_lessons(root)
    if lesson_id is not None:
        if lesson_id not in lessons:
            raise ValueError("unknown lesson_id")
        selected = [lessons[lesson_id]]
    else:
        selected = sorted(lessons.values(), key=lambda item: (item["adopted_on"], item["lesson_id"]))
    reader = _ValidatedReader(root)
    reports = [_lesson_report(reader, lesson, as_of) for lesson in selected[:MAX_REPORT_LESSONS]]
    return {
        "kind": "improvement_report",
        "as_of": as_of.isoformat(),
        "notice": CORRELATION_NOTICE,
        "parameters": {
            "window_cap_days": WINDOW_CAP_DAYS,
            "min_validated_dates_per_window": MIN_VALIDATED_DATES,
            "match": "every significant term of a match term must appear in one issue item",
        },
        "lesson_count": len(selected),
        "lessons": reports,
        "lessons_truncated": len(selected) > MAX_REPORT_LESSONS,
    }
