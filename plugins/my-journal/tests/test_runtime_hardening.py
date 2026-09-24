from __future__ import annotations

import importlib.util
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from multiprocessing import get_context
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_operations():
    package_name = f"journal_runtime_hardening_{os.getpid()}_{time.time_ns()}"
    spec = importlib.util.spec_from_file_location(
        package_name,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return sys.modules[f"{package_name}.operations"]


def hold_generation_lock(root: str, lock_name: str, ready) -> None:
    descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    lock_descriptor = os.open(lock_name, os.O_RDWR | os.O_CREAT, 0o600, dir_fd=descriptor)
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        ready.set()
        while True:
            time.sleep(10)
    finally:
        os.close(lock_descriptor)
        os.close(descriptor)


def write_pending_run(root: Path, journal_date: str, run_id: str) -> dict:
    year, month, _ = journal_date.split("-")
    manifest = root / "evidence" / year / month / f"{journal_date}-{run_id}.json"
    packet_dir = root / "packets" / year / month / f"{journal_date}-{run_id}"
    packet = packet_dir / "chunk-000001.md"
    plan = packet_dir / "plan.json"
    pending = root / "pending" / f"{run_id}.json"
    digest = root / "runs" / run_id / "digests" / "chunk-000001.md"
    for path in (manifest, packet, plan, pending, digest):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n" if path.suffix == ".json" else "safe\n", encoding="utf-8")
    receipt = {
        "run_id": run_id,
        "journal_date": journal_date,
        "manifest_path": str(manifest),
        "packet_path": str(packet),
        "packet_paths": [str(packet)],
        "packet_plan_path": str(plan),
        "status": "pending_note_validation",
    }
    pending.write_text(json.dumps(receipt), encoding="utf-8")
    return {
        "receipt": receipt,
        "manifest": manifest,
        "packet_dir": packet_dir,
        "pending": pending,
        "run_dir": root / "runs" / run_id,
    }


class RuntimeHardeningTests(unittest.TestCase):
    def test_invalid_expected_date_returns_bounded_failure_without_side_effects(self):
        operations = load_operations()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "not-created"
            with mock.patch.object(operations, "journal_root", return_value=root), mock.patch.object(
                operations, "_hermes_executable"
            ) as executable, mock.patch.object(operations.subprocess, "run") as run:
                result = operations.run_generation(
                    "Generate the requested journal date.", expected_dates=["2026-7-2"]
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["exit_code"], 1)
            self.assertIn("canonical ISO date", result["error"])
            self.assertNotIn("Traceback", result["error"])
            self.assertLessEqual(len(result["error"]), 300)
            self.assertFalse(root.exists())
            executable.assert_not_called()
            run.assert_not_called()

    def test_empty_or_duplicate_expected_dates_fail_before_side_effects(self):
        operations = load_operations()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "not-created"
            with mock.patch.object(operations, "journal_root", return_value=root), mock.patch.object(
                operations.subprocess, "run"
            ) as run:
                empty = operations.run_generation("empty", expected_dates=[])
                duplicate = operations.run_generation(
                    "duplicate", expected_dates=["2026-07-27", "2026-07-27"]
                )
            self.assertFalse(empty["ok"])
            self.assertFalse(duplicate["ok"])
            self.assertFalse(root.exists())
            run.assert_not_called()

    def test_bare_intervals_and_one_shot_schedules_never_reach_native_create(self):
        operations = load_operations()
        rejected = ("1h", "30m", "at 2026-07-28 11:00")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            operations, "journal_root", return_value=Path(tmp)
        ), mock.patch.object(operations, "_create_cron_job") as create:
            results = [operations.schedule_create(value, "local") for value in rejected]

        self.assertTrue(all(not result["ok"] for result in results))
        self.assertTrue(all("recurring" in result["error"] for result in results))
        create.assert_not_called()

    def test_native_shape_mismatch_is_rolled_back_and_never_reports_success(self):
        operations = load_operations()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            operations, "journal_root", return_value=Path(tmp)
        ), mock.patch.object(
            operations, "_create_cron_job", return_value={"id": "created-job"}
        ), mock.patch.object(
            operations,
            "_list_cron_jobs",
            return_value=[{"id": "created-job", "name": "provider-mutated"}],
        ) as listed, mock.patch.object(
            operations, "_remove_cron_job", return_value=True
        ) as remove:
            result = operations.schedule_create("0 11 * * *", "local")
            receipt_exists = (Path(tmp) / "cron-job.json").exists()
            intent_exists = (Path(tmp) / "cron-job-intent.json").exists()

        self.assertFalse(result["ok"])
        self.assertIsNone(result["job_id"])
        self.assertIn("did not preserve", result["error"])
        listed.assert_called_once_with(include_disabled=True)
        remove.assert_called_once_with("created-job")
        self.assertFalse(receipt_exists)
        self.assertTrue(intent_exists)

    def test_failed_shape_mismatch_rollback_returns_exact_owned_id_for_recovery(self):
        operations = load_operations()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            operations, "journal_root", return_value=Path(tmp)
        ), mock.patch.object(
            operations, "_create_cron_job", return_value={"id": "recovery-job"}
        ), mock.patch.object(
            operations,
            "_list_cron_jobs",
            return_value=[{"id": "recovery-job", "name": "mutated"}],
        ), mock.patch.object(operations, "_remove_cron_job", return_value=False):
            result = operations.schedule_create("every 1h", "local")
            intent = json.loads((Path(tmp) / "cron-job-intent.json").read_text())
            receipt_exists = (Path(tmp) / "cron-job.json").exists()

        self.assertFalse(result["ok"])
        self.assertEqual(result["job_id"], "recovery-job")
        self.assertIn("rollback", result["error"])
        self.assertEqual(intent["job_spec"]["schedule"], "every 1h")
        self.assertFalse(receipt_exists)

    def test_active_generation_blocks_second_generation_and_purge_then_death_releases_it(self):
        operations = load_operations()
        request = "Generate journal date 2026-07-27."
        lock_identity = json.dumps(["2026-07-27"], separators=(",", ":"), sort_keys=True)
        lock_name = f"generation-{hashlib.sha256(lock_identity.encode()).hexdigest()[:16]}.lock"
        context = get_context("fork")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes").mkdir()
            ready = context.Event()
            process = context.Process(target=hold_generation_lock, args=(tmp, lock_name, ready))
            process.start()
            self.assertTrue(ready.wait(5))
            try:
                with mock.patch.object(operations, "journal_root", return_value=root), mock.patch.object(
                    operations.subprocess, "run"
                ) as run:
                    blocked = operations.run_generation(request, expected_dates=["2026-07-27"])
                    with self.assertRaisesRegex(ValueError, "generation is active"):
                        operations.purge("DELETE MY JOURNAL DATA", apply=True)
                self.assertFalse(blocked["ok"])
                self.assertIn("already running", blocked["error"])
                run.assert_not_called()
            finally:
                process.terminate()
                process.join(5)

            with mock.patch.object(operations, "journal_root", return_value=root):
                result = operations.purge("DELETE MY JOURNAL DATA", apply=True)
            self.assertIn(lock_name, result["removed"])
            self.assertFalse((root / lock_name).exists())

    def test_reset_failed_pending_requires_exact_confirmation_and_removes_only_owned_run(self):
        operations = load_operations()
        journal_date = "2026-06-26"
        run_id = "1f7ce9536c587265"
        confirmation = f"RESET FAILED JOURNAL RUN {journal_date} {run_id}"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = write_pending_run(root, journal_date, run_id)
            unrelated = root / "runs" / "aaaaaaaaaaaaaaaa" / "keep.txt"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("keep", encoding="utf-8")
            with mock.patch.object(operations, "journal_root", return_value=root):
                preview = operations.reset_failed_pending(journal_date, run_id)
                with self.assertRaisesRegex(ValueError, "exact confirmation"):
                    operations.reset_failed_pending(journal_date, run_id, "wrong", apply=True)
                result = operations.reset_failed_pending(
                    journal_date, run_id, confirmation, apply=True
                )

            self.assertTrue(preview["preview"])
            self.assertEqual(result["removed"], result["candidates"])
            self.assertFalse(artifacts["manifest"].exists())
            self.assertFalse(artifacts["packet_dir"].exists())
            self.assertFalse(artifacts["pending"].exists())
            self.assertFalse(artifacts["run_dir"].exists())
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def test_reset_failed_pending_refuses_canonical_or_unowned_paths(self):
        operations = load_operations()
        journal_date = "2026-07-30"
        run_id = "11e948a481a9300c"
        confirmation = f"RESET FAILED JOURNAL RUN {journal_date} {run_id}"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = write_pending_run(root, journal_date, run_id)
            note = root / "notes" / "2026" / "07" / f"{journal_date}.md"
            note.parent.mkdir(parents=True)
            note.write_text("validated", encoding="utf-8")
            with mock.patch.object(operations, "journal_root", return_value=root), mock.patch.object(
                operations, "validated_entry_dates", return_value=[journal_date]
            ):
                with self.assertRaisesRegex(ValueError, "canonical"):
                    operations.reset_failed_pending(
                        journal_date, run_id, confirmation, apply=True
                    )
            note.unlink()
            outside = root / "outside.md"
            outside.write_text("keep", encoding="utf-8")
            receipt = artifacts["receipt"]
            receipt["packet_path"] = str(outside)
            receipt["packet_paths"] = [str(outside)]
            artifacts["pending"].write_text(json.dumps(receipt), encoding="utf-8")
            with mock.patch.object(operations, "journal_root", return_value=root):
                with self.assertRaisesRegex(ValueError, "owned packet paths"):
                    operations.reset_failed_pending(
                        journal_date, run_id, confirmation, apply=True
                    )
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
            self.assertTrue(artifacts["manifest"].exists())

    def test_active_generation_blocks_failed_pending_reset_without_deleting_artifacts(self):
        operations = load_operations()
        journal_date = "2026-06-26"
        run_id = "1f7ce9536c587265"
        confirmation = f"RESET FAILED JOURNAL RUN {journal_date} {run_id}"
        lock_identity = json.dumps([journal_date], separators=(",", ":"), sort_keys=True)
        lock_name = f"generation-{hashlib.sha256(lock_identity.encode()).hexdigest()[:16]}.lock"
        context = get_context("fork")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = write_pending_run(root, journal_date, run_id)
            ready = context.Event()
            process = context.Process(target=hold_generation_lock, args=(tmp, lock_name, ready))
            process.start()
            self.assertTrue(ready.wait(5))
            try:
                with mock.patch.object(operations, "journal_root", return_value=root):
                    with self.assertRaisesRegex(ValueError, "generation is active"):
                        operations.reset_failed_pending(
                            journal_date, run_id, confirmation, apply=True
                        )
                self.assertTrue(artifacts["manifest"].exists())
                self.assertTrue(artifacts["packet_dir"].exists())
                self.assertTrue(artifacts["pending"].exists())
                self.assertTrue(artifacts["run_dir"].exists())
            finally:
                process.terminate()
                process.join(5)

    def test_purge_removes_owned_guided_approval_plans(self):
        operations = load_operations()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plans = root / "approval-plans"
            plans.mkdir()
            (plans / ("a" * 32 + ".json")).write_text("{}", encoding="utf-8")
            size_receipt = root / "database-size-approvals.json"
            size_receipt.write_text("{}", encoding="utf-8")
            workload_receipt = root / "daily-workload-approval.json"
            workload_receipt.write_text("{}", encoding="utf-8")
            with mock.patch.object(operations, "journal_root", return_value=root):
                preview = operations.purge()
                result = operations.purge("DELETE MY JOURNAL DATA", apply=True)
            self.assertIn("approval-plans", preview["candidates"])
            self.assertIn("database-size-approvals.json", preview["candidates"])
            self.assertIn("daily-workload-approval.json", preview["candidates"])
            self.assertIn("approval-plans", result["removed"])
            self.assertIn("database-size-approvals.json", result["removed"])
            self.assertIn("daily-workload-approval.json", result["removed"])
            self.assertFalse(plans.exists())
            self.assertFalse(size_receipt.exists())
            self.assertFalse(workload_receipt.exists())

    def test_purge_removes_stale_regular_generation_lock(self):
        operations = load_operations()
        lock_name = "generation-0123456789abcdef.lock"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / lock_name).write_text("stale", encoding="utf-8")
            with mock.patch.object(operations, "journal_root", return_value=root):
                result = operations.purge("DELETE MY JOURNAL DATA", apply=True)
            self.assertIn(lock_name, result["removed"])
            self.assertFalse((root / lock_name).exists())

    def test_generation_lock_symlink_and_unexpected_type_fail_closed(self):
        operations = load_operations()
        lock_name = "generation-0123456789abcdef.lock"
        for kind in ("symlink", "directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                if kind == "symlink":
                    os.symlink("outside", root / lock_name)
                else:
                    (root / lock_name).mkdir()
                with mock.patch.object(operations, "journal_root", return_value=root):
                    with self.assertRaisesRegex(ValueError, "generation lock is unsafe"):
                        operations.purge("DELETE MY JOURNAL DATA", apply=True)
                self.assertTrue((root / lock_name).exists() or (root / lock_name).is_symlink())

    def test_generation_lock_cleanup_never_unlinks_replacement_inode(self):
        operations = load_operations()
        lock_name = "generation-0123456789abcdef.lock"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / lock_name
            lock_path.write_text("stale", encoding="utf-8")
            real_stat = operations.os.stat
            swapped = False

            def swap_before_identity_check(path, *args, **kwargs):
                nonlocal swapped
                if path == lock_name and kwargs.get("dir_fd") is not None and not swapped:
                    swapped = True
                    lock_path.unlink()
                    lock_path.write_text("replacement", encoding="utf-8")
                return real_stat(path, *args, **kwargs)

            with mock.patch.object(operations, "journal_root", return_value=root), mock.patch.object(
                operations.os, "stat", side_effect=swap_before_identity_check
            ):
                with self.assertRaisesRegex(ValueError, "generation lock changed"):
                    operations.purge("DELETE MY JOURNAL DATA", apply=True)
            self.assertEqual(lock_path.read_text(encoding="utf-8"), "replacement")


if __name__ == "__main__":
    unittest.main()
