#!/usr/bin/env python3
"""Freeze one Journal date before the scheduled agent opens SessionDB."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path


MAX_OUTPUT_CHARS = 2000
_MAX_ERROR_CHARS = 1500


def _hermes_home() -> Path:
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        home = Path(configured).expanduser().absolute()
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if not local:
            raise ValueError("native Windows Hermes home is unavailable")
        home = Path(local) / "hermes"
    else:
        home = Path.home() / ".hermes"
    metadata = home.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("Hermes home is unsafe")
    return home


def _load_plugin(home: Path) -> str:
    plugin_root = home / "plugins" / "my-journal"
    metadata = plugin_root.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("installed My Journal plugin is unsafe")
    package_name = "my_journal_daily_precollect_runtime"
    spec = importlib.util.spec_from_file_location(
        package_name,
        plugin_root / "__init__.py",
        submodule_search_locations=[str(plugin_root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("installed My Journal plugin could not be loaded")
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    return package_name


def _run_daily() -> dict:
    package_name = _load_plugin(_hermes_home())
    if sys.platform == "win32":
        bridge = importlib.import_module(f"{package_name}.windows_bridge")
        return bridge.invoke_wsl(
            "journal_internal_precollect_daily", {}, **bridge._bridge_paths()
        )
    operations = importlib.import_module(f"{package_name}.operations")
    return operations.precollect_daily_generation()


def _error(value: object) -> str:
    return " ".join(str(value or "unknown error").split())[:_MAX_ERROR_CHARS]


def _emit(value: dict) -> None:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(text) > MAX_OUTPUT_CHARS:
        text = json.dumps(
            {"ok": False, "error": "precollection output exceeded its bound"},
            sort_keys=True,
            separators=(",", ":"),
        )
    print(text)


def main() -> int:
    try:
        result = _run_daily()
    except Exception as exc:
        _emit({"ok": False, "error": _error(exc)})
        return 1
    journal_date = result.get("journal_date")
    if result.get("ok") is not True:
        _emit({
            "ok": False,
            "journal_date": journal_date,
            "error": _error(result.get("error") or result.get("reason")),
        })
        return 1
    output = {
        "ok": True,
        "journal_date": journal_date,
        "wakeAgent": result.get("wakeAgent") is not False,
    }
    for key in (
        "binding_id", "run_id", "receipt_sha256", "manifest_sha256",
        "packet_plan_sha256", "chunk_count", "resumed", "already_validated",
    ):
        if key in result:
            output[key] = result[key]
    _emit(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())