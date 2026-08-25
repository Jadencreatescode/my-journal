from __future__ import annotations

import ctypes
import errno
import importlib.util
import fcntl
import hashlib
import json
import os
import stat
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
    root_path = Path(root).resolve()
    descriptor = os.open("/tmp", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    token = hashlib.sha256(os.fsencode(root_path)).hexdigest()[:16]
    stable_name = f".my-journal-{os.getuid()}-{token}.lock"
    lock_descriptor = os.open(stable_name, os.O_RDWR | os.O_CREAT, 0o600, dir_fd=descriptor)
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        ready.set()
        while True:
            time.sleep(10)
    finally:
        os.close(lock_descriptor)
        os.close(descriptor)


GLOBAL_GENERATION_LOCK = "generation.lock"


def stable_lock_path(root: Path) -> Path:
    canonical = root.resolve()
    token = hashlib.sha256(os.fsencode(canonical)).hexdigest()[:16]
    return Path("/tmp") / f".my-journal-{os.getuid()}-{token}.lock"


def reset_intent_path(root: Path, journal_date: str, run_id: str) -> Path:
    return root / f"reset-{journal_date}-{run_id}.json"


def quarantine_paths(root: Path, intent: dict) -> list[Path]:
    return [
        root / target["quarantine_path"]
        if "quarantine_path" in target
        else (root / target["path"]).parent / target["quarantine_name"]
        for target in intent["targets"]
    ]


def write_pending_run(root: Path, journal_date: str, run_id: str) -> dict:
    root = root.resolve()
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
        lock_name = GLOBAL_GENERATION_LOCK
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
            self.assertNotIn(lock_name, result["removed"])
            self.assertTrue(stable_lock_path(root).exists())

    def test_global_lock_blocks_multi_date_generation_and_reset(self):
        operations = load_operations()
        journal_date = "2026-06-26"
        run_id = "1f7ce9536c587265"
        confirmation = f"RESET FAILED JOURNAL RUN {journal_date} {run_id}"
        context = get_context("fork")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = write_pending_run(root, journal_date, run_id)
            ready = context.Event()
            process = context.Process(
                target=hold_generation_lock,
                args=(tmp, GLOBAL_GENERATION_LOCK, ready),
            )
            process.start()
            self.assertTrue(ready.wait(5))
            try:
                with mock.patch.object(operations, "journal_root", return_value=root), mock.patch.object(
                    operations.subprocess, "run"
                ) as run:
                    blocked = operations.run_generation(
                        "Generate two dates.",
                        expected_dates=[journal_date, "2026-06-27"],
                    )
                    with self.assertRaisesRegex(ValueError, "generation is active"):
                        operations.reset_failed_pending(
                            journal_date, run_id, confirmation, apply=True
                        )
                self.assertFalse(blocked["ok"])
                run.assert_not_called()
                self.assertTrue(artifacts["pending"].exists())
            finally:
                process.terminate()
                process.join(5)

    def test_reset_requires_exact_confirmation_and_permanently_quarantines_owned_evidence(self):
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
            original_paths = (
                artifacts["run_dir"], artifacts["packet_dir"],
                artifacts["manifest"], artifacts["pending"],
            )
            original_inodes = [path.lstat().st_ino for path in original_paths]
            with mock.patch.object(operations, "journal_root", return_value=root):
                preview = operations.reset_failed_pending(journal_date, run_id)
                with self.assertRaisesRegex(ValueError, "exact confirmation"):
                    operations.reset_failed_pending(journal_date, run_id, "wrong", apply=True)
                result = operations.reset_failed_pending(
                    journal_date, run_id, confirmation, apply=True
                )

            self.assertTrue(preview["preview"])
            self.assertEqual(result["removed"], [])
            self.assertTrue(all(not path.exists() for path in original_paths))
            intent_path = reset_intent_path(root, journal_date, run_id)
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            self.assertEqual(intent["status"], "completed")
            quarantines = quarantine_paths(root, intent)
            self.assertEqual(
                result["quarantined"],
                [path.relative_to(root).as_posix() for path in quarantines],
            )
            self.assertEqual([path.lstat().st_ino for path in quarantines], original_inodes)
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

            with mock.patch.object(operations, "journal_root", return_value=root):
                retried = operations.reset_failed_pending(
                    journal_date, run_id, confirmation, apply=True
                )
            self.assertTrue(retried["completed"])
            self.assertEqual([path.lstat().st_ino for path in quarantines], original_inodes)

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
        lock_name = GLOBAL_GENERATION_LOCK
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
                self.assertTrue(stable_lock_path(root).exists())
                root_descriptor = os.open(
                    "/tmp", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    contender = os.open(
                        stable_lock_path(root).name,
                        os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=root_descriptor,
                    )
                    try:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    finally:
                        os.close(contender)
                finally:
                    os.close(root_descriptor)
                self.assertTrue(artifacts["manifest"].exists())
                self.assertTrue(artifacts["packet_dir"].exists())
                self.assertTrue(artifacts["pending"].exists())
                self.assertTrue(artifacts["run_dir"].exists())
            finally:
                process.terminate()
                process.join(5)

    def test_reset_refuses_note_state_and_exact_completion_markers(self):
        operations = load_operations()
        journal_date = "2026-06-26"
        run_id = "1f7ce9536c587265"
        confirmation = f"RESET FAILED JOURNAL RUN {journal_date} {run_id}"
        protected_relatives = (
            Path("notes/2026/06") / f"{journal_date}.md",
            Path("state") / f"{journal_date}-{run_id}.json",
            Path("runs") / run_id / "completion.json",
            Path("runs") / run_id / "completed-pending-receipt.json",
        )
        for relative in protected_relatives:
            with self.subTest(relative=str(relative)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                artifacts = write_pending_run(root, journal_date, run_id)
                protected = root / relative
                protected.parent.mkdir(parents=True, exist_ok=True)
                protected.write_text("{}\n", encoding="utf-8")
                with mock.patch.object(operations, "journal_root", return_value=root):
                    with self.assertRaisesRegex(ValueError, "canonical or completed state"):
                        operations.reset_failed_pending(
                            journal_date, run_id, confirmation, apply=True
                        )
                self.assertTrue(protected.exists())
                self.assertTrue(artifacts["pending"].exists())

    def test_post_flock_lock_replacement_fails_before_reset_moves(self):
        operations = load_operations()
        journal_date = "2026-06-26"
        run_id = "1f7ce9536c587265"
        confirmation = f"RESET FAILED JOURNAL RUN {journal_date} {run_id}"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = write_pending_run(root, journal_date, run_id)
            real_flock = operations.fcntl.flock
            replaced = False

            def replace_after_flock(descriptor, operation):
                nonlocal replaced
                result = real_flock(descriptor, operation)
                if operation & fcntl.LOCK_EX and not replaced:
                    replaced = True
                    stable_lock_path(root).rename(
                        Path("/tmp") / f"locked-away-{os.getpid()}-{time.time_ns()}"
                    )
                    stable_lock_path(root).write_text("replacement", encoding="utf-8")
                return result

            with mock.patch.object(operations, "journal_root", return_value=root), mock.patch.object(
                operations.fcntl, "flock", side_effect=replace_after_flock
            ):
                with self.assertRaisesRegex(ValueError, "stable lock changed"):
                    operations.reset_failed_pending(
                        journal_date, run_id, confirmation, apply=True
                    )
            self.assertTrue(replaced)
            self.assertEqual(
                stable_lock_path(root).read_text(encoding="utf-8"), "replacement"
            )
            self.assertTrue(artifacts["pending"].exists())

    def test_reset_root_path_replacement_stays_anchored_to_open_root(self):
        operations = load_operations()
        journal_date = "2026-06-26"
        run_id = "1f7ce9536c587265"
        confirmation = f"RESET FAILED JOURNAL RUN {journal_date} {run_id}"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "journal"
            write_pending_run(root, journal_date, run_id)
            anchored = base / "journal-opened"
            real_move = operations._quarantine_reset_target
            replaced = False

            def replace_root_then_move(*args, **kwargs):
                nonlocal replaced
                if not replaced:
                    replaced = True
                    root.rename(anchored)
                    replacement = write_pending_run(root, journal_date, run_id)
                    replacement["pending"].write_text("replacement", encoding="utf-8")
                return real_move(*args, **kwargs)

            with mock.patch.object(operations, "journal_root", return_value=root), mock.patch.object(
                operations, "_quarantine_reset_target", side_effect=replace_root_then_move
            ):
                operations.reset_failed_pending(journal_date, run_id, confirmation, apply=True)
            self.assertTrue(replaced)
            self.assertEqual(
                (root / "pending" / f"{run_id}.json").read_text(encoding="utf-8"),
                "replacement",
            )
            intent = json.loads(
                reset_intent_path(anchored, journal_date, run_id).read_text(encoding="utf-8")
            )
            self.assertTrue(all(path.exists() for path in quarantine_paths(anchored, intent)))

    def test_reset_resumes_after_each_interrupted_quarantine_move(self):
        journal_date = "2026-06-26"
        run_id = "1f7ce9536c587265"
        confirmation = f"RESET FAILED JOURNAL RUN {journal_date} {run_id}"
        for failure_index in range(4):
            with self.subTest(failure_index=failure_index), tempfile.TemporaryDirectory() as tmp:
                operations = load_operations()
                root = Path(tmp)
                artifacts = write_pending_run(root, journal_date, run_id)
                self.assertTrue(hasattr(operations, "_quarantine_reset_target"))
                real_move = operations._quarantine_reset_target
                calls = 0

                def interrupt_move(*args, **kwargs):
                    nonlocal calls
                    current = calls
                    calls += 1
                    result = real_move(*args, **kwargs)
                    if current == failure_index:
                        raise OSError("injected quarantine interruption")
                    return result

                with mock.patch.object(operations, "journal_root", return_value=root), mock.patch.object(
                    operations, "_quarantine_reset_target", side_effect=interrupt_move
                ):
                    with self.assertRaisesRegex(OSError, "injected quarantine"):
                        operations.reset_failed_pending(
                            journal_date, run_id, confirmation, apply=True
                        )
                self.assertTrue((root / f"reset-{journal_date}-{run_id}.json").exists())
                with mock.patch.object(operations, "journal_root", return_value=root):
                    result = operations.reset_failed_pending(
                        journal_date, run_id, confirmation, apply=True
                    )
                self.assertEqual(result["removed"], [])
                self.assertFalse(artifacts["manifest"].exists())
                self.assertFalse(artifacts["packet_dir"].exists())
                self.assertFalse(artifacts["pending"].exists())
                self.assertFalse(artifacts["run_dir"].exists())
                intent = json.loads(
                    reset_intent_path(root, journal_date, run_id).read_text(encoding="utf-8")
                )
                self.assertEqual(intent["status"], "completed")
                self.assertTrue(all(path.exists() for path in quarantine_paths(root, intent)))

    def test_reset_resumes_after_move_before_intent_write_and_preserves_quarantine(self):
        operations = load_operations()
        journal_date = "2026-06-26"
        run_id = "1f7ce9536c587265"
        confirmation = f"RESET FAILED JOURNAL RUN {journal_date} {run_id}"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = write_pending_run(root, journal_date, run_id)
            real_move = operations._quarantine_reset_target
            interrupted = False

            def interrupt_after_move(*args, **kwargs):
                nonlocal interrupted
                result = real_move(*args, **kwargs)
                if not interrupted:
                    interrupted = True
                    raise OSError("injected after move before intent write")
                return result

            with mock.patch.object(operations, "journal_root", return_value=root), mock.patch.object(
                operations, "_quarantine_reset_target", side_effect=interrupt_after_move
            ):
                with self.assertRaisesRegex(OSError, "after move"):
                    operations.reset_failed_pending(
                        journal_date, run_id, confirmation, apply=True
                    )
            self.assertTrue(interrupted)
            with mock.patch.object(operations, "journal_root", return_value=root):
                result = operations.reset_failed_pending(
                    journal_date, run_id, confirmation, apply=True
                )
            self.assertTrue(result["completed"])
            self.assertFalse(artifacts["run_dir"].exists())
            intent = json.loads(
                reset_intent_path(root, journal_date, run_id).read_text(encoding="utf-8")
            )
            self.assertTrue(all(path.exists() for path in quarantine_paths(root, intent)))

    def test_quarantine_destination_insertion_is_never_overwritten(self):
        operations = load_operations()
        journal_date = "2026-06-26"
        run_id = "1f7ce9536c587265"
        confirmation = f"RESET FAILED JOURNAL RUN {journal_date} {run_id}"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = write_pending_run(root, journal_date, run_id)
            real_rename = operations._rename_noreplace
            inserted = False

            def insert_destination(src_fd, src, dst_fd, dst):
                nonlocal inserted
                if not inserted:
                    inserted = True
                    descriptor = os.open(
                        dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dst_fd
                    )
                    os.write(descriptor, b"attacker")
                    os.close(descriptor)
                return real_rename(src_fd, src, dst_fd, dst)

            with mock.patch.object(operations, "journal_root", return_value=root), mock.patch.object(
                operations, "_rename_noreplace", side_effect=insert_destination
            ):
                with self.assertRaisesRegex(ValueError, "quarantine already exists"):
                    operations.reset_failed_pending(
                        journal_date, run_id, confirmation, apply=True
                    )
            intent = json.loads(
                reset_intent_path(root, journal_date, run_id).read_text(encoding="utf-8")
            )
            self.assertTrue(inserted)
            self.assertTrue(artifacts["run_dir"].exists())
            self.assertEqual(quarantine_paths(root, intent)[0].read_text(), "attacker")

    def test_completed_intent_fails_closed_for_moved_both_or_mismatched_quarantine(self):
        journal_date = "2026-06-26"
        run_id = "1f7ce9536c587265"
        confirmation = f"RESET FAILED JOURNAL RUN {journal_date} {run_id}"
        for mutation in ("moved", "both", "mismatch"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                operations = load_operations()
                root = Path(tmp)
                write_pending_run(root, journal_date, run_id)
                with mock.patch.object(operations, "journal_root", return_value=root):
                    operations.reset_failed_pending(
                        journal_date, run_id, confirmation, apply=True
                    )
                intent = json.loads(
                    reset_intent_path(root, journal_date, run_id).read_text(encoding="utf-8")
                )
                quarantine = quarantine_paths(root, intent)[0]
                original = root / intent["targets"][0]["path"]
                if mutation == "moved":
                    quarantine.rename(quarantine.with_name(quarantine.name + ".moved"))
                elif mutation == "both":
                    original.mkdir()
                else:
                    quarantine.rename(quarantine.with_name(quarantine.name + ".owned"))
                    quarantine.mkdir()
                with mock.patch.object(operations, "journal_root", return_value=root):
                    with self.assertRaisesRegex(
                        ValueError, "lost|both original and quarantine|identity changed"
                    ):
                        operations.reset_failed_pending(
                            journal_date, run_id, confirmation, apply=True
                        )

    def test_windows_move_request_is_bound_to_descriptor_paths_and_inodes(self):
        operations = load_operations()
        source_parent = mock.Mock(st_ino=1002, st_mode=stat.S_IFDIR | 0o700)
        destination_parent = mock.Mock(st_ino=2002, st_mode=stat.S_IFDIR | 0o700)
        source = mock.Mock(st_ino=3002, st_mode=stat.S_IFDIR | 0o700)

        def descriptor_path(path):
            return {
                "/proc/self/fd/11": "/mnt/c/journal/runs",
                "/proc/self/fd/22": "/mnt/c/journal/reset-quarantine/owned",
            }[path]

        with mock.patch.object(operations.os, "readlink", side_effect=descriptor_path), mock.patch.object(
            operations.os, "fstat", side_effect=lambda descriptor: {
                11: source_parent,
                22: destination_parent,
            }[descriptor]
        ), mock.patch.object(operations.os, "stat", return_value=source):
            request = operations._build_windows_move_request(
                11, "run-id", 22, "run"
            )

        self.assertEqual(
            request,
            {
                "source_parent": "C:\\journal\\runs",
                "destination_parent": "C:\\journal\\reset-quarantine\\owned",
                "source_name": "run-id",
                "destination_name": "run",
                "source_parent_inode": 1002,
                "destination_parent_inode": 2002,
                "source_inode": 3002,
                "directory": True,
            },
        )

    def test_windows_backend_invokes_fixed_native_helper_with_bounded_json(self):
        operations = load_operations()
        request = {
            "source_parent": "C:\\journal\\runs",
            "destination_parent": "C:\\journal\\reset-quarantine\\owned",
            "source_name": "run-id",
            "destination_name": "run",
            "source_parent_inode": 1002,
            "destination_parent_inode": 2002,
            "source_inode": 3002,
            "directory": True,
        }
        completed = mock.Mock(
            returncode=0,
            stdout='{"file_id":3000,"ok":true,"volume":99}\n',
            stderr="",
        )
        with mock.patch.object(
            operations, "_build_windows_move_request", return_value=request
        ), mock.patch.object(
            operations,
            "_windows_move_runtime",
            return_value=(Path("/mnt/c/hermes/python.exe"), "C:\\hermes\\move.py"),
            create=True,
        ), mock.patch.object(
            operations.subprocess, "run", return_value=completed
        ) as run:
            operations._windows_mount_rename_noreplace(11, "run-id", 22, "run")

        run.assert_called_once_with(
            ["/mnt/c/hermes/python.exe", "C:\\hermes\\move.py"],
            input=json.dumps(request, separators=(",", ":"), sort_keys=True),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    @unittest.skipIf(sys.platform == "darwin", "requires the Linux WSL rename backend")
    def test_windows_mount_backend_handles_renameat2_einval_without_weak_rename(self):
        operations = load_operations()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "source").mkdir()
            source_parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            destination_parent = os.dup(source_parent)

            def unsupported(*_args):
                ctypes.set_errno(errno.EINVAL)
                return -1

            try:
                with mock.patch.object(
                    operations, "_RENAMEAT2", side_effect=unsupported
                ), mock.patch.object(
                    operations, "_windows_mount_rename_noreplace", create=True
                ) as windows_move:
                    operations._rename_noreplace(
                        source_parent, "source", destination_parent, "destination"
                    )
                windows_move.assert_called_once_with(
                    source_parent, "source", destination_parent, "destination"
                )
                self.assertTrue((root / "source").is_dir())
                self.assertFalse((root / "destination").exists())
            finally:
                os.close(destination_parent)
                os.close(source_parent)

    @unittest.skipIf(sys.platform == "darwin", "requires the Linux renameat2 backend")
    def test_renameat2_unavailable_fails_closed_without_moving_evidence(self):
        operations = load_operations()
        journal_date = "2026-06-26"
        run_id = "1f7ce9536c587265"
        confirmation = f"RESET FAILED JOURNAL RUN {journal_date} {run_id}"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = write_pending_run(root, journal_date, run_id)
            with mock.patch.object(
                operations, "journal_root", return_value=root
            ), mock.patch.object(
                operations, "_RENAMEAT2", None
            ):
                with self.assertRaisesRegex(OSError, "no-replace rename"):
                    operations.reset_failed_pending(
                        journal_date, run_id, confirmation, apply=True
                    )
            self.assertTrue(artifacts["run_dir"].exists())

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

    def test_purge_preserves_unowned_legacy_generation_lock(self):
        operations = load_operations()
        lock_name = "generation-0123456789abcdef.lock"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / lock_name).write_text("stale", encoding="utf-8")
            with mock.patch.object(operations, "journal_root", return_value=root):
                result = operations.purge("DELETE MY JOURNAL DATA", apply=True)
            self.assertNotIn(lock_name, result["removed"])
            self.assertEqual((root / lock_name).read_text(encoding="utf-8"), "stale")

    def test_purge_preserves_unowned_legacy_lock_symlink_and_directory(self):
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
                    result = operations.purge("DELETE MY JOURNAL DATA", apply=True)
                self.assertNotIn(lock_name, result["removed"])
                self.assertTrue((root / lock_name).exists() or (root / lock_name).is_symlink())

    def test_purge_never_stats_or_unlinks_unowned_legacy_lock(self):
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
                result = operations.purge("DELETE MY JOURNAL DATA", apply=True)
            self.assertFalse(swapped)
            self.assertNotIn(lock_name, result["removed"])
            self.assertEqual(lock_path.read_text(encoding="utf-8"), "stale")


class FinalReviewAdversarialTests(unittest.TestCase):
    def test_forged_active_intent_cannot_bypass_owned_receipt_validation(self):
        operations = load_operations()
        journal_date = "2026-06-26"
        run_id = "1f7ce9536c587265"
        confirmation = f"RESET FAILED JOURNAL RUN {journal_date} {run_id}"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifacts = write_pending_run(root, journal_date, run_id)
            receipt = artifacts["receipt"]
            outside = root / "outside.md"
            outside.write_text("unowned", encoding="utf-8")
            receipt["packet_path"] = str(outside)
            receipt["packet_paths"] = [str(outside)]
            receipt_text = json.dumps(receipt)
            artifacts["pending"].write_text(receipt_text, encoding="utf-8")
            token = "a" * 48
            namespace = f"reset-quarantine/{journal_date}-{run_id}-{token}"
            specs = operations._reset_target_specs(journal_date, run_id)
            names = ("run", "packets", "manifest.json", "pending.json")
            targets = []
            for (relative, directory), name in zip(specs, names):
                metadata = (root / relative).lstat()
                targets.append({
                    "path": relative.as_posix(),
                    "directory": directory,
                    "device": metadata.st_dev,
                    "inode": metadata.st_ino,
                    "quarantine_path": f"{namespace}/{name}",
                    "status": "pending",
                })
            intent = {
                "schema_version": 1,
                "journal_date": journal_date,
                "run_id": run_id,
                "status": "active",
                "quarantine_token": token,
                "pending_receipt_sha256": hashlib.sha256(
                    receipt_text.encode("utf-8")
                ).hexdigest(),
                "targets": targets,
            }
            reset_intent_path(root, journal_date, run_id).write_text(
                json.dumps(intent), encoding="utf-8"
            )
            with mock.patch.object(operations, "journal_root", return_value=root):
                with self.assertRaisesRegex(ValueError, "owned packet paths"):
                    operations.reset_failed_pending(
                        journal_date, run_id, confirmation, apply=True
                    )
            self.assertEqual(outside.read_text(encoding="utf-8"), "unowned")
            self.assertTrue(artifacts["pending"].exists())

    def test_parent_namespace_replacement_cannot_split_operating_system_lock(self):
        operations = load_operations()
        journal_date = "2026-07-27"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "workspace"
            root = base / "journal"
            root.mkdir(parents=True)
            moved_base = Path(tmp) / "workspace-opened"
            nested_result = None

            def replace_parent_and_nest(*args, **kwargs):
                nonlocal nested_result
                base.rename(moved_base)
                root.mkdir(parents=True)
                nested_result = operations.run_generation(
                    "nested after parent replacement", expected_dates=[journal_date]
                )
                return mock.Mock(returncode=1, stdout="", stderr="synthetic failure")

            with mock.patch.object(operations, "journal_root", return_value=root), mock.patch.object(
                operations, "_hermes_executable", return_value="hermes"
            ), mock.patch.object(
                operations, "_generation_collect", return_value={"ok": True}
            ), mock.patch.object(
                operations.subprocess, "run", side_effect=replace_parent_and_nest
            ):
                outer = operations.run_generation(
                    "outer generation", expected_dates=[journal_date]
                )
            self.assertFalse(outer["ok"])
            self.assertIsNotNone(nested_result)
            self.assertFalse(nested_result["ok"])
            self.assertIn("already running", nested_result["error"])
            self.assertTrue(
                (moved_base / "journal" / f"generation-{outer['request_id']}.json").is_file()
            )
            self.assertFalse(
                (root / f"generation-{outer['request_id']}.json").exists()
            )

    def test_generation_prelaunch_failure_closes_stable_lock_and_root_descriptors(self):
        operations = load_operations()
        with mock.patch.object(
            operations,
            "_open_stable_locked_root",
            return_value=(101, 102, 103, mock.Mock()),
        ), mock.patch.object(
            operations, "_write_descriptor_json", side_effect=OSError("ledger failed")
        ), mock.patch.object(operations.os, "close") as close:
            with self.assertRaisesRegex(OSError, "ledger failed"):
                operations.run_generation(
                    "prelaunch failure", expected_dates=["2026-07-27"]
                )
        self.assertEqual(
            {call.args[0] for call in close.call_args_list},
            {101, 102, 103},
        )

    def test_root_replacement_cannot_start_nested_generation_and_ledger_stays_anchored(self):
        operations = load_operations()
        journal_date = "2026-07-27"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "journal"
            root.mkdir()
            anchored = base / "journal-opened"
            nested_result = None
            calls = 0

            def replace_root_and_nest(*args, **kwargs):
                nonlocal calls, nested_result
                calls += 1
                if calls == 1:
                    root.rename(anchored)
                    root.mkdir()
                    nested_result = operations.run_generation(
                        "nested generation", expected_dates=[journal_date]
                    )
                return mock.Mock(returncode=1, stdout="", stderr="synthetic failure")

            with mock.patch.object(operations, "journal_root", return_value=root), mock.patch.object(
                operations, "_hermes_executable", return_value="hermes"
            ), mock.patch.object(
                operations, "_generation_collect", return_value={"ok": True}
            ), mock.patch.object(
                operations.subprocess, "run", side_effect=replace_root_and_nest
            ):
                outer = operations.run_generation("outer generation", expected_dates=[journal_date])

            self.assertFalse(outer["ok"])
            self.assertIsNotNone(nested_result)
            self.assertFalse(nested_result["ok"])
            self.assertIn("already running", nested_result["error"])
            self.assertEqual(calls, 1)
            request_id = hashlib.sha256(b"outer generation").hexdigest()[:16]
            self.assertTrue((anchored / f"generation-{request_id}.json").is_file())
            self.assertFalse((root / f"generation-{request_id}.json").exists())

    @unittest.skipIf(sys.platform == "darwin", "requires Linux descriptor path cwd")
    def test_non_bridge_generation_anchors_child_cwd_and_root_to_held_descriptor(self):
        operations = load_operations()
        journal_date = "2026-07-27"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "journal"
            root.mkdir()
            observed = {}

            def inspect_child(*args, **kwargs):
                observed.update(kwargs)
                self.assertEqual(os.stat(kwargs["cwd"]).st_ino, root.stat().st_ino)
                return mock.Mock(returncode=1, stdout="", stderr="synthetic failure")

            with mock.patch.object(
                operations, "journal_root", return_value=root
            ), mock.patch.object(
                operations, "_hermes_executable", return_value="hermes"
            ), mock.patch.object(
                operations, "_generation_collect", return_value={"ok": True}
            ), mock.patch.object(operations.subprocess, "run", side_effect=inspect_child):
                operations.run_generation("anchored child", expected_dates=[journal_date])

            self.assertEqual(observed["env"]["MY_JOURNAL_ROOT"], ".")
            self.assertIn("cwd", observed)

    def test_macos_generation_uses_inherited_descriptor_wrapper_without_path_cwd(self):
        operations = load_operations()
        journal_date = "2026-07-27"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "journal"
            root.mkdir()
            observed = {}

            def inspect_child(command, **kwargs):
                observed["command"] = command
                observed.update(kwargs)
                return mock.Mock(returncode=1, stdout="", stderr="synthetic failure")

            with mock.patch.object(operations.sys, "platform", "darwin"), mock.patch.object(
                operations, "journal_root", return_value=root
            ), mock.patch.object(
                operations, "_hermes_executable", return_value="hermes"
            ), mock.patch.object(
                operations, "_generation_collect", return_value={"ok": True}
            ), mock.patch.object(operations.subprocess, "run", side_effect=inspect_child):
                operations.run_generation("anchored mac child", expected_dates=[journal_date])

            command = observed["command"]
            self.assertEqual(command[0], sys.executable)
            self.assertTrue(command[1].endswith("descriptor_exec.py"))
            self.assertTrue(command[2].isdigit())
            self.assertEqual(command[3], "--")
            self.assertEqual(command[4], "hermes")
            self.assertNotIn("cwd", observed)
            self.assertEqual(observed["pass_fds"], (int(command[2]),))
            self.assertEqual(observed["env"]["MY_JOURNAL_ROOT"], ".")

    def test_completed_intent_rejects_wrong_type_even_with_forged_identity(self):
        operations = load_operations()
        journal_date = "2026-06-26"
        run_id = "1f7ce9536c587265"
        confirmation = f"RESET FAILED JOURNAL RUN {journal_date} {run_id}"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_pending_run(root, journal_date, run_id)
            with mock.patch.object(operations, "journal_root", return_value=root):
                operations.reset_failed_pending(journal_date, run_id, confirmation, apply=True)
            intent_path = reset_intent_path(root, journal_date, run_id)
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            quarantine = quarantine_paths(root, intent)[0]
            quarantine.rename(quarantine.with_name(quarantine.name + "-owned"))
            quarantine.write_text("forged regular file", encoding="utf-8")
            forged = quarantine.lstat()
            intent["targets"][0]["device"] = forged.st_dev
            intent["targets"][0]["inode"] = forged.st_ino
            intent_path.write_text(json.dumps(intent), encoding="utf-8")

            with mock.patch.object(operations, "journal_root", return_value=root):
                with self.assertRaisesRegex(ValueError, "type|owned directory"):
                    operations.reset_failed_pending(
                        journal_date, run_id, confirmation, apply=True
                    )

    def test_successful_reset_leaves_pending_scanners_and_recollection_healthy(self):
        operations = load_operations()
        journal_date = "2026-06-26"
        run_id = "1f7ce9536c587265"
        confirmation = f"RESET FAILED JOURNAL RUN {journal_date} {run_id}"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_pending_run(root, journal_date, run_id)
            with mock.patch.object(operations, "journal_root", return_value=root):
                result = operations.reset_failed_pending(
                    journal_date, run_id, confirmation, apply=True
                )
                self.assertEqual(operations.read_pending_receipts(root), [])
                self.assertEqual(operations.maintenance()["pending_run_count"], 0)
            self.assertTrue(all(path.startswith("reset-quarantine/") for path in result["quarantined"]))
            recollected = write_pending_run(root, journal_date, "2f7ce9536c587265")
            self.assertTrue(recollected["pending"].is_file())
            self.assertEqual(len(operations.read_pending_receipts(root)), 1)

    def test_root_replacement_returns_truthful_relative_quarantine_paths(self):
        operations = load_operations()
        journal_date = "2026-06-26"
        run_id = "1f7ce9536c587265"
        confirmation = f"RESET FAILED JOURNAL RUN {journal_date} {run_id}"
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "journal"
            write_pending_run(root, journal_date, run_id)
            anchored = base / "journal-opened"
            real_move = operations._quarantine_reset_target
            replaced = False

            def replace_root_then_move(*args, **kwargs):
                nonlocal replaced
                if not replaced:
                    replaced = True
                    root.rename(anchored)
                    write_pending_run(root, journal_date, run_id)
                return real_move(*args, **kwargs)

            with mock.patch.object(operations, "journal_root", return_value=root), mock.patch.object(
                operations, "_quarantine_reset_target", side_effect=replace_root_then_move
            ):
                result = operations.reset_failed_pending(
                    journal_date, run_id, confirmation, apply=True
                )
            self.assertTrue(replaced)
            self.assertTrue(all(not Path(path).is_absolute() for path in result["quarantined"]))
            self.assertTrue(all((anchored / path).exists() for path in result["quarantined"]))
            self.assertTrue(all((root / path).exists() is False for path in result["quarantined"]))

    def test_macos_renameatx_np_backend_uses_rename_excl(self):
        operations = load_operations()
        renameatx = mock.Mock(return_value=0)
        with mock.patch.object(operations.sys, "platform", "darwin"), mock.patch.object(
            operations, "_RENAMEAT2", None
        ), mock.patch.object(operations, "_RENAMEATX_NP", renameatx):
            operations._rename_noreplace(11, "source", 12, "destination")
        renameatx.assert_called_once_with(
            11, b"source", 12, b"destination", operations._RENAME_EXCL
        )

    def test_purge_acquires_stable_lock_before_listing_root(self):
        operations = load_operations()
        context = get_context("fork")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "journal"
            (root / "notes").mkdir(parents=True)
            ready = context.Event()
            process = context.Process(
                target=hold_generation_lock,
                args=(str(root), operations._stable_lock_name(root), ready),
            )
            process.start()
            self.assertTrue(ready.wait(5))
            try:
                with mock.patch.object(operations, "journal_root", return_value=root), mock.patch.object(
                    operations.os, "listdir", wraps=operations.os.listdir
                ) as listed:
                    with self.assertRaisesRegex(ValueError, "generation is active"):
                        operations.purge("DELETE MY JOURNAL DATA", apply=True)
                listed.assert_not_called()
                self.assertTrue((root / "notes").is_dir())
            finally:
                process.terminate()
                process.join(5)


if __name__ == "__main__":
    unittest.main()
