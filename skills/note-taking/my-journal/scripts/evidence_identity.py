"""Canonical evidence identity for My Journal manifests."""

from __future__ import annotations

import hashlib
import json
from typing import Any


_CREATION_FIELDS = {"run_id", "evidence_sha256", "created_at", "delivery"}


def canonical_evidence_sha256(manifest: dict[str, Any]) -> str:
    """Hash collected evidence while excluding fields created from that hash."""
    evidence = {key: value for key, value in manifest.items() if key not in _CREATION_FIELDS}
    encoded = json.dumps(
        evidence,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
