from __future__ import annotations

import importlib.util
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_runner():
    path = ROOT / "scripts" / "my-journal-daily" / "precollect.py"
    spec = importlib.util.spec_from_file_location("my_journal_daily_precollect", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_postvalidator():
    path = ROOT / "scripts" / "my-journal-daily" / "postvalidate.py"
    spec = importlib.util.spec_from_file_location("my_journal_daily_postvalidate", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DailyPrecollectorTests(unittest.TestCase):
    def test_success_output_is_bounded_json_and_contains_no_collected_content(self):
        runner = load_runner()
        result = {
            "ok": True,
            "journal_date": "2026-07-27",
            "binding_id": "b" * 64,
            "run_id": "a" * 16,
            "receipt_sha256": "1" * 64,
            "manifest_sha256": "2" * 64,
            "packet_plan_sha256": "3" * 64,
            "output": "private model output must not be delivered",
        }
        stream = io.StringIO()
        with mock.patch.object(
            runner, "_run_daily", return_value=result
        ), redirect_stdout(stream):
            exit_code = runner.main()

        self.assertEqual(exit_code, 0)
        output = json.loads(stream.getvalue())
        self.assertEqual(output["journal_date"], "2026-07-27")
        self.assertTrue(output["wakeAgent"])
        self.assertEqual(output["binding_id"], "b" * 64)
        self.assertNotIn("output", output)
        self.assertNotIn("private", stream.getvalue())

    def test_failure_is_nonzero_and_error_is_bounded(self):
        runner = load_runner()
        stream = io.StringIO()
        with mock.patch.object(
            runner,
            "_run_daily",
            return_value={
                "ok": False,
                "journal_date": "2026-07-27",
                "error": "x" * 5000,
            },
        ), redirect_stdout(stream):
            exit_code = runner.main()

        self.assertEqual(exit_code, 1)
        output = json.loads(stream.getvalue())
        self.assertFalse(output["ok"])
        self.assertIn("x", output["error"])
        self.assertLessEqual(len(stream.getvalue()), runner.MAX_OUTPUT_CHARS + 1)


class DailyPostvalidatorTests(unittest.TestCase):
    def test_success_is_exact_bounded_metadata(self):
        runner = load_postvalidator()
        stream = io.StringIO()
        with mock.patch.object(runner, "_run_daily", return_value={
            "ok": True,
            "binding_id": "b" * 64,
            "run_id": "a" * 16,
            "journal_date": "2026-07-27",
            "validated": True,
            "private": "must not leak",
        }), redirect_stdout(stream):
            exit_code = runner.main()
        self.assertEqual(exit_code, 0)
        output = json.loads(stream.getvalue())
        self.assertEqual(set(output), {"ok", "binding_id", "run_id", "journal_date", "validated"})

    def test_failure_is_nonzero_and_bounded(self):
        runner = load_postvalidator()
        stream = io.StringIO()
        with mock.patch.object(runner, "_run_daily", return_value={
            "ok": False, "journal_date": "2026-07-27", "error": "x" * 5000,
        }), redirect_stdout(stream):
            exit_code = runner.main()
        self.assertEqual(exit_code, 1)
        self.assertLessEqual(len(stream.getvalue()), runner.MAX_OUTPUT_CHARS + 1)


if __name__ == "__main__":
    unittest.main()