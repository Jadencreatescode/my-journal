from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def load_runtime():
    name = f"journal_wsl_runtime_test_{os.getpid()}_{time.time_ns()}"
    spec = importlib.util.spec_from_file_location(name, ROOT / "wsl_runtime.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def request(operation: str = "journal_status", args: dict | None = None) -> dict:
    return {
        "schema_version": 1,
        "operation": operation,
        "args": {} if args is None else args,
        "windows_home": r"C:\Users\jgib7\AppData\Local\hermes",
        "windows_journal_root": r"C:\Users\jgib7\AppData\Local\hermes\journal",
        "windows_executable": r"C:\Users\jgib7\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe",
        "windows_python": r"C:\Users\jgib7\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe",
        "windows_plugin_root": r"C:\Users\jgib7\AppData\Local\hermes\plugins\my-journal",
    }


class WslRuntimeTests(unittest.TestCase):
    def test_dispatch_sets_confined_paths_and_calls_only_the_named_tool_handler(self):
        runtime = load_runtime()
        calls: list[dict] = []
        fake = SimpleNamespace(
            tools=SimpleNamespace(
                handle_status=lambda args: calls.append(args) or json.dumps({"ok": True, "entry_count": 1})
            )
        )

        result = runtime.dispatch(request(), plugin=fake)

        self.assertEqual(result, {"ok": True, "entry_count": 1})
        self.assertEqual(calls, [{}])
        self.assertEqual(os.environ["HERMES_HOME"], "/mnt/c/Users/jgib7/AppData/Local/hermes")
        self.assertEqual(os.environ["MY_JOURNAL_ROOT"], "/mnt/c/Users/jgib7/AppData/Local/hermes/journal")
        self.assertEqual(
            os.environ["HERMES_EXECUTABLE"],
            "/mnt/c/Users/jgib7/AppData/Local/hermes/hermes-agent/venv/Scripts/hermes.exe",
        )
        self.assertEqual(os.environ["MY_JOURNAL_WINDOWS_HERMES_HOME"], request()["windows_home"])

    def test_dispatch_rejects_unknown_operations_and_request_fields_before_loading(self):
        runtime = load_runtime()
        malformed = request("journal_delete_everything")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            runtime.dispatch(malformed, plugin=SimpleNamespace())
        extra = request()
        extra["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "malformed"):
            runtime.dispatch(extra, plugin=SimpleNamespace())

    def test_dispatch_rejects_nonobject_or_malformed_handler_output(self):
        runtime = load_runtime()
        for output in ("not json", '["wrong"]'):
            fake = SimpleNamespace(tools=SimpleNamespace(handle_status=lambda args, value=output: value))
            with self.subTest(output=output), self.assertRaises(ValueError):
                runtime.dispatch(request(), plugin=fake)


if __name__ == "__main__":
    unittest.main()
