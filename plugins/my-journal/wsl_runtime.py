#!/usr/bin/env python3
"""Restricted WSL runtime for the native Windows My Journal bridge."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


MAX_REQUEST_BYTES = 2_000_000
_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "operation",
        "args",
        "windows_home",
        "windows_journal_root",
        "wsl_hermes_home",
        "wsl_journal_root",
        "windows_executable",
        "windows_python",
        "windows_plugin_root",
    }
)
_TOOL_HANDLERS = {
    "journal_status": "handle_status",
    "journal_resolve_range": "handle_resolve_range",
    "journal_read_entries": "handle_read_entries",
    "journal_find_gaps": "handle_find_gaps",
    "journal_plan_backfill": "handle_plan_backfill",
    "journal_setup_inventory": "handle_setup_inventory",
    "journal_setup_database_approve": "handle_setup_database_approve",
    "journal_daily_workload_check": "handle_daily_workload_check",
    "journal_daily_workload_approve": "handle_daily_workload_approve",
    "journal_setup_plan": "handle_setup_plan",
    "journal_setup_approve": "handle_setup_approve",
    "journal_generation_collect": "handle_generation_collect",
    "journal_generation_resume": "handle_generation_resume",
    "journal_generation_get_chunk": "handle_generation_get_chunk",
    "journal_generation_record_digest": "handle_generation_record_digest",
    "journal_generation_complete": "handle_generation_complete",
}
_INTERNAL_HANDLERS = {
    "journal_internal_precollect_daily": (
        "operations", "precollect_daily_generation"
    ),
    "journal_internal_postvalidate_daily": (
        "operations", "postvalidate_daily_generation"
    ),
    "journal_internal_schedule_create": (
        "operations", "schedule_create_from_bridge"
    ),
    "journal_internal_schedule_remove": (
        "operations", "schedule_remove_from_bridge"
    ),
}
_INTERNAL_ARGUMENT_OPERATIONS = frozenset({
    "journal_internal_schedule_create", "journal_internal_schedule_remove",
})


def windows_to_wsl_path(value: object) -> str:
    if not isinstance(value, str) or not value or value.startswith(("\\\\", "//")):
        raise ValueError("Windows path must be an absolute local drive path")
    path = PureWindowsPath(value)
    if not path.is_absolute() or len(path.drive) != 2 or path.drive[1] != ":":
        raise ValueError("Windows path must be an absolute local drive path")
    parts = [part for part in path.parts[1:] if part not in ("", "\\", "/")]
    if any(part in (".", "..") for part in parts):
        raise ValueError("Windows path must be normalized")
    suffix = "/".join(parts)
    base = f"/mnt/{path.drive[0].lower()}"
    return f"{base}/{suffix}" if suffix else base


def _validated_request(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _REQUEST_FIELDS:
        raise ValueError("malformed Journal bridge request")
    if value.get("schema_version") != 1 or not isinstance(value.get("args"), dict):
        raise ValueError("malformed Journal bridge request")
    operation = value.get("operation")
    if operation not in _TOOL_HANDLERS and operation not in _INTERNAL_HANDLERS:
        raise ValueError("unsupported Journal bridge operation")
    for key in (
        "windows_home",
        "windows_journal_root",
        "windows_executable",
        "windows_python",
        "windows_plugin_root",
    ):
        windows_to_wsl_path(value.get(key))
    wsl_home = _validated_wsl_path(value.get("wsl_hermes_home"))
    wsl_journal = _validated_wsl_path(value.get("wsl_journal_root"))
    if wsl_journal != f"{wsl_home}/journal":
        raise ValueError("Journal bridge WSL roots do not match")
    return value


def _validated_wsl_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("Journal bridge WSL path is invalid")
    path = PurePosixPath(value)
    normalized = str(path)
    if not path.is_absolute() or normalized != value.rstrip("/") or any(
        part in (".", "..") for part in path.parts
    ):
        raise ValueError("Journal bridge WSL path is invalid")
    return normalized


def _prepare_environment(request: dict[str, Any]) -> None:
    os.environ["HERMES_HOME"] = request["wsl_hermes_home"]
    os.environ["MY_JOURNAL_ROOT"] = request["wsl_journal_root"]
    os.environ["HERMES_EXECUTABLE"] = windows_to_wsl_path(request["windows_executable"])
    os.environ["MY_JOURNAL_WINDOWS_PYTHON"] = windows_to_wsl_path(request["windows_python"])
    os.environ["MY_JOURNAL_WINDOWS_HERMES_HOME"] = request["windows_home"]
    os.environ["MY_JOURNAL_WINDOWS_JOURNAL_ROOT"] = request["windows_journal_root"]
    os.environ["MY_JOURNAL_WINDOWS_PLUGIN_ROOT"] = request["windows_plugin_root"]
    os.environ["MY_JOURNAL_IMMUTABLE_DATABASES"] = "1"


def _load_plugin(plugin_root: Path):
    package_name = "my_journal_wsl_runtime"
    spec = importlib.util.spec_from_file_location(
        package_name,
        plugin_root / "__init__.py",
        submodule_search_locations=[str(plugin_root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the WSL Journal runtime")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return module


def dispatch(value: object, *, plugin=None) -> dict[str, Any]:
    request = _validated_request(value)
    _prepare_environment(request)
    loaded = plugin or _load_plugin(Path(windows_to_wsl_path(request["windows_plugin_root"])))
    operation = request["operation"]
    if operation in _INTERNAL_HANDLERS:
        module_name, handler_name = _INTERNAL_HANDLERS[operation]
        handler = getattr(getattr(loaded, module_name), handler_name)
        result = handler(request["args"]) if operation in _INTERNAL_ARGUMENT_OPERATIONS else handler()
        if not isinstance(result, dict):
            raise ValueError("Journal runtime handler returned a nonobject response")
        return result
    handler = getattr(loaded.tools, _TOOL_HANDLERS[operation])
    raw = handler(request["args"])
    try:
        result = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Journal runtime handler returned malformed JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("Journal runtime handler returned a nonobject response")
    return result


def main() -> int:
    data = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(data) > MAX_REQUEST_BYTES:
        print("Journal bridge request exceeds its byte ceiling", file=sys.stderr)
        return 2
    try:
        value = json.loads(data.decode("utf-8"))
        result = dispatch(value)
    except Exception as exc:
        print(str(exc)[:2000], file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
