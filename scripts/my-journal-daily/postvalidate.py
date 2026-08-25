#!/usr/bin/env python3
"""Fail closed unless scheduled synthesis completed the exact frozen Journal run."""

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
    package_name = "my_journal_daily_postvalidate_runtime"
    spec = importlib.util.spec_from_file_location(
        package_name, plugin_root / "__init__.py", submodule_search_locations=[str(plugin_root)]
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
        return bridge.invoke_wsl("journal_internal_postvalidate_daily", {}, **bridge._bridge_paths())
    operations = importlib.import_module(f"{package_name}.operations")
    return operations.postvalidate_daily_generation()


def _error(value: object) -> str:
    return " ".join(str(value or "unknown error").split())[:_MAX_ERROR_CHARS]


def _emit(value: dict) -> None:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    if len(text) > MAX_OUTPUT_CHARS:
        text = json.dumps({"ok": False, "error": "postvalidation output exceeded its bound"}, sort_keys=True, separators=(",", ":"))
    print(text)


def main() -> int:
    try:
        result = _run_daily()
    except Exception as exc:
        _emit({"ok": False, "error": _error(exc)})
        return 1
    if result.get("ok") is not True:
        _emit({key: value for key, value in {
            "ok": False, "binding_id": result.get("binding_id"), "run_id": result.get("run_id"),
            "journal_date": result.get("journal_date"), "error": _error(result.get("error")),
        }.items() if value is not None})
        return 1
    _emit({key: result[key] for key in ("ok", "binding_id", "run_id", "journal_date", "validated") if key in result})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
