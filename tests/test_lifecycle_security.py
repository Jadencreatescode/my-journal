from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_installer():
    spec = importlib.util.spec_from_file_location("journal_lifecycle_security", ROOT / "install.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReceiptBoundLifecycleTests(unittest.TestCase):
    def test_lifecycle_guard_refuses_pending_owned_job_without_receipt(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            journal = home / "journal"
            journal.mkdir()
            (journal / "cron-job-intent.json").write_text(json.dumps({
                "schema_version": 1,
                "ownership_token": "c" * 48,
                "job_spec": {"schedule": "every 1h", "deliver": "local"},
                "normalized_schedule": {"kind": "interval", "seconds": 3600},
                "pending_job_id": "leaked",
            }))
            with self.assertRaisesRegex(ValueError, "cron-remove"):
                installer._assert_no_receipt_bound_cron(home)

    def test_lifecycle_guard_rejects_dangling_receipt_symlink(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            journal = home / "journal"
            journal.mkdir()
            (journal / "cron-job.json").symlink_to(journal / "missing-receipt.json")
            with self.assertRaisesRegex(ValueError, "unsafe"):
                installer._assert_no_receipt_bound_cron(home)

    def test_lifecycle_guard_rejects_dangling_runtime_config_symlink(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            metadata = home / ".my-journal"
            metadata.mkdir()
            (metadata / "wsl-runtime.json").symlink_to(metadata / "missing.json")
            with self.assertRaisesRegex(ValueError, "unsafe"):
                installer._assert_no_receipt_bound_cron(home)

    def test_upgrade_refuses_receipt_bound_job(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            installer.install(ROOT, home, upgrade=False)
            journal = home / "journal"
            journal.mkdir()
            (journal / "cron-job.json").write_text(
                json.dumps({"schema_version": 1, "job_id": "owned"}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "cron-remove"):
                installer.install(ROOT, home, upgrade=True)

    def test_lifecycle_guard_follows_persisted_canonical_wsl_home(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            windows_home = root / "windows-home"
            canonical_home = root / "canonical" / ".hermes"
            (windows_home / ".my-journal").mkdir(parents=True)
            (canonical_home / "journal").mkdir(parents=True)
            (windows_home / ".my-journal" / "wsl-runtime.json").write_text(
                json.dumps({"schema_version": 1, "wsl_hermes_home": str(canonical_home)}),
                encoding="utf-8",
            )
            (canonical_home / "journal" / "cron-job.json").write_text(
                json.dumps({"schema_version": 1, "job_id": "owned"}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "cron-remove"):
                installer._assert_no_receipt_bound_cron(windows_home)

    def test_lifecycle_guard_rejects_malformed_wsl_runtime_binding(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".my-journal").mkdir()
            (home / ".my-journal" / "wsl-runtime.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "WSL runtime"):
                installer._assert_no_receipt_bound_cron(home)

    def test_lifecycle_guard_refuses_receipt_bound_job(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            journal = home / "journal"
            journal.mkdir()
            (journal / "cron-job.json").write_text(json.dumps({"schema_version": 1, "job_id": "owned"}))
            with self.assertRaisesRegex(ValueError, "cron-remove"):
                installer._assert_no_receipt_bound_cron(home)

    def test_lifecycle_guard_allows_absent_receipt(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            installer._assert_no_receipt_bound_cron(Path(tmp))

    def test_lifecycle_guard_refuses_structurally_malformed_receipts(self):
        installer = load_installer()
        for value in ({}, [], {"job_id": 1}):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                journal = Path(tmp) / "journal"
                journal.mkdir()
                (journal / "cron-job.json").write_text(json.dumps(value))
                with self.assertRaisesRegex(ValueError, "cron-remove"):
                    installer._assert_no_receipt_bound_cron(Path(tmp))


if __name__ == "__main__":
    unittest.main()
