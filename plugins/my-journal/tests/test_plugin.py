from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "journal_plugin",
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(self):
        self.tools = {}
        self.cli = None
        self.slash = []

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_cli_command(self, **kwargs):
        self.cli = kwargs

    def register_command(self, *args, **kwargs):
        self.slash.append((args, kwargs))


class PluginRegistrationTests(unittest.TestCase):
    def test_generation_exit_zero_without_validated_note_fails_closed(self):
        plugin = load_plugin()
        operations = sys.modules[f"{plugin.__name__}.operations"]
        completed = type(
            "Completed", (), {"returncode": 0, "stdout": "claimed success", "stderr": ""}
        )()
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("MY_JOURNAL_ROOT")
            os.environ["MY_JOURNAL_ROOT"] = tmp
            try:
                with mock.patch.object(
                    operations, "_hermes_executable", return_value="/hermes"
                ), mock.patch.object(
                    operations.subprocess, "run", return_value=completed
                ), mock.patch.object(operations, "discover_entries", return_value=[]):
                    result = operations.run_generation(
                        "Generate journal date 2026-07-27.",
                        expected_dates=["2026-07-27"],
                    )
            finally:
                if previous is None:
                    os.environ.pop("MY_JOURNAL_ROOT", None)
                else:
                    os.environ["MY_JOURNAL_ROOT"] = previous

            self.assertFalse(result["ok"])
            self.assertEqual(result["missing_dates"], ["2026-07-27"])
            ledgers = list(Path(tmp).glob("generation-*.json"))
            self.assertEqual(len(ledgers), 1)
            self.assertEqual(json.loads(ledgers[0].read_text())["status"], "failed")
            self.assertEqual(list(Path(tmp).glob("generation-*.lock")), [])

    def test_cli_registers_generation_backfill_cron_and_maintenance_commands(self):
        plugin = load_plugin()
        ctx = FakeContext()
        plugin.register(ctx)
        parser = argparse.ArgumentParser()
        cli = ctx.cli
        assert cli is not None
        cli["setup_fn"](parser)

        self.assertEqual(parser.parse_args(["generate", "2026-07-27"]).journal_command, "generate")
        self.assertEqual(parser.parse_args(["backfill", "last 7 days"]).journal_command, "backfill")
        self.assertEqual(parser.parse_args(["preview", "last 7 days"]).journal_command, "preview")
        self.assertEqual(parser.parse_args(["cron-setup"]).journal_command, "cron-setup")
        self.assertEqual(parser.parse_args(["cron-remove"]).journal_command, "cron-remove")
        self.assertEqual(parser.parse_args(["maintenance"]).journal_command, "maintenance")
        self.assertEqual(
            parser.parse_args(["purge", "--confirm", "DELETE MY JOURNAL DATA"]).journal_command,
            "purge",
        )

    def test_purge_requires_confirmation_and_preserves_config(self):
        plugin = load_plugin()
        operations = sys.modules[f"{plugin.__name__}.operations"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config.json").write_text("{}", encoding="utf-8")
            for name in operations.PURGE_DIRECTORIES:
                directory = root / name
                directory.mkdir()
                (directory / "data.txt").write_text("data", encoding="utf-8")
            previous = os.environ.get("MY_JOURNAL_ROOT")
            os.environ["MY_JOURNAL_ROOT"] = str(root)
            try:
                preview_result = operations.purge()
                self.assertTrue(preview_result["preview"])
                self.assertEqual(set(preview_result["candidates"]), set(operations.PURGE_DIRECTORIES))
                self.assertTrue(all((root / name).exists() for name in operations.PURGE_DIRECTORIES))
                with self.assertRaisesRegex(ValueError, "exact confirmation"):
                    operations.purge("wrong", apply=True)
                result = operations.purge("DELETE MY JOURNAL DATA", apply=True)
            finally:
                if previous is None:
                    os.environ.pop("MY_JOURNAL_ROOT", None)
                else:
                    os.environ["MY_JOURNAL_ROOT"] = previous

            self.assertTrue(result["config_preserved"])
            self.assertTrue((root / "config.json").is_file())
            for name in operations.PURGE_DIRECTORIES:
                self.assertFalse((root / name).exists())

    def test_cron_setup_uses_native_hermes_cron_and_persists_exact_job_id(self):
        plugin = load_plugin()
        operations = sys.modules[f"{plugin.__name__}.operations"]
        created = type(
            "Completed", (),
            {"returncode": 0, "stdout": "Created job: job_abc123\n", "stderr": ""},
        )()
        listed = type(
            "Completed", (),
            {"returncode": 0, "stdout": "job_abc123  my-journal-daily\n", "stderr": ""},
        )()
        removed = type(
            "Completed", (), {"returncode": 0, "stdout": "removed", "stderr": ""}
        )()
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("MY_JOURNAL_ROOT")
            os.environ["MY_JOURNAL_ROOT"] = tmp
            try:
                with mock.patch.object(
                    operations, "_hermes_executable", return_value="/hermes"
                ), mock.patch.object(
                    operations.subprocess,
                    "run",
                    side_effect=[created, listed, removed],
                ) as run:
                    result = operations.schedule_create("0 11 * * *", "local")
                    repeated = operations.schedule_create("0 11 * * *", "local")
                    removal = operations.schedule_remove()
            finally:
                if previous is None:
                    os.environ.pop("MY_JOURNAL_ROOT", None)
                else:
                    os.environ["MY_JOURNAL_ROOT"] = previous

            self.assertEqual(result["job_id"], "job_abc123")
            self.assertTrue(repeated["existing"])
            self.assertEqual(removal["job_id"], "job_abc123")
            self.assertEqual(run.call_args_list[2].args[0], ["/hermes", "cron", "remove", "job_abc123"])
            self.assertFalse((Path(tmp) / "cron-job.json").exists())

    def test_registers_tools_and_cli_but_does_not_shadow_journal_skill(self):
        plugin = load_plugin()
        ctx = FakeContext()
        plugin.register(ctx)
        self.assertEqual(
            set(ctx.tools),
            {
                "journal_status",
                "journal_resolve_range",
                "journal_read_entries",
                "journal_find_gaps",
                "journal_plan_backfill",
            },
        )
        self.assertEqual(ctx.cli["name"], "journal")
        self.assertEqual(ctx.slash, [])

    def test_range_tool_honors_configured_timezone(self):
        plugin = load_plugin()
        ctx = FakeContext()
        previous = os.environ.get("MY_JOURNAL_TIMEZONE")
        os.environ["MY_JOURNAL_TIMEZONE"] = "Definitely/Not_A_Timezone"
        try:
            plugin.register(ctx)
            result = json.loads(
                ctx.tools["journal_resolve_range"]["handler"]({"range": "today"})
            )
        finally:
            if previous is None:
                os.environ.pop("MY_JOURNAL_TIMEZONE", None)
            else:
                os.environ["MY_JOURNAL_TIMEZONE"] = previous
        self.assertIn("error", result)
        self.assertIn("time zone", result["error"].lower())

    def test_timezone_rejects_symlinked_config(self):
        plugin = load_plugin()
        tools = sys.modules[f"{plugin.__name__}.tools"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "journal"
            root.mkdir()
            external = Path(tmp) / "external.json"
            external.write_text('{"timezone":"UTC"}', encoding="utf-8")
            (root / "config.json").symlink_to(external)
            previous_root = os.environ.get("MY_JOURNAL_ROOT")
            previous_timezone = os.environ.pop("MY_JOURNAL_TIMEZONE", None)
            os.environ["MY_JOURNAL_ROOT"] = str(root)
            try:
                with self.assertRaisesRegex(ValueError, "symlink"):
                    tools.journal_timezone()
            finally:
                if previous_root is None:
                    os.environ.pop("MY_JOURNAL_ROOT", None)
                else:
                    os.environ["MY_JOURNAL_ROOT"] = previous_root
                if previous_timezone is not None:
                    os.environ["MY_JOURNAL_TIMEZONE"] = previous_timezone

    def test_status_tool_returns_json(self):
        plugin = load_plugin()
        ctx = FakeContext()
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("MY_JOURNAL_ROOT")
            os.environ["MY_JOURNAL_ROOT"] = tmp
            try:
                plugin.register(ctx)
                result = json.loads(ctx.tools["journal_status"]["handler"]({}))
            finally:
                if previous is None:
                    os.environ.pop("MY_JOURNAL_ROOT", None)
                else:
                    os.environ["MY_JOURNAL_ROOT"] = previous
            self.assertEqual(result["entry_count"], 0)
            self.assertEqual(result["journal_root"], tmp)

    def test_cli_status_prints_machine_readable_json(self):
        plugin = load_plugin()
        ctx = FakeContext()
        plugin.register(ctx)
        parser = argparse.ArgumentParser()
        ctx.cli["setup_fn"](parser)
        args = parser.parse_args(["status"])
        self.assertEqual(args.journal_command, "status")

    def test_cli_handler_raises_nonzero_system_exit_for_tool_error(self):
        plugin = load_plugin()
        ctx = FakeContext()
        plugin.register(ctx)
        parser = argparse.ArgumentParser()
        ctx.cli["setup_fn"](parser)
        args = parser.parse_args(["resolve-range", "last 0 days"])
        with self.assertRaisesRegex(SystemExit, "1"):
            ctx.cli["handler_fn"](args)


if __name__ == "__main__":
    unittest.main()
