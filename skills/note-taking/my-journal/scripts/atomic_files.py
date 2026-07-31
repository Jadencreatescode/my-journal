"""Atomic UTF8 text writes for My Journal artifacts."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from safe_files import safe_atomic_write_text


def atomic_write_text(
    path: Path,
    text: str,
    *,
    trusted_root: Path | None = None,
    temporary_parent: Path | None = None,
) -> None:
    """Replace a text file atomically after flushing its temporary file."""
    if trusted_root is not None:
        safe_atomic_write_text(
            trusted_root,
            path,
            text,
            temporary_parent=temporary_parent,
        )
        return
    if temporary_parent is not None:
        raise ValueError("temporary_parent requires trusted_root")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
