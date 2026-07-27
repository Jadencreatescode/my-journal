import importlib.util
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "validate_journal.py"


def load_module():
    spec = importlib.util.spec_from_file_location("validate_journal", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class JournalValidationTests(unittest.TestCase):
    def _minimal_valid_manifest(self, module):
        manifest = {
            "schema_version": 1,
            "journal_date": "2026-07-25",
            "created_at": "2026-07-26T00:00:00+00:00",
            "window": {"start_ts": 100.0, "end_ts": 200.0},
            "coverage": {
                "database_count": 0,
                "database_error_count": 0,
                "session_count": 0,
                "message_count": 0,
                "retained_message_count": 0,
                "platforms": [],
                "profiles": [],
            },
            "databases": [],
            "sessions": [],
        }
        manifest["evidence_sha256"] = module.canonical_evidence_sha256(manifest)
        manifest["run_id"] = manifest["evidence_sha256"][:16]
        return manifest

    def _schema_two_manifest(self, module):
        manifest = self._minimal_valid_manifest(module)
        manifest["schema_version"] = 2
        manifest["timezone"] = "UTC"
        manifest["window"] = {
            "start_ts": datetime(2026, 7, 25, tzinfo=timezone.utc).timestamp(),
            "end_ts": datetime(2026, 7, 26, tzinfo=timezone.utc).timestamp(),
        }
        manifest["coverage"]["retained_char_count"] = 0
        manifest["policy"] = {
            "policy_id": "my-journal-safety-v1",
            "redact_secrets": True,
            "profiles": [],
            "platforms": [],
        }
        chunk = {
            "index": 1,
            "chunk_id": "1" * 64,
            "body_sha256": "2" * 64,
            "sha256": "3" * 64,
            "bytes": 1,
            "owned_session_refs": [],
            "continued_session_refs": [],
        }
        index_hash = hashlib.sha256(
            json.dumps([chunk], sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        manifest["delivery"] = {
            "schema_version": 1,
            "mode": "single",
            "chunk_count": 1,
            "chunk_index_sha256": index_hash,
            "chunks": [chunk],
        }
        manifest["evidence_sha256"] = module.canonical_evidence_sha256(manifest)
        manifest["run_id"] = manifest["evidence_sha256"][:16]
        return manifest

    def test_requires_documented_top_level_schema_and_valid_values(self):
        module = load_module()
        base = self._minimal_valid_manifest(module)
        self.assertEqual(module.validate_manifest(base), [])
        mutations = [
            ("missing schema version", lambda m: m.pop("schema_version")),
            ("unsupported schema version", lambda m: m.__setitem__("schema_version", 2)),
            ("invalid journal date", lambda m: m.__setitem__("journal_date", "July 25")),
            ("invalid created timestamp", lambda m: m.__setitem__("created_at", "yesterday")),
            ("invalid timestamp window", lambda m: m.__setitem__("window", {"start_ts": 200.0, "end_ts": 100.0})),
        ]
        for label, mutate in mutations:
            manifest = json.loads(json.dumps(base))
            mutate(manifest)
            manifest["evidence_sha256"] = module.canonical_evidence_sha256(manifest)
            manifest["run_id"] = manifest["evidence_sha256"][:16]
            with self.subTest(label=label):
                self.assertTrue(module.validate_manifest(manifest))

    def test_schema_two_requires_timezone_and_exact_local_day_window(self):
        module = load_module()
        manifest = self._schema_two_manifest(module)
        self.assertEqual(module.validate_manifest(manifest), [])

        broken = json.loads(json.dumps(manifest))
        broken["window"]["start_ts"] += 1
        broken["evidence_sha256"] = module.canonical_evidence_sha256(broken)
        broken["run_id"] = broken["evidence_sha256"][:16]
        self.assertIn(
            "manifest window does not exactly match journal_date in timezone",
            module.validate_manifest(broken),
        )

    def test_schema_two_rejects_incorrect_retained_character_count(self):
        module = load_module()
        manifest = self._schema_two_manifest(module)
        manifest["coverage"]["retained_char_count"] = 1
        manifest["evidence_sha256"] = module.canonical_evidence_sha256(manifest)
        manifest["run_id"] = manifest["evidence_sha256"][:16]

        self.assertIn(
            "retained character count does not match manifest sessions",
            module.validate_manifest(manifest),
        )

    def test_schema_two_rejects_values_forbidden_by_privacy_policy(self):
        module = load_module()
        manifest = self._schema_two_manifest(module)
        manifest["policy"]["pii_mode"] = "mask"
        manifest["policy"]["entropy_mode"] = "report"
        manifest["databases"] = [{
            "profile": "default",
            "path": "alice.private@example.com",
            "status": "error",
            "error": "synthetic",
        }]
        manifest["evidence_sha256"] = module.canonical_evidence_sha256(manifest)
        manifest["run_id"] = manifest["evidence_sha256"][:16]

        self.assertIn(
            "manifest contains values forbidden by its privacy policy",
            module.validate_manifest(manifest),
        )

    def test_schema_two_requires_valid_delivery_index(self):
        module = load_module()
        manifest = self._schema_two_manifest(module)
        manifest.pop("delivery")

        self.assertIn("schema 2 delivery is required", module.validate_manifest(manifest))

    def test_requires_documented_session_and_message_fields(self):
        module = load_module()
        manifest = self._minimal_valid_manifest(module)
        session = {
            "profile": "default",
            "platform": "discord",
            "session_id": "session1",
            "title": "Title",
            "started_at": 100.0,
            "chat_id": None,
            "thread_id": None,
            "display_name": "",
            "coverage_ref": "a" * 64,
            "context_label": "unclassified",
            "messages": [{
                "message_id": 1,
                "role": "user",
                "timestamp": 150.0,
                "content": "hello",
                "tool_name": None,
                "tool_calls": None,
            }],
        }
        manifest["sessions"] = [session]
        manifest["databases"] = [{
            "profile": "default",
            "path": "/portable/state.db",
            "status": "ok",
            "message_count": 1,
            "session_count": 1,
        }]
        manifest["coverage"].update({
            "database_count": 1,
            "session_count": 1,
            "message_count": 1,
            "retained_message_count": 1,
            "platforms": ["discord"],
            "profiles": ["default"],
        })
        manifest["evidence_sha256"] = module.canonical_evidence_sha256(manifest)
        manifest["run_id"] = manifest["evidence_sha256"][:16]
        broken = json.loads(json.dumps(manifest))
        broken["sessions"][0].pop("session_id")
        broken["sessions"][0]["messages"][0].pop("content")
        broken["evidence_sha256"] = module.canonical_evidence_sha256(broken)
        broken["run_id"] = broken["evidence_sha256"][:16]
        errors = module.validate_manifest(broken)
        self.assertTrue(any("session field" in error for error in errors))
        self.assertTrue(any("message field" in error for error in errors))

    def test_unhashable_enum_types_return_errors_without_exception(self):
        module = load_module()
        manifest = self._minimal_valid_manifest(module)
        manifest["databases"] = [{
            "profile": "default",
            "path": "/portable/state.db",
            "status": {},
            "message_count": 0,
            "session_count": 0,
        }]
        manifest["coverage"]["database_count"] = 1
        manifest["sessions"] = [{
            "profile": "default",
            "platform": "discord",
            "session_id": "session1",
            "title": "Title",
            "started_at": None,
            "chat_id": None,
            "thread_id": None,
            "display_name": "",
            "coverage_ref": module.hashlib.sha256(b"default\0session1").hexdigest(),
            "context_label": [],
            "messages": [],
        }]
        manifest["coverage"].update({
            "session_count": 1,
            "platforms": ["discord"],
            "profiles": ["default"],
        })
        manifest["evidence_sha256"] = module.canonical_evidence_sha256(manifest)
        manifest["run_id"] = manifest["evidence_sha256"][:16]
        errors = module.validate_manifest(manifest)
        self.assertIn("database status must be ok or error", errors)
        self.assertIn("session field context_label is invalid", errors)

    def test_validate_and_commit_rejects_external_digest_symlink_without_state(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = self._minimal_valid_manifest(module)
            manifest_path = root / "manifest.json"
            note_path = root / "note.md"
            state_path = root / "state.json"
            digest_dir = root / "runs" / manifest["run_id"]
            digest_dir.mkdir(parents=True)
            external = root / "external.md"
            external.write_text(
                f"Digest Run ID: {manifest['run_id']}\n"
                f"Digest Evidence SHA256: {manifest['evidence_sha256']}\n",
                encoding="utf-8",
            )
            (digest_dir / "evil.md").symlink_to(external)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            headings = "\n".join(
                f"## {heading}\nNone recorded.\n" for heading in module.REQUIRED_HEADINGS
            )
            note_path.write_text(
                f"# My Journal: {manifest['journal_date']}\n{headings}"
                f"Run ID: {manifest['run_id']}\n"
                f"Evidence SHA256: {manifest['evidence_sha256']}\n"
                "Databases: 0\nDatabase Errors: 0\nSessions: 0\nMessages: 0\n"
                "Platforms: \nProfiles: \n"
                f"Evidence manifest: {manifest_path}\nDigest directory: {digest_dir}\n",
                encoding="utf-8",
            )
            result = module.validate_and_commit(
                manifest_path, note_path, state_path, digest_dir
            )
            self.assertFalse(result["valid"])
            self.assertTrue(any("digest file" in error for error in result["errors"]))
            self.assertFalse(state_path.exists())

    def test_rejects_broad_unredacted_secret_families(self):
        module = load_module()
        manifest = {
            "run_id": "run-secret",
            "journal_date": "2026-07-25",
            "coverage": {"database_count": 0, "session_count": 0, "message_count": 0},
        }
        secret_values = [
            "Authorization: Basic " + "dXNlcjpwYXNzd29yZA==",
            'password="' + "two words secret" + '"',
            "ghp_" + "A" * 36,
            "AKIA" + "B" * 16,
            "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiIxMjM0NTY3ODkwIn0" + "." + "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        ]

        for secret in secret_values:
            errors = module.validate_note(manifest, "# My Journal: 2026-07-25\n" + secret)
            self.assertIn("note contains a likely unredacted secret", errors)

    def test_invalid_manifest_json_returns_failure_without_state(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "manifest.json"
            note_path = root / "note.md"
            state_path = root / "state.json"
            manifest_path.write_text("{invalid", encoding="utf-8")
            note_path.write_text("# My Journal: 2026-07-25\n", encoding="utf-8")

            result = module.validate_and_commit(manifest_path, note_path, state_path)

            self.assertFalse(result["valid"])
            self.assertIn("manifest is not valid JSON", result["errors"])
            self.assertFalse(state_path.exists())

    def test_malformed_manifest_shapes_return_errors_without_exceptions(self):
        module = load_module()
        malformed = [
            {"coverage": [], "databases": [], "sessions": []},
            {"coverage": {}, "databases": [], "sessions": {"bad": "shape"}},
            {"coverage": {"database_error_count": "many"}, "databases": [], "sessions": []},
        ]

        for manifest in malformed:
            errors = module.validate_manifest(manifest)
            note_errors = module.validate_note(manifest, "")
            self.assertTrue(errors)
            self.assertTrue(note_errors)

    def test_rejects_inconsistent_message_counts(self):
        module = load_module()
        manifest = {
            "journal_date": "2026-07-25",
            "window": {"start_ts": 1.0, "end_ts": 2.0},
            "databases": [{"profile": "default", "status": "ok", "message_count": 1, "session_count": 1}],
            "sessions": [{
                "profile": "default",
                "platform": "discord",
                "coverage_ref": "ref-one",
                "messages": [{"message_id": 1}],
            }],
            "coverage": {
                "database_count": 1,
                "database_error_count": 0,
                "session_count": 1,
                "message_count": 999,
                "retained_message_count": 888,
                "platforms": ["discord"],
                "profiles": ["default"],
            },
        }
        manifest["evidence_sha256"] = module.canonical_evidence_sha256(manifest)
        manifest["run_id"] = manifest["evidence_sha256"][:16]

        errors = module.validate_manifest(manifest)

        self.assertIn("message count does not match database reports", errors)
        self.assertIn("retained message count does not match manifest sessions", errors)

    def test_schema_two_digest_receipts_match_delivery_index(self):
        module = load_module()
        manifest = self._schema_two_manifest(module)
        chunk = manifest["delivery"]["chunks"][0]
        body = "A bounded summary.\n"
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        receipt = "\n".join([
            "Digest Schema: 2",
            f"Run ID: {manifest['run_id']}",
            f"Evidence SHA256: {manifest['evidence_sha256']}",
            "Chunk Index: 1/1",
            f"Chunk ID: {chunk['chunk_id']}",
            f"Chunk File SHA256: {chunk['sha256']}",
            f"Digest Body SHA256: {body_hash}",
            "---",
            body,
        ])

        self.assertEqual(module.validate_digest_bindings(manifest, [receipt]), [])
        tampered = receipt.replace(chunk["chunk_id"], "f" * 64)
        self.assertIn(
            "digest 1 chunk identity does not match delivery index",
            module.validate_digest_bindings(manifest, [tampered]),
        )

    def test_digest_must_match_current_run_and_evidence_hash(self):
        module = load_module()
        manifest = {"run_id": "new-run", "evidence_sha256": "a" * 64}
        stale_digest = "Digest Run ID: old-run\nDigest Evidence SHA256: " + "b" * 64

        errors = module.validate_digest_bindings(manifest, [stale_digest])

        self.assertIn("digest 1 does not contain the current run ID", errors)
        self.assertIn("digest 1 does not contain the current evidence SHA256", errors)

    def test_rejects_manifest_hash_and_coverage_inconsistency(self):
        module = load_module()
        manifest = {
            "run_id": "wrong-run",
            "evidence_sha256": "d" * 64,
            "journal_date": "2026-07-25",
            "window": {"start_ts": 1.0, "end_ts": 2.0},
            "databases": [{"profile": "default", "status": "ok", "message_count": 1, "session_count": 1}],
            "sessions": [
                {"profile": "default", "platform": "discord", "coverage_ref": "same"},
                {"profile": "writer", "platform": "telegram", "coverage_ref": "same"},
            ],
            "coverage": {
                "database_count": 2,
                "database_error_count": 0,
                "session_count": 1,
                "message_count": 2,
                "retained_message_count": 2,
                "platforms": ["discord"],
                "profiles": ["default"],
            },
        }

        errors = module.validate_manifest(manifest)

        self.assertIn("evidence SHA256 does not match manifest content", errors)
        self.assertIn("run ID does not match the evidence SHA256", errors)
        self.assertIn("session count does not match manifest sessions", errors)
        self.assertIn("database count does not match database reports", errors)
        self.assertIn("platform coverage does not match manifest sessions", errors)
        self.assertIn("profile coverage does not match manifest sessions", errors)
        self.assertIn("session coverage references are missing or duplicated", errors)

    def test_duplicate_session_reference_refuses_completion(self):
        module = load_module()
        manifest = {
            "run_id": "run-duplicate",
            "journal_date": "2026-07-25",
            "sessions": [{"coverage_ref": "ref-one"}],
            "coverage": {"database_count": 0, "database_error_count": 0, "session_count": 1, "message_count": 0},
        }

        errors = module.validate_note(
            manifest,
            "",
            "Session Ref: ref-one\nSession Ref: ref-one\n",
        )

        self.assertIn("session coverage references do not exactly match the manifest", errors)

    def test_requires_complete_provenance_and_zero_database_errors(self):
        module = load_module()
        manifest = {
            "run_id": "run-meta",
            "journal_date": "2026-07-25",
            "evidence_sha256": "c" * 64,
            "sessions": [],
            "coverage": {
                "database_count": 2,
                "database_error_count": 1,
                "session_count": 0,
                "message_count": 0,
                "platforms": ["discord", "telegram"],
                "profiles": ["default", "writer"],
            },
        }
        headings = "\n".join(f"## {heading}\nNone.\n" for heading in module.REQUIRED_HEADINGS)
        note = (
            "# My Journal: 2026-07-25\n" + headings +
            "Run ID: run-meta\nDatabases: 2\nSessions: 0\nMessages: 0\n"
        )

        errors = module.validate_note(manifest, note, manifest_path=Path("/tmp/manifest.json"), digest_dir=Path("/tmp/digests"))

        self.assertIn("manifest reports database errors", errors)
        self.assertIn("provenance does not contain the evidence SHA256", errors)
        self.assertIn("provenance does not contain exact platforms", errors)
        self.assertIn("provenance does not contain exact profiles", errors)
        self.assertIn("provenance does not contain the evidence manifest path", errors)
        self.assertIn("provenance does not contain the digest directory", errors)

    def test_rejects_count_substring_false_positive(self):
        module = load_module()
        manifest = {
            "run_id": "run-count",
            "journal_date": "2026-07-25",
            "coverage": {"database_count": 1, "session_count": 1, "message_count": 1},
        }
        headings = "\n".join(f"## {heading}\nNone.\n" for heading in module.REQUIRED_HEADINGS)
        note = (
            "# My Journal: 2026-07-25\n" + headings +
            "Run ID: run-count\nDatabases: 10\nSessions: 10\nMessages: 10\n"
        )

        errors = module.validate_note(manifest, note)

        self.assertIn("provenance does not contain exact databases coverage", errors)
        self.assertIn("provenance does not contain exact sessions coverage", errors)
        self.assertIn("provenance does not contain exact messages coverage", errors)

    def test_headings_and_counts_without_session_references_refuse_completion(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "manifest.json"
            note_path = root / "note.md"
            state_path = root / "state.json"
            sessions = [
                {"coverage_ref": f"ref{i:03d}", "session_id": f"session{i:03d}", "profile": "default"}
                for i in range(1, 100)
            ]
            manifest = {
                "run_id": "run-no-coverage",
                "journal_date": "2026-07-25",
                "evidence_sha256": "a" * 64,
                "sessions": sessions,
                "coverage": {
                    "database_count": 2,
                    "database_error_count": 0,
                    "session_count": 99,
                    "message_count": 500,
                    "platforms": ["discord"],
                    "profiles": ["default"],
                },
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            headings = "\n".join(f"## {heading}\nNone recorded.\n" for heading in module.REQUIRED_HEADINGS)
            note_path.write_text(
                "# My Journal: 2026-07-25\n" + headings +
                "Run ID: run-no-coverage\nDatabases: 2\nSessions: 99\nMessages: 500\n",
                encoding="utf-8",
            )

            result = module.validate_and_commit(manifest_path, note_path, state_path)

            self.assertFalse(result["valid"])
            self.assertFalse(state_path.exists())
            self.assertTrue(any("session coverage" in error for error in result["errors"]))

    def test_valid_note_commits_completed_state(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "manifest.json"
            note_path = root / "note.md"
            state_path = root / "state.json"
            digest_dir = root / "digests"
            digest_dir.mkdir()
            sessions = []
            for session_id, profile, platform in (
                ("session1", "default", "discord"),
                ("session2", "writer", "telegram"),
                ("session3", "default", "discord"),
            ):
                sessions.append({
                    "coverage_ref": module.hashlib.sha256(
                        f"{profile}\0{session_id}".encode("utf-8")
                    ).hexdigest(),
                    "session_id": session_id,
                    "profile": profile,
                    "platform": platform,
                    "title": "Fixture session",
                    "started_at": 100.0,
                    "chat_id": None,
                    "thread_id": None,
                    "display_name": "",
                    "context_label": "unclassified",
                    "messages": [],
                })
            manifest = {
                "schema_version": 1,
                "journal_date": "2026-07-25",
                "created_at": "2026-07-26T00:00:00+00:00",
                "window": {"start_ts": 100.0, "end_ts": 200.0},
                "databases": [
                    {"profile": "default", "path": "/portable/default.db", "status": "ok", "message_count": 6, "session_count": 2},
                    {"profile": "writer", "path": "/portable/writer.db", "status": "ok", "message_count": 4, "session_count": 1},
                ],
                "sessions": sessions,
                "coverage": {
                    "database_count": 2,
                    "database_error_count": 0,
                    "session_count": 3,
                    "message_count": 10,
                    "retained_message_count": 0,
                    "platforms": ["discord", "telegram"],
                    "profiles": ["default", "writer"],
                },
            }
            manifest["evidence_sha256"] = module.canonical_evidence_sha256(manifest)
            manifest["run_id"] = manifest["evidence_sha256"][:16]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            headings = [
                "Overview",
                "Conversation Coverage",
                "Projects and Workstreams",
                "Decisions",
                "Changes and Verification",
                "Completed Work",
                "Blockers and Failures",
                "Corrections and Preference Changes",
                "Open Threads",
                "Context Index",
                "Automation Appendix",
                "Provenance",
            ]
            note = ["# My Journal: 2026-07-25", ""]
            for heading in headings:
                note.extend([f"## {heading}", "None recorded.", ""])
            note.extend([
                f"Run ID: {manifest['run_id']}",
                f"Evidence SHA256: {manifest['evidence_sha256']}",
                "Databases: 2",
                "Database Errors: 0",
                "Sessions: 3",
                "Messages: 10",
                "Platforms: discord, telegram",
                "Profiles: default, writer",
                f"Evidence manifest: {manifest_path}",
                f"Digest directory: {digest_dir}",
            ])
            note_path.write_text("\n".join(note), encoding="utf-8")
            (digest_dir / "group-one.md").write_text(
                f"# Group one\nDigest Run ID: {manifest['run_id']}\n"
                f"Digest Evidence SHA256: {manifest['evidence_sha256']}\n"
                + "".join(f"Session Ref: {session['coverage_ref']}\n" for session in sessions),
                encoding="utf-8",
            )

            result = module.validate_and_commit(manifest_path, note_path, state_path, digest_dir)

            self.assertTrue(result["valid"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["run_id"], manifest["run_id"])

    def test_missing_session_coverage_refuses_commit(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = root / "manifest.json"
            note_path = root / "note.md"
            state_path = root / "state.json"
            manifest_path.write_text(json.dumps({
                "run_id": "run456",
                "journal_date": "2026-07-25",
                "coverage": {"database_count": 1, "session_count": 4, "message_count": 8},
            }), encoding="utf-8")
            note_path.write_text("# My Journal: 2026-07-25\n\n## Overview\nIncomplete\nRun ID: run456\n", encoding="utf-8")

            result = module.validate_and_commit(manifest_path, note_path, state_path)

            self.assertFalse(result["valid"])
            self.assertFalse(state_path.exists())
            self.assertTrue(result["errors"])


if __name__ == "__main__":
    unittest.main()
