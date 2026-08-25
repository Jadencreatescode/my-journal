from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_worker():
    spec = importlib.util.spec_from_file_location("journal_windows_cron_worker_test", ROOT / "windows_cron_worker.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_operations():
    package_name = "journal_windows_cron_operations_test"
    spec = importlib.util.spec_from_file_location(
        package_name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return sys.modules[f"{package_name}.operations"]


class WindowsCronWorkerTests(unittest.TestCase):
    def test_native_worker_anchors_cron_import_to_exact_hermes_home(self):
        worker = load_worker()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            package = home / "hermes-agent" / "cron"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "jobs.py").write_text(
                "def create_job(prompt, schedule, required_prerun=False, required_postrun=False, lock_execution=False, **kwargs): return {'id': 'anchored', 'prompt': prompt, 'schedule': schedule, **kwargs}\n"
                "def list_jobs(include_disabled=False): return []\n"
                "def remove_job(job_id): return True\n",
                encoding="utf-8",
            )
            old_modules = {key: sys.modules.pop(key) for key in ("cron.jobs", "cron") if key in sys.modules}
            try:
                with mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}, clear=False):
                    result = worker.dispatch({"operation": "create", "windows_home": str(home), "args": {"prompt": "p", "schedule": "0 11 * * *"}})
                self.assertEqual(result["id"], "anchored")
                self.assertTrue(Path(sys.modules["cron.jobs"].__file__).resolve().is_relative_to((home / "hermes-agent").resolve()))
            finally:
                sys.modules.pop("cron.jobs", None)
                sys.modules.pop("cron", None)
                sys.modules.update(old_modules)

    def test_dispatch_uses_native_cron_api_for_complete_lifecycle(self):
        worker = load_worker()
        calls: list[tuple] = []
        create_job = lambda **kwargs: calls.append(("create", kwargs)) or {"id": "job-1", **kwargs}
        list_jobs = lambda include_disabled=False: calls.append(("list", include_disabled)) or [{"id": "job-1"}]
        remove_job = lambda job_id: calls.append(("remove", job_id)) or True
        with mock.patch.object(worker, "_load_native_cron_api", return_value=(create_job, list_jobs, remove_job)):
            created = worker.dispatch({"operation": "create", "windows_home": "C:\\Hermes", "args": {"prompt": "p", "schedule": "0 11 * * *"}})
            listed = worker.dispatch({"operation": "list", "windows_home": "C:\\Hermes", "args": {"include_disabled": True}})
            removed = worker.dispatch({"operation": "remove", "windows_home": "C:\\Hermes", "args": {"job_id": "job-1"}})
        self.assertEqual(created["id"], "job-1")
        self.assertEqual(listed, [{"id": "job-1"}])
        self.assertTrue(removed)
        self.assertEqual(calls, [
            ("create", {"prompt": "p", "schedule": "0 11 * * *"}),
            ("list", True),
            ("remove", "job-1"),
        ])

    @unittest.skipIf(sys.platform == "win32", "Linux ownership code runs inside WSL")
    def test_linux_ownership_code_calls_fixed_native_worker_when_windows_bridge_is_active(self):
        operations = load_operations()
        completed = mock.Mock(returncode=0, stdout='{"id":"job-1"}', stderr="")
        environment = {
            "MY_JOURNAL_WINDOWS_PYTHON": r"C:\Hermes\python.exe",
            "MY_JOURNAL_WINDOWS_PLUGIN_ROOT": r"C:\Hermes\plugins\my-journal",
            "MY_JOURNAL_WINDOWS_HERMES_HOME": r"C:\Hermes",
        }
        with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
            operations.subprocess, "run", return_value=completed
        ) as run:
            result = operations._create_cron_job(prompt="p", schedule="0 11 * * *")
        self.assertEqual(result, {"id": "job-1"})
        self.assertEqual(
            run.call_args.args[0],
            [r"C:\Hermes\python.exe", r"C:\Hermes\plugins\my-journal\windows_cron_worker.py"],
        )
        self.assertEqual(
            json.loads(run.call_args.kwargs["input"]),
            {"operation": "create", "windows_home": r"C:\Hermes", "args": {"prompt": "p", "schedule": "0 11 * * *"}},
        )
        self.assertEqual(run.call_args.kwargs["env"]["HERMES_HOME"], r"C:\Hermes")
        self.assertNotIn("shell", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
