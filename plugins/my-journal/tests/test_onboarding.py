from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    name = "journal_onboarding_test_plugin"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def create_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as con:
        con.execute("create table sessions (id text primary key, source text not null, started_at real)")
        con.execute("create table messages (id integer primary key, session_id text, role text, content text, timestamp real)")
        con.commit()


def add_message(path: Path, session: str, platform: str, role: str | None, timestamp: float, message_id: int = 1) -> None:
    with closing(sqlite3.connect(path)) as con:
        con.execute("insert or ignore into sessions values (?, ?, ?)", (session, platform, timestamp))
        con.execute(
            "insert into messages values (?, ?, ?, ?, ?)",
            (message_id, session, role, "SECRET BODY MUST NOT BE READ", timestamp),
        )
        con.commit()


class GuidedFirstUseTests(unittest.TestCase):
    def test_large_database_inventory_requires_exact_tier_approval_before_sqlite_open(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "journal"
            database = home / "state.db"
            create_db(database)
            with database.open("r+b") as stream:
                stream.truncate(8 * 1024**3 + 4096)

            with mock.patch.object(onboarding.sqlite3, "connect") as connect:
                inventory = onboarding.discover_setup_inventory(home=home, journal_root=root)
            connect.assert_not_called()
            blocked = inventory["blocked_databases"][0]
            self.assertEqual(blocked["profile"], "default")
            self.assertEqual(blocked["required_tier_gib"], 16)
            self.assertIn("confirmation_phrase", blocked)
            self.assertFalse((root / "database-size-approvals.json").exists())

            with self.assertRaisesRegex(ValueError, "exact confirmation"):
                onboarding.approve_database_size(
                    home=home, journal_root=root, profile="default", confirmation="yes"
                )
            self.assertFalse((root / "database-size-approvals.json").exists())

            approved = onboarding.approve_database_size(
                home=home, journal_root=root, profile="default",
                confirmation=blocked["confirmation_phrase"],
            )
            self.assertEqual(approved["approved_tier_gib"], 16)
            receipt = onboarding.load_database_size_approvals(root)
            self.assertEqual(receipt, {"default": 16 * 1024**3})

    def test_database_growth_requires_next_tier_and_hard_max_never_offers_approval(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "journal"
            database = home / "state.db"
            create_db(database)
            with database.open("r+b") as stream:
                stream.truncate(8 * 1024**3 + 4096)
            first = onboarding.discover_setup_inventory(home=home, journal_root=root)
            onboarding.approve_database_size(
                home=home, journal_root=root, profile="default",
                confirmation=first["blocked_databases"][0]["confirmation_phrase"],
            )
            with database.open("r+b") as stream:
                stream.truncate(16 * 1024**3 + 4096)
            second = onboarding.discover_setup_inventory(home=home, journal_root=root)
            self.assertEqual(second["blocked_databases"][0]["required_tier_gib"], 32)

            with database.open("r+b") as stream:
                stream.truncate(32 * 1024**3 + 4096)
            final = onboarding.discover_setup_inventory(home=home, journal_root=root)
            blocked = final["blocked_databases"][0]
            self.assertEqual(blocked["reason"], "absolute_database_limit_exceeded")
            self.assertNotIn("confirmation_phrase", blocked)

    def test_database_tier_approval_updates_only_matching_enabled_profile_limit(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "journal"
            root.mkdir()
            database = home / "state.db"
            create_db(database)
            with database.open("r+b") as stream:
                stream.truncate(8 * 1024**3 + 4096)
            config = {
                "schema_version": 1, "enabled": True, "timezone": "UTC",
                "profiles": ["default"], "platforms": ["cli"],
                "excluded_session_ids": [], "database_size_approvals": {},
                "privacy": {"redact_secrets": True, "pii_mode": "mask", "entropy_mode": "report"},
                "limits": dict(onboarding.GUIDED_LIMITS),
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            inventory = onboarding.discover_setup_inventory(home=home, journal_root=root)
            blocked = inventory["blocked_databases"][0]
            before = json.loads(config_path.read_text(encoding="utf-8"))
            approved = onboarding.approve_database_size(
                home=home, journal_root=root, profile="default",
                confirmation=blocked["confirmation_phrase"],
            )
            after = json.loads(config_path.read_text(encoding="utf-8"))
            expected = json.loads(json.dumps(before))
            expected["database_size_approvals"] = {"default": 16 * 1024**3}
            self.assertEqual(after, expected)
            self.assertTrue(approved["config_updated"])

    def test_database_tier_retry_recovers_receipt_first_configuration_failure(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "journal"
            root.mkdir()
            database = home / "state.db"
            create_db(database)
            with database.open("r+b") as stream:
                stream.truncate(8 * 1024**3 + 4096)
            config = {
                "schema_version": 1, "enabled": True, "timezone": "UTC",
                "profiles": ["default"], "platforms": ["cli"],
                "excluded_session_ids": [], "database_size_approvals": {},
                "privacy": {"redact_secrets": True, "pii_mode": "mask", "entropy_mode": "report"},
                "limits": dict(onboarding.GUIDED_LIMITS),
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            blocked = onboarding.discover_setup_inventory(
                home=home, journal_root=root,
            )["blocked_databases"][0]
            real_write = onboarding._safe_files.safe_atomic_write_text
            failed = False

            def fail_first_config(write_root, target, text):
                nonlocal failed
                if target.name == "config.json" and not failed:
                    failed = True
                    raise OSError("simulated config write failure")
                return real_write(write_root, target, text)

            with mock.patch.object(
                onboarding._safe_files, "safe_atomic_write_text", side_effect=fail_first_config,
            ), self.assertRaisesRegex(OSError, "simulated"):
                onboarding.approve_database_size(
                    home=home, journal_root=root, profile="default",
                    confirmation=blocked["confirmation_phrase"],
                )
            self.assertEqual(onboarding.load_database_size_approvals(root)["default"], 16 * 1024**3)
            recovered = onboarding.approve_database_size(
                home=home, journal_root=root, profile="default",
                confirmation=blocked["confirmation_phrase"],
            )
            updated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["database_size_approvals"]["default"], 16 * 1024**3)
            self.assertTrue(recovered["config_updated"])

    def test_daily_preflight_requires_database_receipt_before_sqlite_and_exact_reapproval_restores_it(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "journal"
            root.mkdir()
            database = home / "state.db"
            create_db(database)
            original_size = database.stat().st_size
            with database.open("r+b") as stream:
                stream.truncate(8 * 1024**3 + 4096)
            config = {
                "schema_version": 1, "enabled": True, "timezone": "UTC",
                "profiles": ["default"], "platforms": ["cli"],
                "excluded_session_ids": [],
                "database_size_approvals": {"default": 16 * 1024**3},
                "privacy": {"redact_secrets": True, "pii_mode": "mask", "entropy_mode": "report"},
                "limits": dict(onboarding.GUIDED_LIMITS),
            }
            config_path = root / "config.json"
            before = json.dumps(config, indent=2, sort_keys=True) + "\n"
            config_path.write_text(before, encoding="utf-8")
            with mock.patch.object(
                onboarding.sqlite3, "connect", side_effect=AssertionError("SQLite opened")
            ) as connect:
                blocked = onboarding.inspect_daily_workload_capacity(
                    home=home, journal_root=root, journal_date="2026-07-27",
                )
            connect.assert_not_called()
            self.assertEqual(blocked["reason"], "database_size_approval_required")
            self.assertTrue(blocked["evidence_missing"])
            self.assertEqual(blocked["required_tier_gib"], 16)
            restored = onboarding.approve_database_size(
                home=home, journal_root=root, profile="default",
                confirmation=blocked["confirmation_phrase"],
            )
            self.assertFalse(restored["config_updated"])
            self.assertEqual(config_path.read_text(encoding="utf-8"), before)
            self.assertEqual(
                onboarding.load_database_size_approvals(root), {"default": 16 * 1024**3}
            )
            ready = onboarding.inspect_daily_workload_capacity(
                home=home, journal_root=root, journal_date="2026-07-27",
            )
            self.assertTrue(ready["ok"])
            (root / "database-size-approvals.json").unlink()
            with database.open("r+b") as stream:
                stream.truncate(original_size)
            shrunk = onboarding.inspect_daily_workload_capacity(
                home=home, journal_root=root, journal_date="2026-07-27",
            )
            self.assertEqual(shrunk["reason"], "database_size_approval_required")
            restored_small = onboarding.approve_database_size(
                home=home, journal_root=root, profile="default",
                confirmation=shrunk["confirmation_phrase"],
            )
            self.assertFalse(restored_small["config_updated"])
            self.assertEqual(restored_small["approved_tier_gib"], 16)
            receipt_path = root / "database-size-approvals.json"
            tampered = json.loads(receipt_path.read_text(encoding="utf-8"))
            tampered["approvals"]["default"]["confirmation_sha256"] = "0" * 64
            receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
            with mock.patch.object(
                onboarding.sqlite3, "connect", side_effect=AssertionError("SQLite opened")
            ) as connect:
                malformed = onboarding.inspect_daily_workload_capacity(
                    home=home, journal_root=root, journal_date="2026-07-27",
                )
            connect.assert_not_called()
            repaired = onboarding.approve_database_size(
                home=home, journal_root=root, profile="default",
                confirmation=malformed["confirmation_phrase"],
            )
            self.assertFalse(repaired["config_updated"])
            self.assertEqual(
                onboarding.load_database_size_approvals(root), {"default": 16 * 1024**3}
            )
            mismatched = {
                "schema_version": 1,
                "approvals": {
                    "default": {
                        "approved_max_bytes": 32 * 1024**3,
                        "confirmation_sha256": hashlib.sha256(
                            onboarding._database_confirmation("default", 32 * 1024**3).encode("utf-8")
                        ).hexdigest(),
                    }
                },
            }
            receipt_path.write_text(json.dumps(mismatched), encoding="utf-8")
            mismatch = onboarding.inspect_daily_workload_capacity(
                home=home, journal_root=root, journal_date="2026-07-27",
            )
            reconciled = onboarding.approve_database_size(
                home=home, journal_root=root, profile="default",
                confirmation=mismatch["confirmation_phrase"],
            )
            self.assertFalse(reconciled["receipt_already_present"])
            self.assertEqual(
                onboarding.load_database_size_approvals(root), {"default": 16 * 1024**3}
            )

    def test_inventory_and_plan_require_enabled_database_evidence_before_sqlite(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "journal"
            root.mkdir()
            database = home / "state.db"
            create_db(database)
            config = {
                "schema_version": 1, "enabled": True, "timezone": "UTC",
                "profiles": ["default"], "platforms": ["cli"],
                "excluded_session_ids": [],
                "database_size_approvals": {"default": 16 * 1024**3},
                "privacy": {"redact_secrets": True, "pii_mode": "mask", "entropy_mode": "report"},
                "limits": dict(onboarding.GUIDED_LIMITS),
            }
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            with mock.patch.object(
                onboarding.sqlite3, "connect", side_effect=AssertionError("SQLite opened")
            ) as connect:
                inventory = onboarding.discover_setup_inventory(home=home, journal_root=root)
            connect.assert_not_called()
            self.assertEqual(inventory["blocked_databases"][0]["required_tier_gib"], 16)
            self.assertTrue(inventory["blocked_databases"][0]["evidence_missing"])
            with mock.patch.object(
                onboarding.sqlite3, "connect", side_effect=AssertionError("SQLite opened")
            ) as connect, self.assertRaisesRegex(ValueError, "database size approval evidence required"):
                onboarding.plan_guided_setup(
                    home=home, journal_root=root, profiles=["default"], platforms=["cli"],
                    timezone_name="UTC", pii_mode="mask", entropy_mode="report",
                )
            connect.assert_not_called()
            config["database_size_approvals"] = {"default": 32 * 1024**3}
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            with database.open("r+b") as stream:
                stream.truncate(32 * 1024**3 + 4096)
            with mock.patch.object(
                onboarding.sqlite3, "connect", side_effect=AssertionError("SQLite opened")
            ) as connect:
                absolute = onboarding.inspect_daily_workload_capacity(
                    home=home, journal_root=root, journal_date="2026-07-27",
                )
            connect.assert_not_called()
            self.assertEqual(absolute["reason"], "absolute_database_limit_exceeded")
            self.assertNotIn("confirmation_phrase", absolute)

    def test_bridge_inventory_rejects_nonempty_write_ahead_log(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            onboarding.os.environ, {"MY_JOURNAL_IMMUTABLE_DATABASES": "1"}, clear=True
        ):
            home = Path(tmp) / "home"
            database = home / "state.db"
            create_db(database)
            (home / "state.db-wal").write_bytes(b"active")

            with self.assertRaisesRegex(ValueError, "write ahead log"):
                onboarding.discover_setup_inventory(home=home)

    def test_setup_inventory_lists_profile_and_platform_metadata_without_message_bodies(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            default = home / "state.db"
            other = home / "profiles" / "other" / "state.db"
            create_db(default)
            create_db(other)
            add_message(default, "s1", "discord", "user", 1_700_000_000.0)
            add_message(other, "s2", "telegram", "user", 1_700_000_001.0)

            result = onboarding.discover_setup_inventory(home=home)

            self.assertTrue(result["ok"])
            self.assertEqual(result["profiles"], ["default", "other"])
            self.assertEqual(result["platforms"], ["discord", "telegram"])
            self.assertEqual(result["database_count"], 2)
            self.assertNotIn("SECRET BODY", json.dumps(result))

    def test_empty_retained_history_returns_no_activity_without_creating_plan(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home, root = base / "home", base / "journal"
            create_db(home / "state.db")

            result = onboarding.plan_guided_setup(
                home=home,
                journal_root=root,
                profiles=["default"],
                platforms=["cli"],
                timezone_name="UTC",
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "no_eligible_activity")
            self.assertEqual(result["activity_dates"], [])
            self.assertFalse((root / "approval-plans").exists())

    def test_discovery_filters_before_accounting_and_uses_local_activity_dates(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            default = home / "state.db"
            other = home / "profiles" / "other" / "state.db"
            create_db(default)
            create_db(other)
            # 00:30 UTC is still the previous day in America/Los_Angeles.
            boundary = datetime(2026, 7, 27, 0, 30, tzinfo=timezone.utc).timestamp()
            add_message(default, "excluded", "cli", "user", boundary - 86400, 1)
            add_message(default, "system-only", "cli", "system", boundary - 500, 2)
            add_message(default, "wrong-platform", "telegram", "user", boundary - 100, 3)
            add_message(default, "eligible", "cli", "user", boundary, 4)
            add_message(other, "wrong-profile", "cli", "user", boundary + 86400, 1)

            result = onboarding.discover_activity_metadata(
                home=home,
                profiles=["default"],
                platforms=["cli"],
                timezone_name="America/Los_Angeles",
                excluded_session_ids=["excluded"],
            )

            self.assertEqual(result["activity_dates"], ["2026-07-26"])
            self.assertEqual(result["workload_count"], 1)
            self.assertEqual(result["session_count"], 1)
            self.assertEqual(result["database_count"], 1)

    def test_plan_persists_exact_bounded_selection_with_overrides_and_existing_dates(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home, root = base / "home", base / "journal"
            db = home / "state.db"
            create_db(db)
            for index, day in enumerate((25, 26, 27), 1):
                stamp = datetime(2026, 7, day, 12, tzinfo=timezone.utc).timestamp()
                add_message(db, f"s{index}", "cli", "user", stamp, index)

            with mock.patch.object(
                onboarding, "validated_entry_dates", return_value={"2026-07-26"}
            ):
                result = onboarding.plan_guided_setup(
                    home=home,
                    journal_root=root,
                    profiles=["default"],
                    platforms=["cli"],
                    timezone_name="UTC",
                    start_date="2026-07-26",
                    end_date="2026-07-27",
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["activity_dates"], ["2026-07-26", "2026-07-27"])
            self.assertEqual(result["existing_dates"], ["2026-07-26"])
            self.assertEqual(result["missing_dates"], ["2026-07-27"])
            self.assertEqual(result["workload_count"], 2)
            self.assertRegex(result["plan_id"], r"^[0-9a-f]{32}$")
            self.assertRegex(
                result["confirmation_phrase"],
                rf"^ENABLE MY JOURNAL PLAN {result['plan_id']} SHA256 [0-9a-f]{{64}}$",
            )
            plan = json.loads(Path(result["plan_path"]).read_text(encoding="utf-8"))
            self.assertEqual(plan["confirmation_phrase"], result["confirmation_phrase"])
            self.assertEqual(plan["profiles"], ["default"])
            self.assertEqual(plan["platforms"], ["cli"])
            self.assertEqual(plan["missing_dates"], ["2026-07-27"])
            self.assertFalse((root / "config.json").exists())

    def test_plan_streams_metadata_above_daily_collection_ceiling(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            db = home / "state.db"
            create_db(db)
            stamp = datetime(2026, 7, 27, 12, tzinfo=timezone.utc).timestamp()
            add_message(db, "s1", "cli", "user", stamp, 1)
            add_message(db, "s2", "cli", "assistant", stamp + 1, 2)
            with mock.patch.object(onboarding, "PLANNING_MAX_SELECTED_MESSAGES", 1):
                result = onboarding.plan_guided_setup(
                    home=home,
                    journal_root=Path(tmp) / "journal",
                    profiles=["default"],
                    platforms=["cli"],
                    timezone_name="UTC",
                )
            self.assertTrue(result["ok"])
            self.assertEqual(result["workload_count"], 2)

    def test_plan_selects_smallest_sufficient_daily_message_tier(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            db = home / "state.db"
            create_db(db)
            stamp = datetime(2026, 7, 27, 12, tzinfo=timezone.utc).timestamp()
            add_message(db, "s1", "cli", "user", stamp, 1)
            add_message(db, "s1", "cli", "assistant", stamp + 1, 2)
            with mock.patch.object(onboarding, "DAILY_MESSAGE_TIERS", (1, 3, 5)), mock.patch.object(
                onboarding._journal_config, "DAILY_MESSAGE_TIERS", (1, 3, 5),
            ):
                result = onboarding.plan_guided_setup(
                    home=home, journal_root=Path(tmp) / "journal",
                    profiles=["default"], platforms=["cli"], timezone_name="UTC",
                )
            self.assertEqual(result["limits"]["max_selected_messages"], 3)
            self.assertEqual(result["maximum_daily_message_count"], 2)

    def test_metadata_role_filter_matches_collector_case_and_null_rules(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            db = home / "state.db"
            create_db(db)
            stamp = datetime(2026, 7, 27, 12, tzinfo=timezone.utc).timestamp()
            add_message(db, "s1", "cli", "System", stamp, 1)
            add_message(db, "s1", "cli", "Developer", stamp + 1, 2)
            add_message(db, "s1", "cli", None, stamp + 2, 3)
            add_message(db, "s1", "cli", "user", stamp + 3, 4)
            metadata = onboarding.discover_activity_metadata(
                home=home, profiles=["default"], platforms=["cli"],
                timezone_name="UTC", excluded_session_ids=[],
            )
            self.assertEqual(metadata["workload_count"], 2)
            self.assertEqual(metadata["activity_date_counts"], {"2026-07-27": 2})

    def test_targeted_daily_workload_approval_updates_only_limit_and_supports_retry(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "journal"
            db = home / "state.db"
            create_db(db)
            stamp = datetime(2026, 7, 27, 12, tzinfo=timezone.utc).timestamp()
            add_message(db, "s1", "cli", "user", stamp, 1)
            with mock.patch.object(onboarding, "DAILY_MESSAGE_TIERS", (1, 3, 5)), mock.patch.object(
                onboarding._journal_config, "DAILY_MESSAGE_TIERS", (1, 3, 5),
            ):
                plan = onboarding.plan_guided_setup(
                    home=home, journal_root=root, profiles=["default"],
                    platforms=["cli"], timezone_name="UTC",
                )
                onboarding.approve_guided_setup(
                    journal_root=root, plan_id=plan["plan_id"],
                    confirmation=plan["confirmation_phrase"],
                    generation_fn=lambda *args, **kwargs: {"ok": True},
                )
                original = json.loads((root / "config.json").read_text(encoding="utf-8"))
                self.assertEqual(original["limits"]["max_selected_messages"], 1)
                add_message(db, "s1", "cli", "assistant", stamp + 1, 2)
                blocked = onboarding.inspect_daily_workload_capacity(
                    home=home, journal_root=root, journal_date="2026-07-27",
                )
                self.assertFalse(blocked["ok"])
                self.assertEqual(blocked["required_tier"], 3)
                with self.assertRaisesRegex(ValueError, "exact confirmation"):
                    onboarding.approve_daily_workload(
                        home=home, journal_root=root, journal_date="2026-07-27",
                        confirmation="yes",
                    )
                self.assertFalse((root / "daily-workload-approval.json").exists())
                approved = onboarding.approve_daily_workload(
                    home=home, journal_root=root, journal_date="2026-07-27",
                    confirmation=blocked["confirmation_phrase"],
                )
                self.assertEqual(approved["approved_tier"], 3)
                updated = json.loads((root / "config.json").read_text(encoding="utf-8"))
                self.assertEqual(updated["limits"]["max_selected_messages"], 3)
                expected = json.loads(json.dumps(original))
                expected["limits"]["max_selected_messages"] = 3
                self.assertEqual(updated, expected)
                self.assertTrue((root / "daily-workload-approval.json").is_file())
                ready = onboarding.inspect_daily_workload_capacity(
                    home=home, journal_root=root, journal_date="2026-07-27",
                )
                self.assertTrue(ready["ok"])
                next_stamp = datetime(2026, 7, 28, 12, tzinfo=timezone.utc).timestamp()
                add_message(db, "s2", "cli", "user", next_stamp, 3)
                add_message(db, "s2", "cli", "assistant", next_stamp + 1, 4)
                future = onboarding.inspect_daily_workload_capacity(
                    home=home, journal_root=root, journal_date="2026-07-28",
                )
                self.assertTrue(future["ok"])
                self.assertEqual(future["approved_tier"], 3)
                before_recovery = (root / "config.json").read_bytes()
                (root / "daily-workload-approval.json").unlink()
                missing = onboarding.inspect_daily_workload_capacity(
                    home=home, journal_root=root, journal_date="2026-07-27",
                )
                self.assertEqual(missing["reason"], "daily_workload_evidence_missing")
                self.assertEqual(missing["required_tier"], 3)
                recovered = onboarding.approve_daily_workload(
                    home=home, journal_root=root, journal_date="2026-07-27",
                    confirmation=missing["confirmation_phrase"],
                )
                self.assertTrue(recovered["evidence_recovered"])
                self.assertFalse(recovered["config_updated"])
                self.assertEqual((root / "config.json").read_bytes(), before_recovery)
                self.assertTrue((root / "daily-workload-approval.json").is_file())

    def test_targeted_daily_approval_repeats_same_phrase_after_receipt_first_interruption(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "journal"
            root.mkdir()
            db = home / "state.db"
            create_db(db)
            stamp = datetime(2026, 7, 27, 12, tzinfo=timezone.utc).timestamp()
            add_message(db, "s1", "cli", "user", stamp, 1)
            add_message(db, "s1", "cli", "assistant", stamp + 1, 2)
            config = {
                "schema_version": 1, "enabled": True, "timezone": "UTC",
                "profiles": ["default"], "platforms": ["cli"],
                "excluded_session_ids": [], "database_size_approvals": {},
                "privacy": {"redact_secrets": True, "pii_mode": "mask", "entropy_mode": "report"},
                "limits": {
                    "max_message_chars": 4000, "max_tool_chars": 1200,
                    "max_selected_messages": 1, "max_retained_chars": 4000000,
                    "max_sessions": 2000, "packet_chunk_bytes": 120000,
                    "max_packet_chunks": 64,
                },
            }
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            with mock.patch.object(onboarding, "DAILY_MESSAGE_TIERS", (1, 3, 5)), mock.patch.object(
                onboarding._journal_config, "DAILY_MESSAGE_TIERS", (1, 3, 5),
            ):
                blocked = onboarding.inspect_daily_workload_capacity(
                    home=home, journal_root=root, journal_date="2026-07-27",
                )
                real_write = onboarding._safe_files.safe_atomic_write_text

                def fail_config_publication(write_root, target, text):
                    if target.name == "config.json":
                        raise OSError("simulated configuration publication failure")
                    return real_write(write_root, target, text)

                with mock.patch.object(
                    onboarding._safe_files, "safe_atomic_write_text",
                    side_effect=fail_config_publication,
                ), self.assertRaisesRegex(OSError, "publication failure"):
                    onboarding.approve_daily_workload(
                        home=home, journal_root=root, journal_date="2026-07-27",
                        confirmation=blocked["confirmation_phrase"],
                    )
                interrupted = onboarding.inspect_daily_workload_capacity(
                    home=home, journal_root=root, journal_date="2026-07-27",
                )
                self.assertEqual(interrupted["reason"], "daily_workload_approval_interrupted")
                self.assertEqual(interrupted["confirmation_phrase"], blocked["confirmation_phrase"])
                recovered = onboarding.approve_daily_workload(
                    home=home, journal_root=root, journal_date="2026-07-27",
                    confirmation=blocked["confirmation_phrase"],
                )
                self.assertTrue(recovered["config_updated"])
                final = json.loads((root / "config.json").read_text(encoding="utf-8"))
                self.assertEqual(final["limits"]["max_selected_messages"], 3)

    def test_targeted_daily_approval_rejects_configuration_mutation_before_limit_write(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "journal"
            root.mkdir()
            db = home / "state.db"
            create_db(db)
            stamp = datetime(2026, 7, 27, 12, tzinfo=timezone.utc).timestamp()
            add_message(db, "s1", "cli", "user", stamp, 1)
            add_message(db, "s1", "cli", "assistant", stamp + 1, 2)
            config = {
                "schema_version": 1, "enabled": True, "timezone": "UTC",
                "profiles": ["default"], "platforms": ["cli"],
                "excluded_session_ids": [], "database_size_approvals": {},
                "privacy": {"redact_secrets": True, "pii_mode": "mask", "entropy_mode": "report"},
                "limits": {
                    "max_message_chars": 4000, "max_tool_chars": 1200,
                    "max_selected_messages": 1, "max_retained_chars": 4000000,
                    "max_sessions": 2000, "packet_chunk_bytes": 120000,
                    "max_packet_chunks": 64,
                },
            }
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with mock.patch.object(onboarding, "DAILY_MESSAGE_TIERS", (1, 3, 5)), mock.patch.object(
                onboarding._journal_config, "DAILY_MESSAGE_TIERS", (1, 3, 5),
            ):
                blocked = onboarding.inspect_daily_workload_capacity(
                    home=home, journal_root=root, journal_date="2026-07-27",
                )
                real_write = onboarding._safe_files.safe_atomic_write_text

                def mutate_after_receipt(write_root, target, text):
                    real_write(write_root, target, text)
                    if target.name == "daily-workload-approval.json":
                        changed = json.loads(config_path.read_text(encoding="utf-8"))
                        changed["platforms"] = ["telegram"]
                        config_path.write_text(json.dumps(changed), encoding="utf-8")

                with mock.patch.object(
                    onboarding._safe_files, "safe_atomic_write_text", side_effect=mutate_after_receipt,
                ), self.assertRaisesRegex(ValueError, "configuration changed"):
                    onboarding.approve_daily_workload(
                        home=home, journal_root=root, journal_date="2026-07-27",
                        confirmation=blocked["confirmation_phrase"],
                    )
            final = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(final["limits"]["max_selected_messages"], 1)

    def test_generation_capacity_failure_returns_approval_without_collector_launch(self):
        plugin = load_plugin()
        tools = sys.modules[f"{plugin.__name__}.tools"]
        blocked = {
            "ok": False, "reason": "daily_workload_approval_required",
            "journal_date": "2026-07-27", "eligible_message_count": 30_000,
            "approved_tier": 25_000, "required_tier": 50_000,
            "confirmation_phrase": "APPROVE MY JOURNAL DAILY TIER 50000 TRIGGERED BY DATE 2026-07-27",
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"MY_JOURNAL_ROOT": tmp}, clear=False,
        ), mock.patch.object(
            tools, "validated_entry_dates", return_value=set(),
        ), mock.patch.object(
            tools, "inspect_daily_workload_capacity", return_value=blocked,
        ), mock.patch.object(tools.subprocess, "run") as run:
            result = tools._generation_collect("2026-07-27")
        self.assertFalse(result["ok"])
        self.assertEqual(result["required_tier"], 50_000)
        self.assertIn("journal_daily_workload_approve", result["next_action"])
        run.assert_not_called()

    def test_generation_database_growth_returns_exact_profile_tier_before_sqlite_open(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "journal"
            root.mkdir()
            database = home / "state.db"
            create_db(database)
            with database.open("r+b") as stream:
                stream.truncate(8 * 1024**3 + 4096)
            config = {
                "schema_version": 1, "enabled": True, "timezone": "UTC",
                "profiles": ["default"], "platforms": ["cli"],
                "excluded_session_ids": [], "database_size_approvals": {},
                "privacy": {"redact_secrets": True, "pii_mode": "mask", "entropy_mode": "report"},
                "limits": dict(onboarding.GUIDED_LIMITS),
            }
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            with mock.patch.object(
                onboarding.sqlite3, "connect", side_effect=AssertionError("SQLite opened"),
            ):
                blocked = onboarding.inspect_daily_workload_capacity(
                    home=home, journal_root=root, journal_date="2026-07-27",
                )
            self.assertEqual(blocked["reason"], "database_size_approval_required")
            self.assertEqual(blocked["profile"], "default")
            self.assertEqual(blocked["required_tier_gib"], 16)
            self.assertEqual(
                blocked["confirmation_phrase"],
                onboarding._database_confirmation("default", 16 * 1024**3),
            )

    def test_discovery_does_not_materialize_irrelevant_oversized_message_ids(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            db = home / "state.db"
            create_db(db)
            with closing(sqlite3.connect(db)) as con:
                con.execute("alter table messages rename to old_messages")
                con.execute(
                    "create table messages (id text, session_id text, role text, content text, timestamp real)"
                )
                con.execute(
                    "insert into messages values (?, ?, ?, ?, ?)",
                    ("x" * 20_000, "s1", "user", "not selected", 1_700_000_000.0),
                )
                con.execute("insert into sessions values (?, ?, ?)", ("s1", "cli", 1_700_000_000.0))
                con.commit()

            real_dumps = json.dumps

            def reject_large_materialized_strings(value, *args, **kwargs):
                def walk(item):
                    if isinstance(item, str):
                        self.assertLess(len(item), 10_000)
                    elif isinstance(item, dict):
                        for key, child in item.items():
                            walk(key)
                            walk(child)
                    elif isinstance(item, (list, tuple)):
                        for child in item:
                            walk(child)
                walk(value)
                return real_dumps(value, *args, **kwargs)

            with mock.patch.object(onboarding.json, "dumps", side_effect=reject_large_materialized_strings):
                result = onboarding.discover_activity_metadata(
                    home=home,
                    profiles=["default"],
                    platforms=["cli"],
                    timezone_name="UTC",
                )
            self.assertEqual(result["workload_count"], 1)

    def test_plan_rejects_symlinked_journal_root_without_external_writes(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            db = home / "state.db"
            create_db(db)
            add_message(db, "s1", "cli", "user", 1_700_000_000.0)
            external = base / "external"
            external.mkdir()
            root = base / "journal"
            root.symlink_to(external, target_is_directory=True)

            with self.assertRaises((ValueError, OSError)):
                onboarding.plan_guided_setup(
                    home=home,
                    journal_root=root,
                    profiles=["default"],
                    platforms=["cli"],
                    timezone_name="UTC",
                )
            self.assertFalse((external / "approval-plans").exists())

    def test_selected_privacy_modes_are_bound_to_plan_and_enabled_config(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home, root = Path(tmp) / "home", Path(tmp) / "journal"
            db = home / "state.db"
            create_db(db)
            add_message(db, "s1", "cli", "user", 1_700_000_000.0)
            plan = onboarding.plan_guided_setup(
                home=home,
                journal_root=root,
                profiles=["default"],
                platforms=["cli"],
                timezone_name="UTC",
                pii_mode="preserve",
                entropy_mode="off",
            )
            result = onboarding.approve_guided_setup(
                journal_root=root,
                plan_id=plan["plan_id"],
                confirmation=plan["confirmation_phrase"],
                generation_fn=lambda *args, **kwargs: {"ok": True},
            )
            self.assertTrue(result["ok"])
            config = json.loads((root / "config.json").read_text())
            self.assertEqual(config["privacy"]["pii_mode"], "preserve")
            self.assertEqual(config["privacy"]["entropy_mode"], "off")
            self.assertTrue(config["privacy"]["redact_secrets"])

    def test_approval_requires_exact_phrase_and_unchanged_source(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home, root = Path(tmp) / "home", Path(tmp) / "journal"
            db = home / "state.db"
            create_db(db)
            stamp = datetime(2026, 7, 27, 12, tzinfo=timezone.utc).timestamp()
            add_message(db, "s1", "cli", "user", stamp, 1)
            plan = onboarding.plan_guided_setup(
                home=home, journal_root=root, profiles=["default"], platforms=["cli"],
                timezone_name="UTC",
            )

            with self.assertRaisesRegex(ValueError, "exact confirmation"):
                onboarding.approve_guided_setup(
                    journal_root=root, plan_id=plan["plan_id"], confirmation="yes"
                )
            self.assertFalse((root / "config.json").exists())

            add_message(db, "s2", "cli", "user", stamp + 1, 2)
            with self.assertRaisesRegex(ValueError, "source.*changed"):
                onboarding.approve_guided_setup(
                    journal_root=root,
                    plan_id=plan["plan_id"],
                    confirmation=plan["confirmation_phrase"],
                )
            self.assertFalse((root / "config.json").exists())

    def test_approval_generates_missing_dates_oldest_first_and_reports_partial_failure(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home, root = Path(tmp) / "home", Path(tmp) / "journal"
            db = home / "state.db"
            create_db(db)
            for index, day in enumerate((25, 26, 27), 1):
                add_message(
                    db, f"s{index}", "cli", "user",
                    datetime(2026, 7, day, 12, tzinfo=timezone.utc).timestamp(), index,
                )
            plan = onboarding.plan_guided_setup(
                home=home, journal_root=root, profiles=["default"], platforms=["cli"],
                timezone_name="UTC",
            )
            generated = []
            events = []

            def collection(journal_date):
                events.append(("collect", journal_date))
                return {"ok": True}

            def generation(request, *, expected_dates):
                generated.extend(expected_dates)
                events.append(("generate", expected_dates[0]))
                return {"ok": expected_dates != ["2026-07-26"]}

            result = onboarding.approve_guided_setup(
                journal_root=root,
                plan_id=plan["plan_id"],
                confirmation=plan["confirmation_phrase"],
                generation_fn=generation,
                collection_fn=collection,
            )

            self.assertFalse(result["ok"])
            self.assertEqual(
                events,
                [
                    ("collect", "2026-07-25"),
                    ("collect", "2026-07-26"),
                    ("collect", "2026-07-27"),
                    ("generate", "2026-07-25"),
                    ("generate", "2026-07-26"),
                    ("generate", "2026-07-27"),
                ],
            )
            self.assertEqual(generated, ["2026-07-25", "2026-07-26", "2026-07-27"])
            self.assertEqual(result["completed_dates"], ["2026-07-25", "2026-07-27"])
            self.assertEqual(result["failed_dates"], ["2026-07-26"])
            self.assertNotIn("schedule_offer", result)
            config = json.loads((root / "config.json").read_text(encoding="utf-8"))
            self.assertTrue(config["enabled"])
            self.assertTrue(config["privacy"]["redact_secrets"])
            self.assertEqual(config["profiles"], ["default"])
            self.assertLessEqual(config["limits"]["max_selected_messages"], 100000)

    def test_reapproval_of_partial_plan_retries_only_failed_dates(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home, root = Path(tmp) / "home", Path(tmp) / "journal"
            db = home / "state.db"
            create_db(db)
            for index, day in enumerate((25, 26), 1):
                add_message(
                    db, f"s{index}", "cli", "user",
                    datetime(2026, 7, day, 12, tzinfo=timezone.utc).timestamp(), index,
                )
            plan = onboarding.plan_guided_setup(
                home=home, journal_root=root, profiles=["default"], platforms=["cli"],
                timezone_name="UTC",
            )
            first_calls = []
            valid_dates = set()

            def first_generation(request, *, expected_dates):
                first_calls.extend(expected_dates)
                if expected_dates == ["2026-07-25"]:
                    valid_dates.update(expected_dates)
                    return {"ok": True}
                return {"ok": False}

            with mock.patch.object(
                onboarding, "validated_entry_dates", side_effect=lambda *args: set(valid_dates)
            ):
                first = onboarding.approve_guided_setup(
                    journal_root=root, plan_id=plan["plan_id"],
                    confirmation=plan["confirmation_phrase"], generation_fn=first_generation,
                )
                self.assertEqual(first["completed_dates"], ["2026-07-25"])
                self.assertEqual(first["failed_dates"], ["2026-07-26"])

                second_calls = []

                def second_generation(request, *, expected_dates):
                    second_calls.extend(expected_dates)
                    valid_dates.update(expected_dates)
                    return {"ok": True}

                second = onboarding.approve_guided_setup(
                    journal_root=root, plan_id=plan["plan_id"],
                    confirmation=plan["confirmation_phrase"],
                    generation_fn=second_generation,
                )
            self.assertEqual(second_calls, ["2026-07-26"])
            self.assertEqual(second["completed_dates"], ["2026-07-25", "2026-07-26"])
            self.assertEqual(second["failed_dates"], [])

    def test_reapproval_rejects_configuration_drift_before_generation(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "journal"
            database = home / "state.db"
            create_db(database)
            add_message(database, "s1", "cli", "user", 1_700_000_000.0)
            plan = onboarding.plan_guided_setup(
                home=home, journal_root=root, profiles=["default"],
                platforms=["cli"], timezone_name="UTC",
            )
            onboarding.approve_guided_setup(
                journal_root=root, plan_id=plan["plan_id"],
                confirmation=plan["confirmation_phrase"],
                generation_fn=lambda *args, **kwargs: {"ok": False},
            )
            config = json.loads((root / "config.json").read_text(encoding="utf-8"))
            config["platforms"] = ["discord"]
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            generation = mock.Mock()
            with self.assertRaisesRegex(ValueError, "configuration changed"):
                onboarding.approve_guided_setup(
                    journal_root=root, plan_id=plan["plan_id"],
                    confirmation=plan["confirmation_phrase"], generation_fn=generation,
                )
            generation.assert_not_called()

    def test_reapproval_regenerates_completed_receipt_when_canonical_note_is_missing(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "journal"
            database = home / "state.db"
            create_db(database)
            add_message(database, "s1", "cli", "user", 1_700_000_000.0)
            plan = onboarding.plan_guided_setup(
                home=home, journal_root=root, profiles=["default"],
                platforms=["cli"], timezone_name="UTC",
            )
            generation = mock.Mock(return_value={"ok": True})
            with mock.patch.object(onboarding, "validated_entry_dates", return_value=set()):
                onboarding.approve_guided_setup(
                    journal_root=root, plan_id=plan["plan_id"],
                    confirmation=plan["confirmation_phrase"], generation_fn=generation,
                )
                onboarding.approve_guided_setup(
                    journal_root=root, plan_id=plan["plan_id"],
                    confirmation=plan["confirmation_phrase"], generation_fn=generation,
                )
            self.assertEqual(generation.call_count, 2)

    def test_setup_rejects_oversized_allowlist_and_exclusion_values(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "journal"
            with self.assertRaisesRegex(ValueError, "profiles value limit"):
                onboarding.plan_guided_setup(
                    home=home, journal_root=root, profiles=["p" * 513],
                    platforms=["cli"], timezone_name="UTC",
                )
            with self.assertRaisesRegex(ValueError, "excluded_session_ids value limit"):
                onboarding.plan_guided_setup(
                    home=home, journal_root=root, profiles=["default"],
                    platforms=["cli"], timezone_name="UTC",
                    excluded_session_ids=["s" * 513],
                )

    def test_corrupted_approval_plan_fails_before_config_or_generation(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "journal"
            database = home / "state.db"
            create_db(database)
            add_message(database, "s1", "cli", "user", 1_700_000_000.0)
            plan = onboarding.plan_guided_setup(
                home=home, journal_root=root, profiles=["default"],
                platforms=["cli"], timezone_name="UTC",
            )
            plan_path = root / "approval-plans" / f"{plan['plan_id']}.json"
            corrupted = json.loads(plan_path.read_text(encoding="utf-8"))
            corrupted["activity_dates"] = "2023-11-14"
            plan_path.write_text(json.dumps(corrupted), encoding="utf-8")
            generation = mock.Mock()
            with self.assertRaisesRegex(ValueError, "approval plan is malformed"):
                onboarding.approve_guided_setup(
                    journal_root=root, plan_id=plan["plan_id"],
                    confirmation=plan["confirmation_phrase"], generation_fn=generation,
                )
            self.assertFalse((root / "config.json").exists())
            generation.assert_not_called()

    def test_valid_compiled_tier_tampering_invalidates_exact_plan_confirmation(self):
        plugin = load_plugin()
        onboarding = sys.modules[f"{plugin.__name__}.onboarding"]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            root = Path(tmp) / "journal"
            database = home / "state.db"
            create_db(database)
            add_message(database, "s1", "cli", "user", 1_700_000_000.0)
            plan = onboarding.plan_guided_setup(
                home=home, journal_root=root, profiles=["default"],
                platforms=["cli"], timezone_name="UTC",
            )
            plan_path = root / "approval-plans" / f"{plan['plan_id']}.json"
            corrupted = json.loads(plan_path.read_text(encoding="utf-8"))
            corrupted["limits"]["max_selected_messages"] = 100_000
            plan_path.write_text(json.dumps(corrupted), encoding="utf-8")
            generation = mock.Mock()
            with self.assertRaisesRegex(ValueError, "approval plan is malformed"):
                onboarding.approve_guided_setup(
                    journal_root=root, plan_id=plan["plan_id"],
                    confirmation=plan["confirmation_phrase"], generation_fn=generation,
                )
            self.assertFalse((root / "config.json").exists())
            generation.assert_not_called()

    def test_registers_guided_setup_tools_and_cli_without_replacing_slash_command(self):
        plugin = load_plugin()

        class Context:
            def __init__(self):
                self.tools, self.cli, self.slash = {}, None, []
            def register_tool(self, **kwargs):
                self.tools[kwargs["name"]] = kwargs
            def register_cli_command(self, **kwargs):
                self.cli = kwargs
            def register_command(self, *args, **kwargs):
                self.slash.append((args, kwargs))

        ctx = Context()
        plugin.register(ctx)
        self.assertIn("journal_setup_inventory", ctx.tools)
        self.assertIn("journal_setup_database_approve", ctx.tools)
        self.assertIn("journal_daily_workload_check", ctx.tools)
        self.assertIn("journal_daily_workload_approve", ctx.tools)
        self.assertIn("journal_setup_plan", ctx.tools)
        self.assertIn("journal_setup_approve", ctx.tools)
        self.assertEqual(ctx.tools["journal_setup_plan"]["toolset"], "journal")
        plan_schema = ctx.tools["journal_setup_plan"]["schema"]["parameters"]["properties"]
        self.assertEqual(plan_schema["profiles"]["maxItems"], 128)
        self.assertEqual(plan_schema["platforms"]["maxItems"], 256)
        self.assertEqual(plan_schema["excluded_session_ids"]["maxItems"], 10000)
        self.assertEqual(ctx.slash, [])
        import argparse
        parser = argparse.ArgumentParser()
        ctx.cli["setup_fn"](parser)
        inventory = parser.parse_args(["setup-inventory"])
        database_approval = parser.parse_args([
            "setup-database-approve", "default", "--confirm", "EXACT PHRASE",
        ])
        workload_check = parser.parse_args(["workload-check", "2026-07-27"])
        workload_approval = parser.parse_args([
            "workload-approve", "2026-07-27", "--confirm", "EXACT PHRASE",
        ])
        with self.assertRaises(SystemExit):
            parser.parse_args([
                "setup-plan", "--profile", "default", "--platform", "cli",
                "--timezone", "UTC",
            ])
        planned = parser.parse_args([
            "setup-plan", "--profile", "default", "--platform", "cli",
            "--timezone", "UTC", "--pii-mode", "mask", "--entropy-mode", "report",
        ])
        approved = parser.parse_args([
            "setup-approve", "a" * 32, "--confirm", "ENABLE MY JOURNAL PLAN " + "a" * 32,
        ])
        self.assertEqual(planned.journal_command, "setup-plan")
        self.assertEqual(inventory.journal_command, "setup-inventory")
        self.assertEqual(database_approval.journal_command, "setup-database-approve")
        self.assertEqual(workload_check.journal_command, "workload-check")
        self.assertEqual(workload_approval.journal_command, "workload-approve")
        self.assertEqual(approved.journal_command, "setup-approve")


if __name__ == "__main__":
    unittest.main()
