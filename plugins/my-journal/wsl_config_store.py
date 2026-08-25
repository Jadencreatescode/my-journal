#!/usr/bin/env python3
"""Descriptor anchored WSL storage for the native Windows Journal binding."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath

MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 4096
_ROOT = re.compile(r"/mnt/[a-z](?:/[^\x00]*)?")
_CONFIG = Path(".my-journal/wsl-runtime.json")


def _safe_files_module():
    path = (
        Path(__file__).resolve().parents[2]
        / "skills" / "note-taking" / "my-journal" / "scripts" / "safe_files.py"
    )
    spec = importlib.util.spec_from_file_location("my_journal_wsl_config_safe_files", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("safe Journal filesystem module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validated_root(value: object) -> Path:
    if not isinstance(value, str) or not _ROOT.fullmatch(value):
        raise ValueError("Windows Hermes home must map to a normalized WSL drive path")
    path = PurePosixPath(value)
    if any(part in {".", ".."} for part in path.parts):
        raise ValueError("Windows Hermes home must map to a normalized WSL drive path")
    return Path(str(path))


def _validated_wsl_home(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("WSL Hermes home must be an absolute normalized POSIX path")
    path = PurePosixPath(value)
    normalized = str(path)
    if not path.is_absolute() or normalized == "/" or normalized != value.rstrip("/"):
        raise ValueError("WSL Hermes home must be an absolute normalized POSIX path")
    if any(part in {".", ".."} for part in path.parts):
        raise ValueError("WSL Hermes home must be an absolute normalized POSIX path")
    return normalized


def dispatch(request: object) -> dict:
    if not isinstance(request, dict) or set(request) not in (
        {"operation", "root"},
        {"operation", "root", "wsl_hermes_home"},
    ):
        raise ValueError("malformed WSL Journal configuration request")
    operation = request.get("operation")
    root = _validated_root(request.get("root"))
    safe = _safe_files_module()
    root = safe.canonical_descriptor_path(root)
    descriptor = safe.open_directory_fd(root)
    os.close(descriptor)
    target = root / _CONFIG
    if operation == "read":
        if set(request) != {"operation", "root"}:
            raise ValueError("malformed WSL Journal configuration read")
        try:
            text = safe.safe_read_text(root, target, max_bytes=MAX_RESPONSE_BYTES)
        except FileNotFoundError:
            return {"exists": False}
        value = json.loads(text)
        if not isinstance(value, dict) or set(value) != {"schema_version", "wsl_hermes_home"}:
            raise ValueError("malformed My Journal WSL runtime configuration")
        if value.get("schema_version") != 1:
            raise ValueError("unsupported My Journal WSL runtime configuration")
        return {"exists": True, "wsl_hermes_home": _validated_wsl_home(value.get("wsl_hermes_home"))}
    if operation == "write":
        if set(request) != {"operation", "root", "wsl_hermes_home"}:
            raise ValueError("malformed WSL Journal configuration write")
        home = _validated_wsl_home(request.get("wsl_hermes_home"))
        parent_descriptor = safe._open_or_create_directory(target.parent, 0o700)
        os.close(parent_descriptor)
        payload = json.dumps(
            {"schema_version": 1, "wsl_hermes_home": home},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        safe.safe_atomic_write_text(root, target, payload)
        return {"ok": True}
    raise ValueError("unsupported WSL Journal configuration operation")


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise SystemExit("WSL Journal configuration request exceeds byte ceiling")
    try:
        request = json.loads(raw.decode("utf-8"))
        result = dispatch(request)
        output = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    except Exception as exc:
        output = json.dumps({"error": str(exc)[:2000]}, ensure_ascii=False, separators=(",", ":"))
    if len(output.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise SystemExit("WSL Journal configuration response exceeds byte ceiling")
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
