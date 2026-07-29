from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_core():
    path = ROOT / "core.py"
    spec = importlib.util.spec_from_file_location("journal_plugin_core", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DateRangeTests(unittest.TestCase):
    def setUp(self):
        self.core = load_core()
        self.now = datetime(2026, 7, 26, 15, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

    def test_last_week_uses_complete_monday_through_sunday(self):
        start, end = self.core.resolve_date_range("last week", now=self.now)
        self.assertEqual(str(start), "2026-07-13")
        self.assertEqual(str(end), "2026-07-19")

    def test_last_month_uses_complete_calendar_month(self):
        start, end = self.core.resolve_date_range("last month", now=self.now)
        self.assertEqual(str(start), "2026-06-01")
        self.assertEqual(str(end), "2026-06-30")

    def test_since_iso_date_ends_today(self):
        start, end = self.core.resolve_date_range("since 2026-07-01", now=self.now)
        self.assertEqual(str(start), "2026-07-01")
        self.assertEqual(str(end), "2026-07-26")

    def test_since_weekday_uses_current_or_previous_weekday(self):
        start, end = self.core.resolve_date_range("since Monday", now=self.now)
        self.assertEqual(str(start), "2026-07-20")
        self.assertEqual(str(end), "2026-07-26")

    def test_last_seven_days_is_inclusive(self):
        start, end = self.core.resolve_date_range("last 7 days", now=self.now)
        self.assertEqual(str(start), "2026-07-20")
        self.assertEqual(str(end), "2026-07-26")

    def test_explicit_range_is_ordered(self):
        start, end = self.core.resolve_date_range(
            "2026-07-01 to 2026-07-15", now=self.now
        )
        self.assertEqual(str(start), "2026-07-01")
        self.assertEqual(str(end), "2026-07-15")

    def test_reversed_range_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "start date must not follow end date"):
            self.core.resolve_date_range(
                "2026-07-15 to 2026-07-01", now=self.now
            )


class EntryDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.core = load_core()

    def test_root_symlink_validation_uses_shared_canonical_path(self):
        requested = Path("/var/folders/example/journal")
        canonical = Path("/private/var/folders/example/journal")

        def fake_lstat(path):
            mode = 0o120777 if str(path) == "/var" else 0o040755
            return os.stat_result((mode, 0, 0, 0, 0, 0, 0, 0, 0, 0))

        with mock.patch.object(
            self.core._safe_files,
            "_canonical_descriptor_path",
            return_value=canonical,
        ) as canonicalize, mock.patch.object(Path, "lstat", fake_lstat):
            self.core.reject_symlink_components(requested)

        canonicalize.assert_called_once_with(requested)

    def test_validator_source_remains_anchored_during_directory_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            trusted = base / "trusted"
            trusted.mkdir()
            validator = trusted / "validator.py"
            validator.write_text("VALUE = 'safe'\n", encoding="utf-8")
            external = base / "external"
            external.mkdir()
            marker = base / "executed.txt"
            (external / "validator.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('evil')\nVALUE = 'evil'\n",
                encoding="utf-8",
            )
            real_open = os.open
            swapped = False

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if path == validator.name and dir_fd is not None and not swapped:
                    swapped = True
                    trusted.rename(base / "trusted-original")
                    trusted.symlink_to(external, target_is_directory=True)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch.object(self.core._safe_files.os, "open", side_effect=racing_open):
                loaded = self.core._load_validator_module(validator)

            self.assertTrue(swapped)
            self.assertEqual(loaded.VALUE, "safe")
            self.assertFalse(marker.exists())

    def test_symlinked_entry_outside_notes_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "journal"
            outside = Path(tmp) / "2026-07-20.md"
            outside.write_text("# outside\n", encoding="utf-8")
            linked = root / "notes" / "2026" / "07" / "2026-07-20.md"
            linked.parent.mkdir(parents=True)
            linked.symlink_to(outside)
            self.assertEqual(self.core.discover_entries(root), [])

    def test_symlinked_notes_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "journal"
            outside = Path(tmp) / "external-notes"
            note = outside / "2026" / "07" / "2026-07-20.md"
            note.parent.mkdir(parents=True)
            note.write_text("# My Journal: 2026-07-20\n", encoding="utf-8")
            root.mkdir()
            (root / "notes").symlink_to(outside, target_is_directory=True)

            self.assertEqual(self.core.discover_entries(root), [])

    def test_journal_root_with_symlinked_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            trusted = base / "trusted"
            external = base / "external"
            trusted.mkdir()
            note = external / "journal" / "notes" / "2026" / "07" / "2026-07-25.md"
            note.parent.mkdir(parents=True)
            note.write_text("# My Journal: 2026-07-25\n", encoding="utf-8")
            (trusted / "link").symlink_to(external, target_is_directory=True)

            with self.assertRaisesRegex(self.core.UnsafePathError, "symlink"):
                self.core.discover_entries(trusted / "link" / "journal")

    def test_noncanonical_date_path_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "notes" / "WRONG" / "YEAR" / "2026-07-20.md"
            path.parent.mkdir(parents=True)
            path.write_text("# My Journal: 2026-07-20\n", encoding="utf-8")
            self.assertEqual(self.core.discover_entries(root), [])

    def test_status_discovers_entries_and_calendar_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for day in ("2026-07-20", "2026-07-22"):
                year, month, _ = day.split("-")
                path = root / "notes" / year / month / f"{day}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"# My Journal: {day}\n", encoding="utf-8")

            status = self.core.journal_status(root)

            self.assertEqual(status["entry_count"], 2)
            self.assertEqual(status["earliest_entry"], "2026-07-20")
            self.assertEqual(status["latest_entry"], "2026-07-22")
            self.assertEqual(status["calendar_gaps"], ["2026-07-21"])
            self.assertEqual(
                [item["date"] for item in status["entries"]],
                ["2026-07-20", "2026-07-22"],
            )

    def test_non_date_markdown_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / "notes"
            notes.mkdir(parents=True)
            (notes / "README.md").write_text("ignore", encoding="utf-8")
            self.assertEqual(self.core.journal_status(root)["entry_count"], 0)

    def test_default_validation_uses_manifest_digest_and_note_checks_without_state_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "evidence" / "2026" / "07" / "entry.json"
            digests = root / "runs" / "abcdef0123456789"
            note = root / "notes" / "2026" / "07" / "2026-07-20.md"
            manifest.parent.mkdir(parents=True)
            digests.mkdir(parents=True)
            note.parent.mkdir(parents=True)
            manifest.write_text(
                '{"ok": true, "journal_date": "2026-07-20"}', encoding="utf-8"
            )
            (digests / "one.md").write_text("digest marker", encoding="utf-8")
            note.write_text(
                "# My Journal: 2026-07-20\n\n"
                f"Evidence manifest: {manifest}\n\n"
                f"Digest directory: {digests}\n",
                encoding="utf-8",
            )
            fake = root / "fake_validator.py"
            fake.write_text(
                "def validate_manifest(manifest):\n"
                "    return [] if manifest.get('ok') is True else ['bad manifest']\n"
                "def validate_digest_bindings(manifest, digests):\n"
                "    return [] if digests == ['digest marker'] else ['bad digest']\n"
                "def validate_note(manifest, note, session_evidence='', manifest_path=None, digest_dir=None):\n"
                "    return [] if manifest.get('ok') is True and '# My Journal' in note and 'digest marker' in session_evidence else ['bad note']\n",
                encoding="utf-8",
            )

            valid, errors = self.core.validate_entry(
                note, root=root, validator_path=fake
            )

            self.assertTrue(valid)
            self.assertEqual(errors, [])
            self.assertFalse((root / "state").exists())

    def test_validated_note_snapshot_survives_parent_symlink_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "journal"
            manifest = root / "evidence" / "2026" / "07" / "entry.json"
            digests = root / "runs" / "abcdef0123456789"
            note = root / "notes" / "2026" / "07" / "2026-07-20.md"
            manifest.parent.mkdir(parents=True)
            digests.mkdir(parents=True)
            note.parent.mkdir(parents=True)
            manifest.write_text('{"ok": true, "journal_date": "2026-07-20"}', encoding="utf-8")
            (digests / "one.md").write_text("digest marker", encoding="utf-8")
            safe_text = (
                "# My Journal: 2026-07-20\n\n"
                f"Evidence manifest: {manifest}\n\nDigest directory: {digests}\n"
            )
            note.write_text(safe_text, encoding="utf-8")
            external_parent = Path(tmp) / "external"
            external_parent.mkdir()
            (external_parent / note.name).write_text("evil", encoding="utf-8")
            fake = Path(tmp) / "fake_validator.py"
            fake.write_text(
                "def validate_manifest(manifest): return []\n"
                "def validate_digest_bindings(manifest, digests): return []\n"
                "def validate_note(manifest, note, **kwargs): return []\n",
                encoding="utf-8",
            )
            real_open = os.open
            swapped = False

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if path == note.name and dir_fd is not None and not swapped:
                    swapped = True
                    note.parent.rename(note.parent.with_name("07-original"))
                    note.parent.symlink_to(external_parent, target_is_directory=True)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch.object(self.core._safe_files.os, "open", side_effect=racing_open):
                valid, errors, content = self.core.validate_entry_content(
                    note, root=root, validator_path=fake
                )

            self.assertTrue(swapped)
            self.assertTrue(valid, errors)
            self.assertEqual(content, safe_text)

    def test_validation_rejects_manifest_date_that_differs_from_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "evidence" / "2026" / "07" / "entry.json"
            digests = root / "runs" / "abcdef0123456789"
            note = root / "notes" / "2026" / "07" / "2026-07-20.md"
            manifest.parent.mkdir(parents=True)
            digests.mkdir(parents=True)
            note.parent.mkdir(parents=True)
            manifest.write_text(
                '{"ok": true, "journal_date": "2026-07-25"}', encoding="utf-8"
            )
            (digests / "one.md").write_text("digest marker", encoding="utf-8")
            note.write_text(
                "# My Journal: 2026-07-25\n\n"
                f"Evidence manifest: {manifest}\n\nDigest directory: {digests}\n",
                encoding="utf-8",
            )
            fake = root / "fake_validator.py"
            fake.write_text(
                "def validate_manifest(manifest): return []\n"
                "def validate_digest_bindings(manifest, digests): return []\n"
                "def validate_note(manifest, note, **kwargs): return []\n",
                encoding="utf-8",
            )
            valid, errors = self.core.validate_entry(note, root=root, validator_path=fake)
            self.assertFalse(valid)
            self.assertIn("manifest journal date does not match note filename", errors)

    def test_digest_symlink_outside_run_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "journal"
            external = Path(tmp) / "external.md"
            external.write_text("digest marker", encoding="utf-8")
            manifest = root / "evidence" / "2026" / "07" / "entry.json"
            digests = root / "runs" / "abcdef0123456789"
            note = root / "notes" / "2026" / "07" / "2026-07-20.md"
            manifest.parent.mkdir(parents=True)
            digests.mkdir(parents=True)
            note.parent.mkdir(parents=True)
            manifest.write_text(
                '{"ok": true, "journal_date": "2026-07-20"}', encoding="utf-8"
            )
            (digests / "one.md").symlink_to(external)
            note.write_text(
                "# My Journal: 2026-07-20\n\n"
                f"Evidence manifest: {manifest}\n\nDigest directory: {digests}\n",
                encoding="utf-8",
            )
            fake = root / "fake_validator.py"
            fake.write_text(
                "def validate_manifest(manifest): return []\n"
                "def validate_digest_bindings(manifest, digests): return []\n"
                "def validate_note(manifest, note, **kwargs): return []\n",
                encoding="utf-8",
            )
            valid, errors = self.core.validate_entry(note, root=root, validator_path=fake)
            self.assertFalse(valid)
            self.assertTrue(any("digest file" in error for error in errors))

    def test_status_caps_gap_and_entry_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for day in ("1000-01-01", "2000-01-01"):
                year, month, _ = day.split("-")
                path = root / "notes" / year / month / f"{day}.md"
                path.parent.mkdir(parents=True)
                path.write_text("entry", encoding="utf-8")
            status = self.core.journal_status(root, max_entries=1, max_gaps=10)
            self.assertEqual(len(status["entries"]), 1)
            self.assertTrue(status["entries_truncated"])
            self.assertEqual(len(status["calendar_gaps"]), 10)
            self.assertTrue(status["calendar_gaps_truncated"])
            self.assertEqual(status["calendar_gap_count"], 365241)

    def test_default_validation_rejects_provenance_paths_outside_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "notes" / "2026" / "07" / "2026-07-20.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "# My Journal: 2026-07-20\n\n"
                "## Provenance\n\n"
                "Run ID: abcdef0123456789\n\n"
                "Evidence SHA256: " + "a" * 64 + "\n\n"
                "Evidence manifest: /etc/passwd\n\n"
                "Digest directory: /tmp/digests\n",
                encoding="utf-8",
            )
            valid, errors = self.core.validate_entry(path, root=root)
            self.assertFalse(valid)
            self.assertTrue(any("outside journal evidence directory" in error for error in errors))

    def test_symlinked_evidence_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "journal"
            outside = Path(tmp) / "external-evidence"
            manifest = outside / "2026" / "07" / "entry.json"
            digests = root / "runs" / "abcdef0123456789"
            note = root / "notes" / "2026" / "07" / "2026-07-20.md"
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"ok": true, "journal_date": "2026-07-20"}', encoding="utf-8")
            root.mkdir()
            (root / "evidence").symlink_to(outside, target_is_directory=True)
            digests.mkdir(parents=True)
            (digests / "one.md").write_text("digest marker", encoding="utf-8")
            note.parent.mkdir(parents=True)
            note.write_text(
                "# My Journal: 2026-07-20\n\n"
                f"Evidence manifest: {root / 'evidence' / '2026' / '07' / 'entry.json'}\n\n"
                f"Digest directory: {digests}\n",
                encoding="utf-8",
            )
            fake = root / "fake_validator.py"
            fake.write_text(
                "def validate_manifest(manifest): return []\n"
                "def validate_digest_bindings(manifest, digests): return []\n"
                "def validate_note(manifest, note, **kwargs): return []\n",
                encoding="utf-8",
            )

            valid, errors = self.core.validate_entry(note, root=root, validator_path=fake)

            self.assertFalse(valid)
            self.assertTrue(any("evidence root" in error for error in errors))

    def test_symlinked_runs_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "journal"
            outside = Path(tmp) / "external-runs"
            manifest = root / "evidence" / "2026" / "07" / "entry.json"
            digests = outside / "abcdef0123456789"
            note = root / "notes" / "2026" / "07" / "2026-07-20.md"
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"ok": true, "journal_date": "2026-07-20"}', encoding="utf-8")
            digests.mkdir(parents=True)
            (digests / "one.md").write_text("digest marker", encoding="utf-8")
            (root / "runs").symlink_to(outside, target_is_directory=True)
            note.parent.mkdir(parents=True)
            note.write_text(
                "# My Journal: 2026-07-20\n\n"
                f"Evidence manifest: {manifest}\n\n"
                f"Digest directory: {root / 'runs' / 'abcdef0123456789'}\n",
                encoding="utf-8",
            )
            fake = root / "fake_validator.py"
            fake.write_text(
                "def validate_manifest(manifest): return []\n"
                "def validate_digest_bindings(manifest, digests): return []\n"
                "def validate_note(manifest, note, **kwargs): return []\n",
                encoding="utf-8",
            )

            valid, errors = self.core.validate_entry(note, root=root, validator_path=fake)

            self.assertFalse(valid)
            self.assertTrue(any("runs root" in error for error in errors))

    def test_reader_returns_exact_content_from_validation_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "notes" / "2026" / "07" / "2026-07-20.md"
            path.parent.mkdir(parents=True)
            path.write_text("MUTATED-AFTER-VALIDATION", encoding="utf-8")
            original = getattr(self.core, "validate_entry_content", None)
            self.core.validate_entry_content = lambda candidate, root=None: (
                True,
                [],
                "VALIDATED-SNAPSHOT",
            )
            try:
                result = self.core.read_validated_entries(
                    root,
                    self.core.date.fromisoformat("2026-07-20"),
                    self.core.date.fromisoformat("2026-07-20"),
                )
            finally:
                if original is None:
                    delattr(self.core, "validate_entry_content")
                else:
                    self.core.validate_entry_content = original
            self.assertEqual(result["entries"][0]["content"], "VALIDATED-SNAPSHOT")

    def test_reader_caps_rejected_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for offset in range(20):
                day = self.core.date(2026, 7, 1) + self.core.timedelta(days=offset)
                path = root / "notes" / f"{day.year:04d}" / f"{day.month:02d}" / f"{day}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("entry", encoding="utf-8")
            result = self.core.read_validated_entries(
                root,
                self.core.date(2026, 7, 1),
                self.core.date(2026, 7, 20),
                validate_fn=lambda path: (False, ["x" * 5000]),
                max_records=5,
            )
            self.assertEqual(result["rejected_count"], 20)
            self.assertEqual(len(result["rejected"]), 5)
            self.assertTrue(result["rejected_truncated"])
            self.assertLessEqual(len(result["rejected"][0]["errors"][0]), 1000)

    def test_reader_returns_only_validated_entries_in_requested_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for day in ("2026-07-20", "2026-07-21", "2026-07-22"):
                year, month, _ = day.split("-")
                path = root / "notes" / year / month / f"{day}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"# My Journal: {day}\nbody {day}\n", encoding="utf-8")

            def validate(path):
                if path.name == "2026-07-21.md":
                    return False, ["synthetic validation failure"]
                return True, []

            result = self.core.read_validated_entries(
                root,
                self.core.date.fromisoformat("2026-07-20"),
                self.core.date.fromisoformat("2026-07-22"),
                validate_fn=validate,
            )

            self.assertEqual(
                [item["date"] for item in result["entries"]],
                ["2026-07-20", "2026-07-22"],
            )
            self.assertEqual(result["rejected"], [
                {
                    "date": "2026-07-21",
                    "path": str(root / "notes" / "2026" / "07" / "2026-07-21.md"),
                    "errors": ["synthetic validation failure"],
                }
            ])

    def test_custom_validator_cannot_swap_valid_note_for_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "journal"
            note = root / "notes" / "2026" / "07" / "2026-07-27.md"
            note.parent.mkdir(parents=True)
            note.write_text("safe content", encoding="utf-8")
            external = Path(tmp) / "external.md"
            external.write_text("external content", encoding="utf-8")

            def validate(path):
                path.unlink()
                path.symlink_to(external)
                return True, []

            with self.assertRaisesRegex(ValueError, "symlink|unsafe"):
                self.core.read_validated_entries(
                    root,
                    self.core.date.fromisoformat("2026-07-27"),
                    self.core.date.fromisoformat("2026-07-27"),
                    validate_fn=validate,
                )


if __name__ == "__main__":
    unittest.main()
