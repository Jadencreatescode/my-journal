from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "journal_config.py"
COLLECTOR = Path(__file__).parents[1] / "scripts" / "collect_journal.py"


def load_module():
    spec = importlib.util.spec_from_file_location("journal_config", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_collector():
    spec = importlib.util.spec_from_file_location("collect_journal", COLLECTOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def config_payload() -> dict:
    return {
        "schema_version": 1,
        "enabled": True,
        "timezone": "America/Los_Angeles",
        "profiles": ["default"],
        "platforms": ["discord"],
        "excluded_session_ids": [],
        "database_size_approvals": {},
        "privacy": {
            "redact_secrets": True,
            "pii_mode": "mask",
            "entropy_mode": "report",
        },
        "limits": {
            "max_message_chars": 4000,
            "max_tool_chars": 1200,
            "max_selected_messages": 25000,
            "max_retained_chars": 4000000,
            "max_sessions": 2000,
            "packet_chunk_bytes": 120000,
            "max_packet_chunks": 64,
        },
    }


class JournalConfigTests(unittest.TestCase):
    def test_database_size_approvals_accept_only_profile_bound_compiled_tiers(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = config_payload()
            payload["database_size_approvals"] = {"default": 16 * 1024**3}
            config = module.load_config(self.write_config(root, payload))
            self.assertEqual(dict(config.database_size_approvals), {"default": 16 * 1024**3})

            for approvals in (
                {"default": 64 * 1024**3},
                {"unapproved-profile": 16 * 1024**3},
                {"default": True},
            ):
                payload = config_payload()
                payload["database_size_approvals"] = approvals
                with self.subTest(approvals=approvals):
                    with self.assertRaisesRegex(ValueError, "database_size_approvals"):
                        module.load_config(self.write_config(root, payload))

    def test_unimplemented_retention_policy_fails_closed(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = config_payload()
            config["retention"] = {"days": 30}
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported fields.*retention"):
                module.load_config(path)

    def test_daily_message_limit_cannot_exceed_absolute_compiled_tier(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            payload = config_payload()
            payload["limits"]["max_selected_messages"] = 100_001
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "compiled ceiling"):
                module.load_config(path)

    def test_daily_message_limit_accepts_only_compiled_tiers(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for value in (25_001, 75_000, 99_999):
                payload = config_payload()
                payload["limits"]["max_selected_messages"] = value
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "compiled tier"):
                    module.load_config(path)

    def test_higher_capacity_configuration_requires_matching_durable_receipts(self):
        module = load_module()
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "journal"
            output.mkdir()
            payload = config_payload()
            payload["database_size_approvals"] = {"default": 16 * 1024**3}
            payload["limits"]["max_selected_messages"] = 50_000
            path = output / "config.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            config = module.load_config(path)
            with self.assertRaisesRegex(ValueError, "approval receipt"):
                collector.validate_capacity_receipts(output, config)

            database_phrase = collector._database_confirmation("default", 16 * 1024**3)
            (output / "database-size-approvals.json").write_text(json.dumps({
                "schema_version": 1,
                "approvals": {
                    "default": {
                        "approved_max_bytes": 16 * 1024**3,
                        "confirmation_sha256": hashlib.sha256(database_phrase.encode()).hexdigest(),
                    }
                },
            }), encoding="utf-8")
            journal_date = "2026-07-27"
            daily_phrase = collector._daily_confirmation(journal_date, 50_000)
            (output / "daily-workload-approval.json").write_text(json.dumps({
                "schema_version": 1,
                "approval_kind": "targeted_tier",
                "journal_date": journal_date,
                "approved_tier": 50_000,
                "eligible_message_count": 26_795,
                "configuration_sha256": config.daily_authorization_sha256(),
                "confirmation_sha256": hashlib.sha256(daily_phrase.encode()).hexdigest(),
            }), encoding="utf-8")
            collector.validate_capacity_receipts(output, config)

            receipt = json.loads((output / "daily-workload-approval.json").read_text(encoding="utf-8"))
            receipt["confirmation_sha256"] = "0" * 64
            (output / "daily-workload-approval.json").write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "malformed"):
                collector.validate_capacity_receipts(output, config)

            plan_id = "a" * 32
            plan = {
                "plan_id": plan_id,
                "confirmation_phrase": "",
                "completed_dates": [],
                "config_activated": True,
                "limits": {"max_selected_messages": 50_000},
            }
            plan["confirmation_phrase"] = collector._plan_confirmation(plan)
            plans = output / "approval-plans"
            plans.mkdir()
            (plans / f"{plan_id}.json").write_text(json.dumps(plan), encoding="utf-8")
            (output / "daily-workload-approval.json").write_text(json.dumps({
                "schema_version": 1,
                "approval_kind": "guided_setup",
                "plan_id": plan_id,
                "approved_tier": 50_000,
                "configuration_sha256": config.daily_authorization_sha256(),
                "confirmation_sha256": hashlib.sha256(
                    plan["confirmation_phrase"].encode()
                ).hexdigest(),
            }), encoding="utf-8")
            collector.validate_capacity_receipts(output, config)

            changed_scope = json.loads(json.dumps(payload))
            changed_scope["platforms"] = ["cli", "discord"]
            path.write_text(json.dumps(changed_scope), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "scope and policy"):
                collector.validate_capacity_receipts(output, module.load_config(path))

            changed_database = json.loads(json.dumps(payload))
            changed_database["database_size_approvals"]["default"] = 32 * 1024**3
            database_phrase = collector._database_confirmation("default", 32 * 1024**3)
            (output / "database-size-approvals.json").write_text(json.dumps({
                "schema_version": 1,
                "approvals": {
                    "default": {
                        "approved_max_bytes": 32 * 1024**3,
                        "confirmation_sha256": hashlib.sha256(database_phrase.encode()).hexdigest(),
                    }
                },
            }), encoding="utf-8")
            path.write_text(json.dumps(changed_database), encoding="utf-8")
            collector.validate_capacity_receipts(output, module.load_config(path))

    def test_production_collector_rejects_missing_capacity_evidence_before_sqlite_open(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            output = root / "journal"
            output.mkdir()
            for kind in ("database", "daily"):
                payload = config_payload()
                if kind == "database":
                    payload["database_size_approvals"] = {"default": 16 * 1024**3}
                else:
                    payload["limits"]["max_selected_messages"] = 50_000
                config_path = output / "config.json"
                config_path.write_text(json.dumps(payload), encoding="utf-8")
                stream = io.StringIO()
                with self.subTest(kind=kind), mock.patch.object(
                    collector.sqlite3, "connect", side_effect=AssertionError("SQLite opened")
                ) as connect, redirect_stdout(stream):
                    code = collector.main([
                        "--home", str(home), "--output", str(output),
                        "--config", str(config_path), "--date", "2026-07-27",
                    ])
                self.assertEqual(code, 1)
                self.assertIn("approval receipt", stream.getvalue())
                connect.assert_not_called()

    def write_config(self, root: Path, payload: dict) -> Path:
        path = root / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_disabled_config_requires_explicit_consent(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            payload = config_payload()
            payload["enabled"] = False
            payload["profiles"] = []
            payload["platforms"] = []
            config = module.load_config(self.write_config(Path(tmp), payload))

            with self.assertRaisesRegex(ValueError, "explicit consent"):
                config.require_enabled()

    def test_enabled_config_requires_explicit_profile_and_platform_allowlists(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            for field in ("profiles", "platforms"):
                payload = config_payload()
                payload[field] = []
                with self.subTest(field=field):
                    with self.assertRaisesRegex(ValueError, field[:-1]):
                        module.load_config(self.write_config(Path(tmp), payload))

    def test_secret_redaction_cannot_be_disabled(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            payload = config_payload()
            payload["privacy"]["redact_secrets"] = False

            with self.assertRaisesRegex(ValueError, "mandatory"):
                module.load_config(self.write_config(Path(tmp), payload))

    def test_manifest_policy_records_scope_without_excluded_identifiers(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            payload = config_payload()
            payload["excluded_session_ids"] = ["private-session"]
            config = module.load_config(self.write_config(Path(tmp), payload))
            policy = config.manifest_policy()

            self.assertEqual(policy["profiles"], ["default"])
            self.assertEqual(policy["platforms"], ["discord"])
            self.assertEqual(policy["excluded_session_count"], 1)
            self.assertNotIn("private-session", json.dumps(policy))

    def test_collector_cli_refuses_disabled_config_without_artifacts(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            output = root / "journal"
            payload = config_payload()
            payload["enabled"] = False
            payload["profiles"] = []
            payload["platforms"] = []
            config_path = self.write_config(root, payload)

            stream = io.StringIO()
            with redirect_stdout(stream):
                code = collector.main([
                    "--home", str(home),
                    "--output", str(output),
                    "--config", str(config_path),
                    "--date", "1970-01-01",
                ])

            self.assertEqual(code, 1)
            self.assertFalse(output.exists())
            self.assertIn("explicit consent", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
