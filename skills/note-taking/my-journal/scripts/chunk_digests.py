"""Deterministic, resumable digest receipts for My Journal packet chunks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from atomic_files import atomic_write_text
from collect_journal import reject_symlink_components
from safe_files import canonical_descriptor_path, safe_mkdir_tree, safe_read_text


_RESERVED_PREFIXES = (
    "Digest Schema:",
    "Run ID:",
    "Evidence SHA256:",
    "Chunk Index:",
    "Chunk ID:",
    "Chunk File SHA256:",
    "Digest Body SHA256:",
    "Session Ref:",
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _journal_root(plan_path: Path) -> Path:
    absolute = canonical_descriptor_path(plan_path)
    try:
        root = absolute.parents[4]
    except IndexError as exc:
        raise ValueError("packet plan path has no journal root") from exc
    if absolute.parents[3].name != "packets":
        raise ValueError("packet plan is outside the journal packets tree")
    return root


def _inside_lexically(path: Path, parent: Path) -> bool:
    try:
        canonical_descriptor_path(path).relative_to(canonical_descriptor_path(parent))
        return True
    except ValueError:
        return False


def _header_values(text: str) -> tuple[dict[str, str], list[str], str]:
    try:
        header, body = text.split("\n---\n", 1)
    except ValueError as exc:
        raise ValueError("digest receipt is missing its provenance boundary") from exc
    values: dict[str, str] = {}
    session_refs: list[str] = []
    for line in header.splitlines():
        if line.startswith("Session Ref: "):
            session_refs.append(line.removeprefix("Session Ref: "))
        elif ": " in line:
            label, value = line.split(": ", 1)
            values[label] = value
    return values, session_refs, body


def load_packet_plan(plan_path: Path) -> dict[str, Any]:
    """Load and verify an immutable packet plan and every indexed chunk."""
    plan_path = canonical_descriptor_path(plan_path)
    journal_root = _journal_root(plan_path)
    try:
        plan = json.loads(safe_read_text(journal_root, plan_path, max_bytes=2_000_000))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"packet plan could not be read: {exc}") from exc
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise ValueError("packet plan schema is unsupported")
    run_id = plan.get("run_id")
    evidence_sha256 = plan.get("evidence_sha256")
    chunks = plan.get("chunks")
    if not isinstance(run_id, str) or not isinstance(evidence_sha256, str):
        raise ValueError("packet plan provenance is malformed")
    if not isinstance(chunks, list) or plan.get("chunk_count") != len(chunks):
        raise ValueError("packet plan chunk count is inconsistent")
    expected_indices = list(range(1, len(chunks) + 1))
    if [item.get("index") if isinstance(item, dict) else None for item in chunks] != expected_indices:
        raise ValueError("packet plan indices are not contiguous")
    index_chunks = [
        {
            key: item.get(key)
            for key in (
                "index", "chunk_id", "body_sha256", "sha256", "bytes",
                "owned_session_refs", "continued_session_refs",
            )
        }
        for item in chunks
        if isinstance(item, dict)
    ]
    expected_index_hash = _sha256_bytes(
        json.dumps(index_chunks, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    if plan.get("chunk_index_sha256") != expected_index_hash:
        raise ValueError("packet plan chunk index hash is invalid")

    run_dir = plan_path.parent
    for item in chunks:
        if not isinstance(item, dict):
            raise ValueError("packet plan chunk must be an object")
        chunk_path = Path(str(item.get("path", ""))).expanduser().absolute()
        if not _inside_lexically(chunk_path, run_dir):
            raise ValueError("packet chunk path is outside its run directory")
        text = safe_read_text(journal_root, chunk_path, max_bytes=2_000_000)
        raw = text.encode("utf-8")
        if _sha256_bytes(raw) != item.get("sha256"):
            raise ValueError("packet chunk file hash does not match its plan")
        header, body = text.split("\n---\n", 1)
        values = {
            line.split(": ", 1)[0]: line.split(": ", 1)[1]
            for line in header.splitlines()
            if ": " in line
        }
        if values.get("Run ID") != run_id or values.get("Evidence SHA256") != evidence_sha256:
            raise ValueError("packet chunk provenance does not match its plan")
        if values.get("Chunk ID") != item.get("chunk_id"):
            raise ValueError("packet chunk identity does not match its plan")
        if values.get("Chunk Body SHA256") != item.get("body_sha256"):
            raise ValueError("packet chunk body hash header does not match its plan")
        if _sha256_bytes(body.encode("utf-8")) != item.get("body_sha256"):
            raise ValueError("packet chunk body hash is invalid")
    return plan


def _expected_digest_dir(plan_path: Path, plan: dict[str, Any]) -> Path:
    return _journal_root(plan_path) / "runs" / plan["run_id"] / "digests"


def _digest_path(digest_dir: Path, chunk: dict[str, Any]) -> Path:
    return digest_dir / f"{chunk['index']:06d}-{chunk['chunk_id'][:12]}.md"


def _render_receipt(plan: dict[str, Any], chunk: dict[str, Any], body: str) -> str:
    normalized = body.rstrip() + "\n"
    body_sha256 = _sha256_bytes(normalized.encode("utf-8"))
    lines = [
        "Digest Schema: 2",
        f"Run ID: {plan['run_id']}",
        f"Evidence SHA256: {plan['evidence_sha256']}",
        f"Chunk Index: {chunk['index']}/{plan['chunk_count']}",
        f"Chunk ID: {chunk['chunk_id']}",
        f"Chunk File SHA256: {chunk['sha256']}",
        f"Digest Body SHA256: {body_sha256}",
    ]
    lines.extend(f"Session Ref: {ref}" for ref in chunk["owned_session_refs"])
    return "\n".join(lines + ["---", normalized])


def _validate_receipt(text: str, plan: dict[str, Any], chunk: dict[str, Any]) -> None:
    values, session_refs, body = _header_values(text)
    expected = {
        "Digest Schema": "2",
        "Run ID": plan["run_id"],
        "Evidence SHA256": plan["evidence_sha256"],
        "Chunk Index": f"{chunk['index']}/{plan['chunk_count']}",
        "Chunk ID": chunk["chunk_id"],
        "Chunk File SHA256": chunk["sha256"],
        "Digest Body SHA256": _sha256_bytes(body.encode("utf-8")),
    }
    if any(values.get(label) != value for label, value in expected.items()):
        raise ValueError("digest receipt provenance is invalid")
    if session_refs != chunk["owned_session_refs"]:
        raise ValueError("digest receipt session ownership is invalid")


def next_pending_chunk(plan_path: Path, digest_dir: Path) -> dict[str, Any] | None:
    """Return the first chunk lacking one valid receipt, or None when complete."""
    plan = load_packet_plan(plan_path)
    expected_dir = canonical_descriptor_path(_expected_digest_dir(plan_path, plan))
    digest_dir = canonical_descriptor_path(digest_dir)
    if digest_dir != expected_dir:
        raise ValueError("digest directory is outside the owned run directory")
    reject_symlink_components(digest_dir)
    for chunk in plan["chunks"]:
        path = _digest_path(digest_dir, chunk)
        if not path.exists():
            return dict(chunk)
        text = safe_read_text(_journal_root(plan_path), path, max_bytes=1_000_000)
        _validate_receipt(text, plan, chunk)
    return None


def accept_chunk_digest(
    plan_path: Path,
    digest_dir: Path,
    chunk_id: str,
    digest_body: str,
) -> dict[str, Any]:
    """Atomically accept one code-bound digest body with idempotent replay."""
    if not isinstance(digest_body, str) or not digest_body.strip():
        raise ValueError("digest body must be nonempty text")
    if any(line.startswith(_RESERVED_PREFIXES) for line in digest_body.splitlines()):
        raise ValueError("digest body contains reserved provenance")
    plan = load_packet_plan(plan_path)
    matches = [item for item in plan["chunks"] if item.get("chunk_id") == chunk_id]
    if len(matches) != 1:
        raise ValueError("chunk identity is not present exactly once in the packet plan")
    chunk = matches[0]
    expected_dir = canonical_descriptor_path(_expected_digest_dir(plan_path, plan))
    digest_dir = canonical_descriptor_path(digest_dir)
    if digest_dir != expected_dir:
        raise ValueError("digest directory is outside the owned run directory")
    reject_symlink_components(digest_dir)
    receipt_text = _render_receipt(plan, chunk, digest_body)
    receipt_path = _digest_path(digest_dir, chunk)
    if receipt_path.exists() or receipt_path.is_symlink():
        existing = safe_read_text(
            _journal_root(plan_path), receipt_path, max_bytes=1_000_000
        )
        _validate_receipt(existing, plan, chunk)
        if existing != receipt_text:
            raise ValueError("chunk already has a different digest")
    else:
        safe_mkdir_tree(_journal_root(plan_path), digest_dir)
        reject_symlink_components(digest_dir)
        atomic_write_text(receipt_path, receipt_text, trusted_root=_journal_root(plan_path))
    return {
        "chunk_id": chunk_id,
        "digest_path": str(receipt_path),
        "digest_sha256": _sha256_bytes(receipt_text.encode("utf-8")),
        "status": "complete",
    }
