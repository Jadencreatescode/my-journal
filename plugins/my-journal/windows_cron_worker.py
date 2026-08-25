"""Native Hermes cron API worker for the Windows to WSL Journal bridge."""

from __future__ import annotations

import json
import importlib
import inspect
import os
import sys
from pathlib import Path
from typing import Any

_MAX_REQUEST_BYTES = 2_000_000
_MAX_RESPONSE_BYTES = 4_000_000


def _load_native_cron_api(home_value: str):
    if not isinstance(home_value, str) or not home_value.strip():
        raise RuntimeError("native Journal cron worker requires HERMES_HOME")
    home_value = home_value.strip()
    os.environ["HERMES_HOME"] = home_value
    core_root = (Path(home_value) / "hermes-agent").resolve(strict=True)
    if not core_root.is_dir():
        raise RuntimeError("native Journal cron worker core root is not a directory")
    for module_name in tuple(sys.modules):
        if module_name == "cron" or module_name.startswith("cron."):
            del sys.modules[module_name]
    sys.path.insert(0, str(core_root))
    importlib.invalidate_caches()
    jobs = importlib.import_module("cron.jobs")
    module_file = Path(getattr(jobs, "__file__", "")).resolve(strict=True)
    if not module_file.is_relative_to(core_root):
        raise RuntimeError("native Journal cron API resolved outside the exact Hermes home")
    required_parameters = {"required_prerun", "required_postrun", "lock_execution"}
    if not required_parameters.issubset(inspect.signature(jobs.create_job).parameters):
        raise RuntimeError("native Hermes cron API does not support the required Journal safety contract")
    return jobs.create_job, jobs.list_jobs, jobs.remove_job


def dispatch(value: object) -> Any:
    if not isinstance(value, dict) or set(value) != {"operation", "windows_home", "args"}:
        raise ValueError("malformed native Journal cron request")
    operation = value.get("operation")
    args = value.get("args")
    if not isinstance(args, dict):
        raise ValueError("malformed native Journal cron arguments")
    windows_home = value.get("windows_home")
    if not isinstance(windows_home, str) or not windows_home.strip():
        raise ValueError("malformed native Journal cron home")
    create_job, list_jobs, remove_job = _load_native_cron_api(windows_home)

    if operation == "create":
        return create_job(**args)
    if operation == "list":
        if set(args) != {"include_disabled"} or not isinstance(args["include_disabled"], bool):
            raise ValueError("malformed native Journal cron list request")
        return list_jobs(include_disabled=args["include_disabled"])
    if operation == "remove":
        if set(args) != {"job_id"} or not isinstance(args["job_id"], str) or not args["job_id"]:
            raise ValueError("malformed native Journal cron remove request")
        return remove_job(args["job_id"])
    raise ValueError("unsupported native Journal cron operation")


def main() -> int:
    raw = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
    if len(raw) > _MAX_REQUEST_BYTES:
        raise ValueError("native Journal cron request exceeds its byte ceiling")
    request = json.loads(raw)
    encoded = json.dumps(dispatch(request), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_RESPONSE_BYTES:
        raise ValueError("native Journal cron response exceeds its byte ceiling")
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
