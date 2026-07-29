"""Shared secret detection and redaction for My Journal evidence and notes."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Mapping


_REDACTED = "[REDACTED]"
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PHONE = re.compile(r"(?<![A-Za-z0-9-])(?:\+?\d[\d ().-]{7,}\d)(?![A-Za-z0-9-])")
_OPAQUE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{24,}(?![A-Za-z0-9_-])")
_UUID = re.compile(
    r"(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_HEX = re.compile(r"(?i)^[0-9a-f]{32,128}$")


@dataclass(frozen=True)
class RedactionResult:
    text: str
    finding_counts: Mapping[str, int]

_AUTH_HEADER = re.compile(
    r"(?im)\bauthorization[ \t]*:[ \t]*+(?!\[REDACTED\][ \t]*$)[^\r\n]+$"
)
_COOKIE_HEADER = re.compile(
    r"(?im)\b(?:set-cookie|cookie)[ \t]*:[ \t]*+(?!\[REDACTED\][ \t]*$)[^\r\n]+$"
)
_AUTH_SCHEME = re.compile(
    r'''(?ix)\b(?:basic|bearer)\s+(?!\[REDACTED\])(?:"[^"\r\n]*"|'[^'\r\n]*'|[A-Za-z0-9._~+/=-]{8,})'''
)
_AUTH_ASSIGNMENT = re.compile(
    r'''(?ix)
    (?P<prefix>["']?authorization["']?[ \t]*+[:=][ \t]*+)
    (?!\[REDACTED\](?:[ \t]*[,}\]]|[ \t]*(?:\r?\n|$)))
    (?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^,}\]\r\n]+)
    '''
)
_ASSIGNMENT = re.compile(
    r'''(?ix)
    (?P<prefix>["']?(?:password|passwd|pwd|token|api[_ -]?key|secret|
      client[_ -]?secret|access[_ -]?key|authorization)["']?\s*[:=]\s*)
    (?!\[REDACTED(?:[^\]]*)\])
    (?P<value>"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;&}\]\r\n]+)
    '''
)
_GITHUB = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_AWS_ACCESS_ID = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_GOOGLE_API_KEY = re.compile(r"\bAIza[A-Za-z0-9_-]{20,}\b")
_HUGGING_FACE_TOKEN = re.compile(r"\bhf_[A-Za-z0-9]{20,}\b")
_STRIPE_TOKEN = re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{8,}\b")
_PROVIDER_TOKEN = re.compile(r"\b(?:sk|xox[baprs])-[A-Za-z0-9_-]{8,}\b")
_TELEGRAM_BOT_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
_DISCORD_BOT_TOKEN = re.compile(
    r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,}\b"
)
_GOOGLE_OAUTH_TOKEN = re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}\b")
_GOOGLE_REFRESH_TOKEN = re.compile(r"\b1//[A-Za-z0-9_-]{20,}\b")
_TWILIO_API_KEY = re.compile(r"\bSK[0-9a-fA-F]{32}\b")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.DOTALL,
)
_URI_USERINFO = re.compile(
    r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)(?!\[REDACTED\]@)[^\s/@]+(@)"
)


def _secret_patterns() -> tuple[re.Pattern[str], ...]:
    return (
        _AUTH_HEADER,
        _COOKIE_HEADER,
        _AUTH_ASSIGNMENT,
        _AUTH_SCHEME,
        _ASSIGNMENT,
        _GITHUB,
        _AWS_ACCESS_ID,
        _GOOGLE_API_KEY,
        _HUGGING_FACE_TOKEN,
        _STRIPE_TOKEN,
        _JWT,
        _PROVIDER_TOKEN,
        _TELEGRAM_BOT_TOKEN,
        _DISCORD_BOT_TOKEN,
        _GOOGLE_OAUTH_TOKEN,
        _GOOGLE_REFRESH_TOKEN,
        _TWILIO_API_KEY,
        _PRIVATE_KEY,
        _URI_USERINFO,
    )


def contains_likely_secret(text: str) -> bool:
    """Return true when text contains a supported unredacted credential shape."""
    return any(pattern.search(text) for pattern in _secret_patterns())


def redact_text(text: str) -> str:
    """Redact supported credential shapes without trying to reconstruct values."""
    text = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    text = _AUTH_HEADER.sub("Authorization: [REDACTED]", text)
    text = _COOKIE_HEADER.sub("Cookie: [REDACTED]", text)
    text = _AUTH_ASSIGNMENT.sub(lambda match: match.group("prefix") + _REDACTED, text)
    text = _AUTH_SCHEME.sub("Authorization [REDACTED]", text)
    text = _URI_USERINFO.sub(r"\1[REDACTED]\2", text)
    text = _GITHUB.sub(_REDACTED, text)
    text = _AWS_ACCESS_ID.sub(_REDACTED, text)
    text = _GOOGLE_API_KEY.sub(_REDACTED, text)
    text = _HUGGING_FACE_TOKEN.sub(_REDACTED, text)
    text = _STRIPE_TOKEN.sub(_REDACTED, text)
    text = _JWT.sub(_REDACTED, text)
    text = _PROVIDER_TOKEN.sub(_REDACTED, text)
    text = _TELEGRAM_BOT_TOKEN.sub(_REDACTED, text)
    text = _DISCORD_BOT_TOKEN.sub(_REDACTED, text)
    text = _GOOGLE_OAUTH_TOKEN.sub(_REDACTED, text)
    text = _GOOGLE_REFRESH_TOKEN.sub(_REDACTED, text)
    text = _TWILIO_API_KEY.sub(_REDACTED, text)
    return _ASSIGNMENT.sub(lambda match: match.group("prefix") + _REDACTED, text)


def _looks_high_entropy(value: str) -> bool:
    if _UUID.fullmatch(value) or _HEX.fullmatch(value):
        return False
    if len(set(value)) < 8:
        return False
    return (
        any(char.islower() for char in value)
        and any(char.isupper() for char in value)
        and any(char.isdigit() for char in value)
    )


def redact_sensitive(
    value: str,
    *,
    pii_mode: str = "mask",
    entropy_mode: str = "report",
) -> RedactionResult:
    """Redact credentials and policy-selected PII with safe category counts."""
    if pii_mode not in {"mask", "preserve"}:
        raise ValueError("pii_mode must equal mask or preserve")
    if entropy_mode not in {"report", "redact", "off"}:
        raise ValueError("entropy_mode must equal report, redact, or off")
    counts: Counter[str] = Counter()
    text = redact_text(value)
    if text != value:
        counts["secret"] += 1

    if pii_mode == "mask":
        def mask_email(match: re.Match[str]) -> str:
            counts["email"] += 1
            return "[REDACTED:EMAIL]"

        def mask_phone(match: re.Match[str]) -> str:
            if sum(char.isdigit() for char in match.group(0)) < 10:
                return match.group(0)
            counts["phone"] += 1
            return "[REDACTED:PHONE]"

        text = _EMAIL.sub(mask_email, text)
        text = _PHONE.sub(mask_phone, text)

    if entropy_mode != "off":
        def mask_entropy(match: re.Match[str]) -> str:
            candidate = match.group(0)
            if not _looks_high_entropy(candidate):
                return candidate
            counts["high_entropy"] += 1
            return "[REDACTED:HIGH_ENTROPY]"

        text = _OPAQUE.sub(mask_entropy, text)
    return RedactionResult(text=text, finding_counts=dict(counts))


def sensitive_finding_counts(
    value: str,
    *,
    pii_mode: str = "mask",
    entropy_mode: str = "report",
) -> Mapping[str, int]:
    """Report categories that policy-selected redaction would remove."""
    return redact_sensitive(
        value,
        pii_mode=pii_mode,
        entropy_mode=entropy_mode,
    ).finding_counts
