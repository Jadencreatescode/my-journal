import importlib.util
import io
import json
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import contextmanager, redirect_stderr
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "collect_journal.py"


def load_module():
    spec = importlib.util.spec_from_file_location("collect_journal", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def db_connection(path):
    con = sqlite3.connect(path)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def create_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with db_connection(path) as con:
        con.execute(
            """create table sessions (
                id text primary key,
                source text not null,
                started_at real not null,
                title text,
                user_id text,
                chat_id text,
                thread_id text,
                display_name text,
                profile_name text
            )"""
        )
        con.execute(
            """create table messages (
                id integer primary key,
                session_id text not null,
                role text not null,
                content text,
                timestamp real not null,
                tool_calls text,
                tool_name text,
                finish_reason text,
                reasoning text,
                reasoning_content text
            )"""
        )


def create_legacy_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with db_connection(path) as con:
        con.execute("create table sessions (id text primary key, source text not null, started_at real not null, title text)")
        con.execute("create table messages (id integer primary key, session_id text not null, role text not null, content text, timestamp real not null)")


def add_session(con, session_id, source, started_at, title):
    con.execute(
        "insert into sessions (id, source, started_at, title) values (?, ?, ?, ?)",
        (session_id, source, started_at, title),
    )


def add_message(con, message_id, session_id, role, content, timestamp, tool_calls=None, tool_name=None):
    con.execute(
        """insert into messages
           (id, session_id, role, content, timestamp, tool_calls, tool_name)
           values (?, ?, ?, ?, ?, ?, ?)""",
        (message_id, session_id, role, content, timestamp, tool_calls, tool_name),
    )


class DiscoverDatabaseTests(unittest.TestCase):
    def test_atomic_write_replaces_content_without_temp_residue(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "record.json"
            module.atomic_write_text(target, "first")
            module.atomic_write_text(target, "second")

            self.assertEqual(target.read_text(encoding="utf-8"), "second")
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_cli_rejects_nonpositive_text_bounds(self):
        module = load_module()
        parser = module.build_parser()
        for flag in ("--max-message-chars", "--max-tool-chars"):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser.parse_args([flag, "0"])
                with self.assertRaises(SystemExit):
                    parser.parse_args([flag, "-1"])

    def test_collects_from_path_containing_uri_control_characters(self):
        module = load_module()
        with tempfile.TemporaryDirectory(prefix="journal?") as tmp:
            home = Path(tmp)
            db = home / "state.db"
            create_db(db)
            with db_connection(db) as con:
                add_session(con, "special", "telegram", 100.0, "Special path")
                add_message(con, 1, "special", "user", "Captured", 110.0)

            manifest = module.collect_range(home, 100.0, 200.0)

            self.assertEqual(manifest["coverage"]["database_error_count"], 0)
            self.assertEqual(manifest["coverage"]["session_count"], 1)

    def test_rejects_symlinked_default_database(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            external = root / "outside.db"
            create_db(external)
            home.mkdir()
            (home / "state.db").symlink_to(external)

            self.assertEqual(module.discover_databases(home), [])

    def test_rejects_symlinked_profile_database(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            external = root / "outside.db"
            create_db(external)
            profile = home / "profiles" / "writer"
            profile.mkdir(parents=True)
            (profile / "state.db").symlink_to(external)

            self.assertEqual(module.discover_databases(home), [])

    def test_rejects_symlinked_profiles_root(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            actual_profiles = home / "actual-profiles"
            create_db(actual_profiles / "writer" / "state.db")
            (home / "profiles").symlink_to(actual_profiles, target_is_directory=True)

            self.assertEqual(module.discover_databases(home), [])

    def test_rejects_hermes_home_with_symlinked_ancestor(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trusted = root / "trusted"
            external = root / "external"
            trusted.mkdir()
            create_db(external / "home" / "state.db")
            (trusted / "link").symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(module.UnsafePathError, "symlink"):
                module.collect_range(
                    trusted / "link" / "home",
                    start_ts=0.0,
                    end_ts=86400.0,
                )

    def test_discovers_default_and_profile_databases(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            create_db(home / "state.db")
            create_db(home / "profiles" / "writer" / "state.db")
            create_db(home / "profiles" / "empty" / "state.db")

            found = module.discover_databases(home)

            self.assertEqual(
                [(item.profile, item.path.relative_to(home).as_posix()) for item in found],
                [
                    ("default", "state.db"),
                    ("empty", "profiles/empty/state.db"),
                    ("writer", "profiles/writer/state.db"),
                ],
            )


class CollectionTests(unittest.TestCase):
    def test_redacts_standalone_telegram_bot_token(self):
        module = load_module()
        token = "1234567890:" + "A" * 35

        redacted = module.redact_text(f"telegram value {token}")

        self.assertNotIn(token, redacted)
        self.assertIn("[REDACTED]", redacted)
        self.assertFalse(module.contains_likely_secret(redacted))

    def test_redacts_standalone_discord_bot_token(self):
        module = load_module()
        token = "MTAwMDAwMDAwMDAwMDAwMDAw" + ".GhIjKl." + "Z" * 27

        redacted = module.redact_text(f"discord value {token}")

        self.assertNotIn(token, redacted)
        self.assertIn("[REDACTED]", redacted)
        self.assertFalse(module.contains_likely_secret(redacted))

    def test_redacts_google_oauth_access_token(self):
        module = load_module()
        token = "ya29." + "A" * 64

        redacted = module.redact_text(f"oauth value {token}")

        self.assertNotIn(token, redacted)
        self.assertIn("[REDACTED]", redacted)
        self.assertFalse(module.contains_likely_secret(redacted))

    def test_redacts_twilio_api_key(self):
        module = load_module()
        token = "SK" + "a1" * 16

        redacted = module.redact_text(f"twilio value {token}")

        self.assertNotIn(token, redacted)
        self.assertIn("[REDACTED]", redacted)
        self.assertFalse(module.contains_likely_secret(redacted))

    def test_redacts_google_oauth_refresh_token(self):
        module = load_module()
        token = "1//" + "A" * 64

        redacted = module.redact_text(f"oauth refresh {token}")

        self.assertNotIn(token, redacted)
        self.assertIn("[REDACTED]", redacted)
        self.assertFalse(module.contains_likely_secret(redacted))

    def test_masks_email_phone_and_reports_suspicious_entropy(self):
        module = load_module()
        email = "alice.private@example.com"
        phone = "+1 (415) 555-0123"
        opaque = "Q7vN3kLp9Wx2Za8Bc4Df6Gh1Jm5Rt0Yu"

        result = module.redact_sensitive(
            f"contact {email} or {phone}; token-like {opaque}",
            pii_mode="mask",
            entropy_mode="report",
        )

        self.assertNotIn(email, result.text)
        self.assertNotIn(phone, result.text)
        self.assertNotIn(opaque, result.text)
        self.assertEqual(result.finding_counts["email"], 1)
        self.assertEqual(result.finding_counts["phone"], 1)
        self.assertEqual(result.finding_counts["high_entropy"], 1)

    def test_entropy_scanner_preserves_hashes_uuids_and_ordinary_identifiers(self):
        module = load_module()
        safe_values = [
            "a" * 64,
            "550e8400-e29b-41d4-a716-446655440000",
            "issue-12345678",
            "2026-07-27T12:34:56Z",
        ]

        result = module.redact_sensitive(
            " ".join(safe_values),
            pii_mode="mask",
            entropy_mode="report",
        )

        for value in safe_values:
            self.assertIn(value, result.text)
        self.assertEqual(result.finding_counts.get("high_entropy", 0), 0)

    def test_redacts_multiline_authorization_without_reprocessing_marker(self):
        module = load_module()
        for scheme in ("Basic", "Bearer", "Bot", "Custom", "Token", "Unknown"):
            raw = f"authorization: {scheme} alpha beta gamma\nnext: safe"
            redacted = module.redact_text(raw)
            self.assertEqual(redacted, "Authorization: [REDACTED]\nnext: safe")
            self.assertFalse(module.contains_likely_secret(redacted))
            self.assertEqual(module.redact_text(redacted), redacted)

    def test_redacts_unquoted_authorization_assignments_as_one_field(self):
        module = load_module()
        leaked_values = [
            "Bot MTAwMDAwMDAwMDAwMDAwMDAw.GhIjKl.signaturevalue",
            "Bearer secret value with spaces",
            "Custom short value",
        ]
        for value in leaked_values:
            raw = f"authorization={value}"
            redacted = module.redact_text(raw)
            self.assertEqual(redacted, "authorization=[REDACTED]")
            self.assertFalse(module.contains_likely_secret(redacted))
            self.assertNotIn(value, redacted)

        json_raw = '{"authorization": "Bot secret value with spaces", "safe": true}'
        json_redacted = module.redact_text(json_raw)
        self.assertNotIn("secret value with spaces", json_redacted)
        self.assertIn('"safe": true', json_redacted)
        self.assertFalse(module.contains_likely_secret(json_redacted))

    def test_redacts_broad_secret_families_without_leaving_trailing_values(self):
        module = load_module()
        cases = [
            "Authorization: Basic " + "dXNlcjpwYXNzd29yZA==",
            'Authorization: Bearer "' + "quoted secret with spaces" + '"',
            "Authorization: Bot " + "MTAwMDAwMDAwMDAwMDAwMDAw.GhIjKl.signaturevalue",
            'password="' + "two words secret" + '"',
            "token='" + "three words secret" + "'",
            "ghp_" + "A" * 36,
            "AKIA" + "B" * 16,
            "AIza" + "C" * 35,
            "hf_" + "E" * 32,
            "sk_live_" + "D" * 28,
            "postgresql://alice:p%40ss%20word@example.test/db",
            "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        ]

        for raw in cases:
            redacted = module.redact_text(raw)
            self.assertIn("[REDACTED", redacted)
            self.assertFalse(module.contains_likely_secret(redacted))
            self.assertNotIn("two words secret", redacted)
            self.assertNotIn("three words secret", redacted)

    def test_collects_legacy_profile_without_optional_session_columns(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            db = home / "profiles" / "legacy" / "state.db"
            create_legacy_db(db)
            with db_connection(db) as con:
                add_session(con, "old1", "telegram", 100.0, "Legacy session")
                con.execute(
                    "insert into messages (id, session_id, role, content, timestamp) values (?, ?, ?, ?, ?)",
                    (1, "old1", "user", "Legacy request", 110.0),
                )

            manifest = module.collect_range(home, start_ts=100.0, end_ts=200.0)

            self.assertEqual(manifest["coverage"]["database_error_count"], 0)
            self.assertEqual(manifest["coverage"]["session_count"], 1)
            self.assertEqual(manifest["sessions"][0]["platform"], "telegram")
            self.assertIsNone(manifest["sessions"][0]["chat_id"])

    def test_global_message_limit_aborts_collection(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            db = home / "state.db"
            create_db(db)
            with db_connection(db) as con:
                add_session(con, "s1", "discord", 100.0, "Busy session")
                for message_id in range(1, 4):
                    add_message(con, message_id, "s1", "user", f"message {message_id}", 100.0 + message_id)

            with self.assertRaisesRegex(module.CollectionLimitError, "selected message limit"):
                module.collect_range(
                    home,
                    start_ts=100.0,
                    end_ts=200.0,
                    max_selected_messages=2,
                )

    def test_global_retained_character_limit_aborts_collection(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            db = home / "state.db"
            create_db(db)
            with db_connection(db) as con:
                add_session(con, "s1", "discord", 100.0, "Busy session")
                add_message(con, 1, "s1", "user", "A" * 30, 110.0)
                add_message(con, 2, "s1", "assistant", "B" * 30, 120.0)

            with self.assertRaisesRegex(module.CollectionLimitError, "retained character limit"):
                module.collect_range(
                    home,
                    start_ts=100.0,
                    end_ts=200.0,
                    max_retained_chars=50,
                )

    def test_global_session_limit_aborts_collection(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            db = home / "state.db"
            create_db(db)
            with db_connection(db) as con:
                add_session(con, "s1", "discord", 100.0, "First")
                add_message(con, 1, "s1", "user", "one", 110.0)
                add_session(con, "s2", "discord", 100.0, "Second")
                add_message(con, 2, "s2", "user", "two", 120.0)

            with self.assertRaisesRegex(module.CollectionLimitError, "session limit"):
                module.collect_range(
                    home,
                    start_ts=100.0,
                    end_ts=200.0,
                    max_sessions=1,
                )

    def test_profile_allowlist_is_enforced_before_collection(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            create_db(home / "state.db")
            create_db(home / "profiles" / "writer" / "state.db")
            with db_connection(home / "state.db") as con:
                add_session(con, "d1", "discord", 100.0, "Default")
                add_message(con, 1, "d1", "user", "default", 110.0)
            with db_connection(home / "profiles" / "writer" / "state.db") as con:
                add_session(con, "w1", "discord", 100.0, "Writer")
                add_message(con, 1, "w1", "user", "writer", 120.0)

            manifest = module.collect_range(
                home,
                start_ts=100.0,
                end_ts=200.0,
                allowed_profiles={"default"},
            )

            self.assertEqual(manifest["coverage"]["profiles"], ["default"])
            self.assertEqual([item["profile"] for item in manifest["sessions"]], ["default"])
            self.assertEqual([item["profile"] for item in manifest["databases"]], ["default"])

    def test_platform_allowlist_is_enforced_before_counting(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            db = home / "state.db"
            create_db(db)
            with db_connection(db) as con:
                add_session(con, "d1", "discord", 100.0, "Discord")
                add_message(con, 1, "d1", "user", "keep", 110.0)
                add_session(con, "t1", "telegram", 100.0, "Telegram")
                add_message(con, 2, "t1", "user", "drop", 120.0)

            manifest = module.collect_range(
                home,
                start_ts=100.0,
                end_ts=200.0,
                allowed_platforms={"discord"},
            )

            self.assertEqual(manifest["coverage"]["platforms"], ["discord"])
            self.assertEqual(manifest["coverage"]["message_count"], 1)
            self.assertEqual([item["platform"] for item in manifest["sessions"]], ["discord"])

    def test_excluded_sessions_are_removed_before_counting(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            db = home / "state.db"
            create_db(db)
            with db_connection(db) as con:
                add_session(con, "keep", "discord", 100.0, "Keep")
                add_message(con, 1, "keep", "user", "keep", 110.0)
                add_session(con, "private", "discord", 100.0, "Private")
                add_message(con, 2, "private", "user", "drop", 120.0)

            manifest = module.collect_range(
                home,
                start_ts=100.0,
                end_ts=200.0,
                excluded_session_ids={"private"},
            )

            self.assertEqual(manifest["coverage"]["message_count"], 1)
            self.assertEqual([item["session_id"] for item in manifest["sessions"]], ["keep"])
            self.assertNotIn("private", json.dumps(manifest))

    def test_callers_cannot_raise_compiled_collection_ceiling(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)

            with self.assertRaisesRegex(ValueError, "compiled ceiling"):
                module.collect_range(
                    home,
                    start_ts=0.0,
                    end_ts=86400.0,
                    max_selected_messages=module.HARD_MAX_SELECTED_MESSAGES + 1,
                )

    def test_database_collection_remains_anchored_after_path_swap(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            safe_db = home / "state.db"
            create_db(safe_db)
            with db_connection(safe_db) as con:
                add_session(con, "safe-session", "discord", 100.0, "safe title")
                add_message(con, 1, "safe-session", "user", "safe content", 110.0)

            external_home = root / "external"
            evil_db = external_home / "state.db"
            create_db(evil_db)
            with db_connection(evil_db) as con:
                add_session(con, "evil-session", "discord", 100.0, "evil title")
                add_message(con, 1, "evil-session", "user", "evil content", 110.0)

            real_open = module.safe_open_regular_fd
            swapped = False

            def racing_open(trusted_root, target):
                nonlocal swapped
                descriptor = real_open(trusted_root, target)
                if not swapped:
                    swapped = True
                    safe_db.rename(home / "state-original.db")
                    safe_db.symlink_to(evil_db)
                return descriptor

            with mock.patch.object(module, "safe_open_regular_fd", side_effect=racing_open):
                result = module.collect_range(home, 100.0, 200.0)

            self.assertTrue(swapped)
            serialized = json.dumps(result)
            self.assertIn("safe content", serialized)
            self.assertNotIn("evil content", serialized)

    def test_accounts_for_sessions_across_profiles_and_platforms(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            default_db = home / "state.db"
            profile_db = home / "profiles" / "writer" / "state.db"
            create_db(default_db)
            create_db(profile_db)
            with db_connection(default_db) as con:
                add_session(con, "d1", "discord", 100.0, "Discord work")
                add_message(con, 1, "d1", "user", "Design the feature", 110.0)
                add_message(con, 2, "d1", "assistant", "Feature designed", 120.0)
            with db_connection(profile_db) as con:
                add_session(con, "t1", "telegram", 100.0, "Telegram work")
                add_message(con, 1, "t1", "user", "Write the release", 130.0)
                add_message(con, 2, "t1", "assistant", "Release written", 140.0)

            manifest = module.collect_range(home, start_ts=100.0, end_ts=200.0)

            self.assertEqual(manifest["coverage"]["database_count"], 2)
            self.assertEqual(manifest["coverage"]["session_count"], 2)
            self.assertEqual(manifest["coverage"]["message_count"], 4)
            self.assertEqual(manifest["coverage"]["platforms"], ["discord", "telegram"])
            self.assertEqual(manifest["coverage"]["profiles"], ["default", "writer"])
            self.assertEqual(
                {(item["profile"], item["platform"], item["session_id"]) for item in manifest["sessions"]},
                {("default", "discord", "d1"), ("writer", "telegram", "t1")},
            )

    def test_redacts_secrets_excludes_system_text_and_bounds_tool_results(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            db = home / "state.db"
            create_db(db)
            with db_connection(db) as con:
                add_session(con, "s1", "discord", 100.0, "Security test")
                add_message(con, 1, "s1", "system", "hidden system instructions", 110.0)
                add_message(con, 2, "s1", "user", "Deploy with password=supersecret", 120.0)
                add_message(con, 3, "s1", "assistant", "I will deploy", 130.0, tool_calls='[{"name":"terminal","arguments":{"token":"abc123secret"}}]')
                add_message(con, 4, "s1", "tool", "X" * 500, 140.0, tool_name="terminal")

            manifest = module.collect_range(
                home,
                start_ts=100.0,
                end_ts=200.0,
                max_message_chars=100,
                max_tool_chars=60,
            )

            messages = manifest["sessions"][0]["messages"]
            combined = " ".join(str(value) for item in messages for value in item.values())
            self.assertNotIn("hidden system instructions", combined)
            self.assertNotIn("supersecret", combined)
            self.assertNotIn("abc123secret", combined)
            self.assertIn("[REDACTED]", combined)
            tool = next(item for item in messages if item["role"] == "tool")
            self.assertLessEqual(len(tool["content"]), 90)
            self.assertEqual(manifest["coverage"]["message_count"], 4)
            self.assertEqual(manifest["coverage"]["retained_message_count"], 3)

    def test_write_run_records_timezone_and_exact_local_day_window(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            start_ts, end_ts = module.date_window("2026-03-08", "America/Los_Angeles")

            result = module.write_run(
                home=root / "home",
                output_dir=root / "journal",
                journal_date="2026-03-08",
                start_ts=start_ts,
                end_ts=end_ts,
                timezone_name="America/Los_Angeles",
            )

            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["timezone"], "America/Los_Angeles")
            self.assertEqual(manifest["window"]["start_ts"], start_ts)
            self.assertEqual(manifest["window"]["end_ts"], end_ts)
            self.assertEqual(end_ts - start_ts, 23 * 60 * 60)

    def test_write_run_chunks_large_packet_with_deterministic_plan(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            db = home / "state.db"
            create_db(db)
            with db_connection(db) as con:
                add_session(con, "s1", "discord", 100.0, "Chunked session")
                for message_id in range(1, 9):
                    add_message(
                        con,
                        message_id,
                        "s1",
                        "user" if message_id % 2 else "assistant",
                        f"message-{message_id}-" + "x" * 400,
                        100.0 + message_id,
                    )

            result = module.write_run(
                home=home,
                output_dir=root / "journal",
                journal_date="1970-01-01",
                start_ts=0.0,
                end_ts=86400.0,
                packet_chunk_chars=1400,
                max_packet_chunks=10,
            )

            packet_paths = [Path(value) for value in result["packet_paths"]]
            self.assertGreater(len(packet_paths), 1)
            self.assertIsNone(result["packet_path"])
            self.assertTrue(all(path.stat().st_size <= 1400 for path in packet_paths))
            plan = json.loads(Path(result["packet_plan_path"]).read_text(encoding="utf-8"))
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(plan["chunk_count"], len(packet_paths))
            self.assertEqual(manifest["delivery"]["chunk_count"], len(packet_paths))
            self.assertEqual(manifest["delivery"]["chunk_index_sha256"], plan["chunk_index_sha256"])
            self.assertEqual([item["index"] for item in plan["chunks"]], list(range(1, len(packet_paths) + 1)))
            self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", item["chunk_id"]) for item in plan["chunks"]))
            self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", item["body_sha256"]) for item in plan["chunks"]))
            owned = [ref for item in plan["chunks"] for ref in item["owned_session_refs"]]
            self.assertEqual(len(owned), 1)
            self.assertTrue(all(
                set(item["owned_session_refs"]).isdisjoint(item["continued_session_refs"])
                for item in plan["chunks"]
            ))
            self.assertEqual(
                [item["sha256"] for item in plan["chunks"]],
                [module.hashlib.sha256(path.read_bytes()).hexdigest() for path in packet_paths],
            )

    def test_packet_chunk_limit_fails_without_run_artifacts(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            db = home / "state.db"
            create_db(db)
            with db_connection(db) as con:
                add_session(con, "s1", "discord", 100.0, "Chunked session")
                for message_id in range(1, 5):
                    add_message(con, message_id, "s1", "user", "x" * 400, 100.0 + message_id)
            output = root / "journal"

            with self.assertRaisesRegex(module.CollectionLimitError, "packet chunk limit"):
                module.write_run(
                    home=home,
                    output_dir=output,
                    journal_date="1970-01-01",
                    start_ts=0.0,
                    end_ts=86400.0,
                    packet_chunk_chars=1400,
                    max_packet_chunks=1,
                )

            self.assertFalse(output.exists())

    def test_write_run_rejects_symlinked_output_subtree_without_artifacts(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            output = root / "journal"
            external = root / "external"
            output.mkdir()
            external.mkdir()
            (output / "packets").symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(module.UnsafePathError, "symlink"):
                module.write_run(
                    home=home,
                    output_dir=output,
                    journal_date="1970-01-01",
                    start_ts=0.0,
                    end_ts=86400.0,
                )

            self.assertEqual(list(external.iterdir()), [])
            self.assertEqual([path.name for path in output.iterdir()], ["packets"])

    def test_sensitive_policy_applies_to_every_collected_text_field(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            db = home / "state.db"
            create_db(db)
            email = "alice.private@example.com"
            phone = "+1 (415) 555-0123"
            opaque = "Q7vN3kLp9Wx2Za8Bc4Df6Gh1Jm5Rt0Yu"
            chat_id = "private-chat-123456789"
            thread_id = "private-thread-987654321"
            with db_connection(db) as con:
                add_session(con, "s1", "discord", 100.0, email)
                con.execute(
                    "update sessions set display_name = ?, chat_id = ?, thread_id = ? where id = ?",
                    (phone, chat_id, thread_id, "s1"),
                )
                add_message(
                    con, 1, "s1", "user", opaque, 110.0,
                    tool_name=email,
                    tool_calls=json.dumps({"phone": phone}),
                )

            result = module.write_run(
                home=home,
                output_dir=root / "journal",
                journal_date="1970-01-01",
                start_ts=0.0,
                end_ts=86400.0,
                pii_mode="mask",
                entropy_mode="report",
            )

            artifact_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in (root / "journal").rglob("*")
                if path.is_file()
            )
            for sensitive in (email, phone, opaque, chat_id, thread_id, str(db)):
                self.assertNotIn(sensitive, artifact_text)
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["sessions"][0]["session_id"], manifest["sessions"][0]["coverage_ref"])
            counts = manifest["coverage"]["redaction_counts"]
            self.assertGreaterEqual(counts["email"], 2)
            self.assertGreaterEqual(counts["phone"], 2)
            self.assertGreaterEqual(counts["high_entropy"], 1)

    def test_run_identifier_changes_when_evidence_changes_with_same_counts(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            db = home / "state.db"
            create_db(db)
            with db_connection(db) as con:
                add_session(con, "s1", "discord", 100.0, "Stable title")
                add_message(con, 1, "s1", "user", "alpha", 110.0)

            first = module.write_run(home, root / "out", "1970-01-01", 0.0, 86400.0)
            with db_connection(db) as con:
                con.execute("update messages set content = ? where id = ?", ("bravo", 1))
            second = module.write_run(home, root / "out", "1970-01-01", 0.0, 86400.0)

            self.assertNotEqual(first["run_id"], second["run_id"])
            first_manifest = json.loads(Path(first["manifest_path"]).read_text(encoding="utf-8"))
            second_manifest = json.loads(Path(second["manifest_path"]).read_text(encoding="utf-8"))
            self.assertNotEqual(first_manifest["evidence_sha256"], second_manifest["evidence_sha256"])
            self.assertTrue(first_manifest["sessions"][0]["coverage_ref"])

    def test_write_run_creates_manifest_and_packet_with_every_session(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            output = root / "journal"
            db = home / "state.db"
            create_db(db)
            with db_connection(db) as con:
                add_session(con, "s1", "telegram", 100.0, "First session")
                add_message(con, 1, "s1", "user", "First request", 110.0)
                add_session(con, "s2", "discord", 100.0, "Second session")
                add_message(con, 2, "s2", "user", "Second request", 120.0)

            result = module.write_run(
                home=home,
                output_dir=output,
                journal_date="1970-01-01",
                start_ts=0.0,
                end_ts=86400.0,
            )

            manifest_path = Path(result["manifest_path"])
            packet_path = Path(result["packet_path"])
            self.assertTrue(manifest_path.is_file())
            self.assertTrue(packet_path.is_file())
            packet = packet_path.read_text(encoding="utf-8")
            self.assertIn("First session", packet)
            self.assertIn("Second session", packet)
            self.assertIn("First request", packet)
            self.assertIn("Second request", packet)
            self.assertIn(result["run_id"], packet)


if __name__ == "__main__":
    unittest.main()
