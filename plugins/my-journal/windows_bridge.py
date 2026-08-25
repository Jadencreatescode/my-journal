"""Bounded native Windows to WSL transport for My Journal."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable

from .schemas import (
    BACKFILL_SCHEMA,
    DAILY_WORKLOAD_APPROVE_SCHEMA,
    DAILY_WORKLOAD_CHECK_SCHEMA,
    GAPS_SCHEMA,
    GENERATION_COLLECT_SCHEMA,
    GENERATION_COMPLETE_SCHEMA,
    GENERATION_GET_CHUNK_SCHEMA,
    GENERATION_RECORD_DIGEST_SCHEMA,
    GENERATION_RESUME_SCHEMA,
    RANGE_SCHEMA,
    READ_SCHEMA,
    SETUP_APPROVE_SCHEMA,
    SETUP_DATABASE_APPROVE_SCHEMA,
    SETUP_INVENTORY_SCHEMA,
    SETUP_PLAN_SCHEMA,
    STATUS_SCHEMA,
)


GENERATION_TOOLSET = "my-journal-generation"
TOOL_SCHEMAS = {
    schema["name"]: schema
    for schema in (
        STATUS_SCHEMA,
        RANGE_SCHEMA,
        READ_SCHEMA,
        GAPS_SCHEMA,
        BACKFILL_SCHEMA,
        SETUP_INVENTORY_SCHEMA,
        SETUP_DATABASE_APPROVE_SCHEMA,
        DAILY_WORKLOAD_CHECK_SCHEMA,
        DAILY_WORKLOAD_APPROVE_SCHEMA,
        SETUP_PLAN_SCHEMA,
        SETUP_APPROVE_SCHEMA,
        GENERATION_COLLECT_SCHEMA,
        GENERATION_RESUME_SCHEMA,
        GENERATION_GET_CHUNK_SCHEMA,
        GENERATION_RECORD_DIGEST_SCHEMA,
        GENERATION_COMPLETE_SCHEMA,
    )
}


MAX_REQUEST_BYTES = 2_000_000
MAX_RESPONSE_BYTES = 4_000_000
_TIMEOUT_SECONDS = 360
_SETUP_APPROVAL_TIMEOUT_SECONDS = 12 * 60 * 60
_OPERATION = re.compile(r"journal_[a-z0-9_]+")
_DISTRO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_MAX_RUNTIME_CONFIG_BYTES = 4096


def _validated_wsl_home(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("WSL Hermes home must be an absolute normalized POSIX path")
    path = PurePosixPath(value)
    if not path.is_absolute() or any(part in (".", "..") for part in path.parts):
        raise ValueError("WSL Hermes home must be an absolute normalized POSIX path")
    normalized = str(path)
    if normalized != value.rstrip("/") or normalized == "/":
        raise ValueError("WSL Hermes home must be an absolute normalized POSIX path")
    return normalized


def _write_wsl_runtime_config(windows_home: Path, wsl_hermes_home: str) -> None:
    normalized = _validated_wsl_home(wsl_hermes_home)
    result = _config_store_request("write", windows_home, wsl_hermes_home=normalized)
    if result.get("ok") is not True:
        raise ValueError(str(result.get("error") or "WSL Journal runtime configuration write failed")[:2000])


def _configured_wsl_roots(windows_home: Path) -> tuple[str, str]:
    result = _config_store_request("read", windows_home)
    if result.get("exists") is False:
        mapped_home = windows_to_wsl_path(windows_home)
        return mapped_home, f"{mapped_home}/journal"
    if result.get("exists") is not True:
        raise ValueError(str(result.get("error") or "WSL Journal runtime configuration read failed")[:2000])
    wsl_home = _validated_wsl_home(result.get("wsl_hermes_home"))
    return wsl_home, f"{wsl_home}/journal"


def windows_to_wsl_path(value: object) -> str:
    """Map one absolute local drive path into its standard WSL mount path."""
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("Windows path must be an absolute local drive path")
    raw = os.fspath(value)
    if not raw or raw.startswith(("\\\\", "//")):
        raise ValueError("Windows path must be an absolute local drive path")
    path = PureWindowsPath(raw)
    drive = path.drive
    if not path.is_absolute() or not re.fullmatch(r"[A-Za-z]:", drive):
        raise ValueError("Windows path must be an absolute local drive path")
    parts = [part for part in path.parts[1:] if part not in ("", "\\", "/")]
    if any(part in (".", "..") for part in parts):
        raise ValueError("Windows path must be normalized")
    suffix = "/".join(parts)
    base = f"/mnt/{drive[0].lower()}"
    return f"{base}/{suffix}" if suffix else base


def _config_store_request(
    operation: str,
    windows_home: Path,
    *,
    wsl_hermes_home: str | None = None,
) -> dict[str, Any]:
    if operation not in {"read", "write"}:
        raise ValueError("unsupported WSL Journal configuration operation")
    distro = os.environ.get("MY_JOURNAL_WSL_DISTRO", "Ubuntu").strip()
    if _DISTRO.fullmatch(distro) is None:
        raise ValueError("invalid WSL distribution name")
    root = windows_to_wsl_path(windows_home)
    request: dict[str, Any] = {"operation": operation, "root": root}
    if operation == "write":
        request["wsl_hermes_home"] = _validated_wsl_home(wsl_hermes_home)
    runtime = windows_to_wsl_path(Path(__file__).resolve().with_name("wsl_config_store.py"))
    payload = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > _MAX_RUNTIME_CONFIG_BYTES:
        raise ValueError("WSL Journal runtime configuration request exceeds byte ceiling")
    completed = subprocess.run(
        ["wsl.exe", "-d", distro, "--", "python3", runtime],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
        timeout=60,
    )
    if completed.returncode != 0:
        raise ValueError((completed.stderr.strip() or "WSL Journal configuration helper failed")[:2000])
    if len(completed.stdout.encode("utf-8")) > _MAX_RUNTIME_CONFIG_BYTES:
        raise ValueError("WSL Journal runtime configuration response exceeds byte ceiling")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("WSL Journal configuration helper returned malformed JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("WSL Journal configuration helper returned a nonobject response")
    if result.get("error"):
        raise ValueError(str(result["error"])[:2000])
    return result


def invoke_wsl(
    operation: str,
    args: dict[str, Any],
    *,
    windows_home: Path,
    plugin_root: Path,
    windows_executable: Path,
    windows_python: Path,
) -> dict[str, Any]:
    """Invoke the fixed WSL runtime through JSON standard input without a shell."""
    if not isinstance(operation, str) or _OPERATION.fullmatch(operation) is None:
        raise ValueError("unsupported Journal bridge operation")
    if not isinstance(args, dict):
        raise ValueError("Journal bridge arguments must be an object")
    distro = os.environ.get("MY_JOURNAL_WSL_DISTRO", "Ubuntu").strip()
    if _DISTRO.fullmatch(distro) is None:
        raise ValueError("invalid WSL distribution name")
    windows_home = Path(windows_home)
    plugin_root = Path(plugin_root)
    wsl_hermes_home, wsl_journal_root = _configured_wsl_roots(windows_home)
    payload = {
        "schema_version": 1,
        "operation": operation,
        "args": args,
        "windows_home": str(windows_home),
        "windows_journal_root": str(windows_home / "journal"),
        "wsl_hermes_home": wsl_hermes_home,
        "wsl_journal_root": wsl_journal_root,
        "windows_executable": str(windows_executable),
        "windows_python": str(windows_python),
        "windows_plugin_root": str(plugin_root),
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError("Journal bridge request exceeds its byte ceiling")
    runtime = windows_to_wsl_path(plugin_root / "wsl_runtime.py")
    command = ["wsl.exe", "-d", distro, "--", "python3", runtime]
    completed = subprocess.run(
        command,
        input=encoded,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
        timeout=(
            _SETUP_APPROVAL_TIMEOUT_SECONDS
            if operation == "journal_setup_approve"
            else _TIMEOUT_SECONDS
        ),
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or "WSL Journal runtime failed"
        raise ValueError(message[:2000])
    if len(completed.stdout.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("Journal bridge response exceeds its byte ceiling")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("WSL Journal runtime returned malformed JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("WSL Journal runtime returned a nonobject response")
    return result


def _bridge_paths() -> dict[str, Path]:
    home_value = os.environ.get("HERMES_HOME", "").strip()
    if home_value:
        home = Path(home_value)
    else:
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if not local:
            raise ValueError("native Windows Hermes home is unavailable")
        home = Path(local) / "hermes"
    executable_value = os.environ.get("HERMES_EXECUTABLE", "").strip()
    executable = Path(executable_value or shutil.which("hermes") or "")
    if not str(executable):
        raise ValueError("native Windows Hermes executable is unavailable")
    return {
        "windows_home": home,
        "plugin_root": Path(__file__).resolve().parent,
        "windows_executable": executable,
        "windows_python": Path(sys.executable),
    }


def _forward(
    operation: str,
    args: dict[str, Any],
    *,
    invoker: Callable[..., dict[str, Any]] = invoke_wsl,
) -> str:
    try:
        result = invoker(operation, args, **_bridge_paths())
    except Exception as exc:
        result = {"error": str(exc)[:2000]}
    return json.dumps(result, ensure_ascii=False)


def register_cli(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="journal_command")
    subs.add_parser("status")
    gaps = subs.add_parser("gaps")
    gaps.add_argument("range", nargs="?", default="")
    plan = subs.add_parser("backfill-plan")
    plan.add_argument("range")
    resolve = subs.add_parser("resolve-range")
    resolve.add_argument("range")
    subs.add_parser("setup-inventory")
    cron_setup = subs.add_parser("cron-setup")
    cron_setup.add_argument("--schedule", default="0 11 * * *")
    cron_setup.add_argument("--deliver", default="local")
    cron_setup.add_argument("--wsl-hermes-home")
    cron_remove = subs.add_parser("cron-remove")
    cron_remove.add_argument("--force-job-id")
    cron_remove.add_argument("--confirm", default="")
    parser.set_defaults(func=journal_command)


def journal_command(args: argparse.Namespace) -> int:
    command = getattr(args, "journal_command", None)
    if command == "status":
        operation, payload = "journal_status", {}
    elif command == "gaps":
        operation, payload = "journal_find_gaps", {"range": args.range}
    elif command == "backfill-plan":
        operation, payload = "journal_plan_backfill", {"range": args.range}
    elif command == "resolve-range":
        operation, payload = "journal_resolve_range", {"range": args.range}
    elif command == "setup-inventory":
        operation, payload = "journal_setup_inventory", {}
    elif command == "cron-setup":
        if args.wsl_hermes_home:
            _write_wsl_runtime_config(_bridge_paths()["windows_home"], args.wsl_hermes_home)
        operation, payload = "journal_internal_schedule_create", {
            "schedule": args.schedule, "deliver": args.deliver,
        }
    elif command == "cron-remove":
        operation, payload = "journal_internal_schedule_remove", {
            "force_job_id": args.force_job_id, "confirmation": args.confirm,
        }
    else:
        print("usage: hermes journal COMMAND")
        raise SystemExit(2)
    parsed = json.loads(_forward(operation, payload))
    print(json.dumps(parsed, ensure_ascii=False, indent=2))
    if parsed.get("error") or parsed.get("ok") is False:
        raise SystemExit(1)
    return 0


def register_windows(
    ctx,
    *,
    invoker: Callable[..., dict[str, Any]] = invoke_wsl,
) -> None:
    generation_names = {
        "journal_generation_collect",
        "journal_generation_resume",
        "journal_generation_get_chunk",
        "journal_generation_record_digest",
        "journal_generation_complete",
    }
    for name, schema in TOOL_SCHEMAS.items():
        def handler(args: dict, _name: str = name, **kwargs) -> str:
            return _forward(_name, args, invoker=invoker)

        ctx.register_tool(
            name=name,
            toolset=GENERATION_TOOLSET if name in generation_names else "journal",
            schema=schema,
            handler=handler,
            emoji="📓" if name not in generation_names else "🔒",
        )
    ctx.register_cli_command(
        name="journal",
        help="Inspect and plan the evidence backed My Journal",
        setup_fn=register_cli,
        handler_fn=journal_command,
        description="Status, gaps, deterministic date ranges, and safe backfill planning.",
    )
