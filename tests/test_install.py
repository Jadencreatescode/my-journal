from __future__ import annotations

import importlib.util
import signal
import tempfile
import unittest
import os
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_installer():
    spec = importlib.util.spec_from_file_location("my_journal_installer", ROOT / "install.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class InstallerSecurityTests(unittest.TestCase):
    def test_remove_remains_anchored_when_parent_is_swapped(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            parent = base / "trusted"
            external = base / "external"
            parent.mkdir()
            external.mkdir()
            target = parent / "component"
            target.mkdir()
            (target / "safe.txt").write_text("safe", encoding="utf-8")
            external_file = external / "keep.txt"
            external_file.write_text("keep", encoding="utf-8")
            real_rmtree = installer.shutil.rmtree
            swapped = False

            def racing_rmtree(path, *args, dir_fd=None, **kwargs):
                nonlocal swapped
                if dir_fd is not None and not swapped:
                    swapped = True
                    parent.rename(base / "trusted-original")
                    parent.symlink_to(external, target_is_directory=True)
                return real_rmtree(path, *args, dir_fd=dir_fd, **kwargs)

            with mock.patch.object(installer.shutil, "rmtree", side_effect=racing_rmtree):
                installer._remove_path(target)

            self.assertTrue(swapped)
            self.assertFalse((base / "trusted-original" / "component").exists())
            self.assertEqual(external_file.read_text(encoding="utf-8"), "keep")

    def test_move_remains_anchored_when_destination_parent_is_swapped(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source_parent = base / "source"
            destination_parent = base / "destination"
            external_parent = base / "external"
            source_parent.mkdir()
            destination_parent.mkdir()
            external_parent.mkdir()
            source = source_parent / "component"
            source.mkdir()
            (source / "safe.txt").write_text("safe", encoding="utf-8")
            destination = destination_parent / "component"
            real_rename = os.rename
            swapped = False

            def racing_rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
                nonlocal swapped
                if src_dir_fd is not None and dst_dir_fd is not None and not swapped:
                    swapped = True
                    destination_parent.rename(base / "destination-original")
                    destination_parent.symlink_to(external_parent, target_is_directory=True)
                return real_rename(
                    src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd
                )

            with mock.patch.object(installer.os, "rename", side_effect=racing_rename):
                installer._move_path(source, destination)

            self.assertTrue(swapped)
            self.assertEqual(
                (base / "destination-original" / "component" / "safe.txt").read_text(encoding="utf-8"),
                "safe",
            )
            self.assertEqual(list(external_parent.iterdir()), [])

    def test_recover_repairs_interrupted_upgrade_activation(self):
        installer = load_installer()

        class SimulatedProcessDeath(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            for _, target in installer.COMPONENTS:
                destination = home / target
                destination.mkdir(parents=True)
                (destination / "original.txt").write_text("original", encoding="utf-8")
            real_move = installer._move_path
            calls = 0

            def die_during_activation(source, destination):
                nonlocal calls
                calls += 1
                if calls == 4:
                    raise SimulatedProcessDeath()
                return real_move(source, destination)

            setattr(installer, "_move_path", die_during_activation)
            try:
                with self.assertRaises(SimulatedProcessDeath):
                    installer.install(ROOT, home, upgrade=True)
            finally:
                setattr(installer, "_move_path", real_move)

            recovered = installer.recover(home)

            self.assertEqual(len(recovered), len(installer.COMPONENTS))
            for _, target in installer.COMPONENTS:
                destination = home / target
                self.assertEqual(
                    (destination / "original.txt").read_text(encoding="utf-8"), "original"
                )
            self.assertFalse((home / ".my-journal" / "install-transaction.json").exists())

    def test_restore_of_fresh_install_returns_to_absent_components(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            installer.install(ROOT, home, upgrade=False)

            restored = installer.restore(home)

            self.assertEqual(len(restored), len(installer.COMPONENTS))
            for _, target in installer.COMPONENTS:
                self.assertFalse((home / target).exists())
            self.assertFalse((home / ".my-journal" / "install-state.json").exists())

    def test_restore_failure_rolls_back_to_installed_components(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            for _, target in installer.COMPONENTS:
                destination = home / target
                destination.mkdir(parents=True)
                (destination / "original.txt").write_text("original", encoding="utf-8")
            installer.install(ROOT, home, upgrade=True)
            installed_hashes = {
                target.as_posix(): installer._tree_sha256(home / target)
                for _, target in installer.COMPONENTS
            }
            real_move = installer._move_path
            calls = 0

            def fail_fourth_move(source, destination):
                nonlocal calls
                calls += 1
                if calls == 4:
                    raise OSError("synthetic restore failure")
                return real_move(source, destination)

            setattr(installer, "_move_path", fail_fourth_move)
            try:
                with self.assertRaisesRegex(OSError, "synthetic restore failure"):
                    installer.restore(home)
            finally:
                setattr(installer, "_move_path", real_move)

            for _, target in installer.COMPONENTS:
                self.assertEqual(
                    installer._tree_sha256(home / target), installed_hashes[target.as_posix()]
                )
            self.assertTrue((home / ".my-journal" / "install-state.json").is_file())

    def test_cli_can_uninstall_transactionally(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            installer.install(ROOT, home, upgrade=False)

            exit_code = installer.main([
                "--hermes-home", str(home), "--uninstall", "--force"
            ])

            self.assertEqual(exit_code, 0)
            for _, target in installer.COMPONENTS:
                self.assertFalse((home / target).exists())

    def test_uninstall_refuses_modified_installed_component(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            installer.install(ROOT, home, upgrade=False)
            modified = home / "plugins" / "my-journal" / "core.py"
            modified.write_text(modified.read_text(encoding="utf-8") + "\n# local change\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "modified"):
                installer.uninstall(home, force=False)

            self.assertTrue(modified.exists())

    def test_forced_uninstall_removes_code_but_preserves_journal_data(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            installer.install(ROOT, home, upgrade=False)
            note = home / "journal" / "notes" / "2026" / "07" / "2026-07-27.md"
            note.parent.mkdir(parents=True)
            note.write_text("private journal", encoding="utf-8")

            removed = installer.uninstall(home, force=True)

            self.assertEqual(len(removed), len(installer.COMPONENTS))
            for _, target in installer.COMPONENTS:
                self.assertFalse((home / target).exists())
            self.assertEqual(note.read_text(encoding="utf-8"), "private journal")
            self.assertFalse((home / ".my-journal" / "install-state.json").exists())

    def test_successful_upgrade_can_restore_exact_previous_components(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            for index, (_, target) in enumerate(installer.COMPONENTS, start=1):
                destination = home / target
                destination.mkdir(parents=True)
                (destination / "original.txt").write_text(
                    f"original-{index}", encoding="utf-8"
                )

            installer.install(ROOT, home, upgrade=True)

            state_path = home / ".my-journal" / "install-state.json"
            self.assertTrue(state_path.is_file())
            restored = installer.restore(home)

            self.assertEqual(len(restored), len(installer.COMPONENTS))
            for index, (_, target) in enumerate(installer.COMPONENTS, start=1):
                destination = home / target
                self.assertEqual(
                    (destination / "original.txt").read_text(encoding="utf-8"),
                    f"original-{index}",
                )
            # Restored components remain managed so a later uninstall is safe.
            self.assertTrue(state_path.is_file())

    def test_rejects_symlinked_destination_parent_without_external_writes(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "home"
            outside = base / "outside"
            home.mkdir()
            outside.mkdir()
            (home / "skills").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink"):
                installer.install(ROOT, home, upgrade=False)

            self.assertEqual(list(outside.iterdir()), [])
            self.assertFalse((home / "plugins").exists())
    def test_upgrade_failure_restores_every_original_component(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            for _, target in installer.COMPONENTS:
                destination = home / target
                destination.mkdir(parents=True)
                (destination / "original.txt").write_text("original", encoding="utf-8")

            real_copytree = installer.shutil.copytree
            calls = 0

            def fail_second_copy(source, destination, *args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic copy failure")
                return real_copytree(source, destination, *args, **kwargs)

            installer.shutil.copytree = fail_second_copy
            try:
                with self.assertRaisesRegex(OSError, "synthetic copy failure"):
                    installer.install(ROOT, home, upgrade=True)
            finally:
                installer.shutil.copytree = real_copytree

            for _, target in installer.COMPONENTS:
                destination = home / target
                self.assertEqual((destination / "original.txt").read_text(encoding="utf-8"), "original")
            self.assertEqual(list(home.rglob("*.backup-*")), [])
            self.assertEqual(list(home.glob(".my-journal-stage-*")), [])
            self.assertFalse((home / ".my-journal" / "install-transaction.json").exists())

    def test_commit_failure_rolls_back_every_destination(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            for _, target in installer.COMPONENTS:
                destination = home / target
                destination.mkdir(parents=True)
                (destination / "original.txt").write_text("original", encoding="utf-8")

            real_move = getattr(installer, "_move_path")
            calls = 0

            def fail_fourth_move(source, destination):
                nonlocal calls
                calls += 1
                if calls == 4:
                    raise OSError("synthetic commit failure")
                return real_move(source, destination)

            setattr(installer, "_move_path", fail_fourth_move)
            try:
                with self.assertRaisesRegex(OSError, "synthetic commit failure"):
                    installer.install(ROOT, home, upgrade=True)
            finally:
                setattr(installer, "_move_path", real_move)

            for _, target in installer.COMPONENTS:
                destination = home / target
                self.assertEqual((destination / "original.txt").read_text(encoding="utf-8"), "original")
            self.assertEqual(list(home.rglob("*.backup-*")), [])
            self.assertEqual(list(home.glob(".my-journal-stage-*")), [])


class InstallerHardeningRegressionTests(unittest.TestCase):
    def test_install_rejects_symlink_inside_package_component(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "package"
            home = self._home(root)
            for source, _ in installer.COMPONENTS:
                component = package / source
                component.mkdir(parents=True)
                (component / "payload.txt").write_text("payload", encoding="utf-8")
            victim = root / "external.txt"
            victim.write_text("must not be copied", encoding="utf-8")
            link = package / "plugins" / "my-journal" / "external.txt"
            link.symlink_to(victim)

            with self.assertRaises((OSError, ValueError)):
                installer.install(package, home, upgrade=False)
            self.assertFalse((home / "plugins" / "my-journal").exists())
            self.assertEqual(list(home.glob(".my-journal-stage-*")), [])
            self.assertFalse((home / ".my-journal" / "install-transaction.json").exists())

    def test_staged_fingerprint_base_exception_removes_pretransaction_stage(self):
        installer = load_installer()

        class SimulatedProcessDeath(BaseException):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(Path(tmp))
            with mock.patch.object(
                installer, "_tree_sha256", side_effect=SimulatedProcessDeath()
            ):
                with self.assertRaises(SimulatedProcessDeath):
                    installer.install(ROOT, home, upgrade=False)

            self.assertEqual(list(home.glob(".my-journal-stage-*")), [])
            self.assertFalse((home / ".my-journal" / "install-transaction.json").exists())

    def test_tree_fingerprint_does_not_follow_file_swapped_to_symlink(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            component = root / "component"
            component.mkdir()
            entry = component / "entry.txt"
            entry.write_text("safe", encoding="utf-8")
            victim = root / "victim.txt"
            victim.write_text("external secret", encoding="utf-8")
            real_stat = os.stat
            swapped = False

            def racing_stat(path, *args, **kwargs):
                nonlocal swapped
                result = real_stat(path, *args, **kwargs)
                if not swapped and (path == entry or path == entry.name):
                    entry.unlink()
                    entry.symlink_to(victim)
                    swapped = True
                return result

            with mock.patch.object(os, "stat", side_effect=racing_stat):
                with self.assertRaises((OSError, ValueError)):
                    installer._tree_sha256(component)
            self.assertTrue(swapped)

    def _home(self, root: Path) -> Path:
        home = root / "home"
        home.mkdir()
        return home

    def _write_existing_components(self, installer, home: Path) -> None:
        for _, target in installer.COMPONENTS:
            destination = home / target
            destination.mkdir(parents=True)
            (destination / "original.txt").write_text("original", encoding="utf-8")

    def test_atomic_json_temp_symlink_cannot_overwrite_external_file(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._home(root)
            metadata = home / ".my-journal"
            metadata.mkdir()
            external = root / "external.json"
            external.write_text("keep", encoding="utf-8")
            token = "a" * 32
            (metadata / f".install-state.{token}.tmp").symlink_to(external)
            with mock.patch.object(installer.secrets, "token_hex", return_value=token):
                with self.assertRaises(FileExistsError):
                    installer._write_state(home, {"safe": True})
            self.assertEqual(external.read_text(encoding="utf-8"), "keep")

    def test_state_traversal_is_rejected_by_restore_and_uninstall(self):
        installer = load_installer()
        for operation in (installer.restore, installer.uninstall):
            with self.subTest(operation=operation.__name__), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                home = self._home(root)
                external = root / "external"
                external.mkdir()
                keep = external / "keep.txt"
                keep.write_text("keep", encoding="utf-8")
                state = home / ".my-journal" / "install-state.json"
                state.parent.mkdir()
                state.write_text(
                    '{"schema_version":1,"components":[{"destination":"../external",'
                    '"backup":null,"installed_sha256":"x"}]}', encoding="utf-8"
                )
                with self.assertRaises(ValueError):
                    operation(home)
                self.assertEqual(keep.read_text(encoding="utf-8"), "keep")

    def test_transaction_traversal_cannot_delete_external_tree(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._home(root)
            external = root / "external"
            external.mkdir()
            keep = external / "keep.txt"
            keep.write_text("keep", encoding="utf-8")
            transaction = home / ".my-journal" / "install-transaction.json"
            transaction.parent.mkdir()
            transaction.write_text(
                '{"schema_version":1,"action":"install","status":"active",'
                '"stage_root":"../external","components":[]}', encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                installer.recover(home)
            self.assertEqual(keep.read_text(encoding="utf-8"), "keep")

    def test_lifecycle_lock_rejects_concurrent_operation(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(Path(tmp))
            with installer._lifecycle_lock(home):
                with self.assertRaisesRegex(BlockingIOError, "lifecycle operation"):
                    installer.install(ROOT, home, upgrade=False)
        # A released advisory lock can immediately be acquired again.
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(Path(tmp))
            installer.install(ROOT, home, upgrade=False)

    def test_restore_refuses_modified_code_unless_forced(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(Path(tmp))
            self._write_existing_components(installer, home)
            installer.install(ROOT, home, upgrade=True)
            modified = home / "plugins" / "my-journal" / "core.py"
            modified.write_text(modified.read_text(encoding="utf-8") + "\n# modified\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "modified"):
                installer.restore(home)
            restored = installer.restore(home, force=True)
            self.assertEqual(len(restored), len(installer.COMPONENTS))

    def test_cli_force_is_valid_with_restore(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(Path(tmp))
            self._write_existing_components(installer, home)
            installer.install(ROOT, home, upgrade=True)
            modified = home / "plugins" / "my-journal" / "core.py"
            modified.write_text("modified", encoding="utf-8")
            self.assertEqual(installer.main(["--hermes-home", str(home), "--restore", "--force"]), 0)

    def test_tree_fingerprint_detects_mode_and_empty_directory_changes(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tree"
            root.mkdir()
            file = root / "file"
            file.write_text("same", encoding="utf-8")
            baseline = installer._tree_sha256(root)
            file.chmod(0o700)
            self.assertNotEqual(installer._tree_sha256(root), baseline)
            mode_hash = installer._tree_sha256(root)
            (root / "empty").mkdir()
            self.assertNotEqual(installer._tree_sha256(root), mode_hash)
            shape_hash = installer._tree_sha256(root)
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "ignored.pyc").write_bytes(b"runtime")
            self.assertEqual(installer._tree_sha256(root), shape_hash)

    def test_install_is_recoverable_after_every_move_boundary(self):
        installer = load_installer()
        class Death(BaseException):
            pass
        for boundary in range(1, 7):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                home = self._home(Path(tmp))
                self._write_existing_components(installer, home)
                real_move = installer._move_path
                calls = 0
                def dying_move(source, destination):
                    nonlocal calls
                    calls += 1
                    result = real_move(source, destination)
                    if calls == boundary:
                        raise Death()
                    return result
                with mock.patch.object(installer, "_move_path", side_effect=dying_move):
                    with self.assertRaises(Death):
                        installer.install(ROOT, home, upgrade=True)
                installer.recover(home)
                for _, target in installer.COMPONENTS:
                    self.assertEqual((home / target / "original.txt").read_text(encoding="utf-8"), "original")

    def test_backup_traversal_is_rejected_without_external_deletion(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = self._home(root)
            external = root / "external"
            external.mkdir()
            keep = external / "keep.txt"
            keep.write_text("keep", encoding="utf-8")
            state = home / ".my-journal" / "install-state.json"
            state.parent.mkdir()
            destination = installer.COMPONENTS[0][1].as_posix()
            state.write_text(
                '{"schema_version":1,"components":[{"destination":' + repr(destination).replace("'", '"') +
                ',"backup":"../external","installed_sha256":"' + ("0" * 64) + '"}]}',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                installer.restore(home, force=True)
            self.assertEqual(keep.read_text(encoding="utf-8"), "keep")

    @unittest.skipUnless(hasattr(os, "fork"), "requires POSIX process locks")
    def test_lifecycle_lock_releases_when_holder_process_dies(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(Path(tmp))
            read_fd, write_fd = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                try:
                    with installer._lifecycle_lock(home):
                        os.write(write_fd, b"1")
                        signal.pause()
                finally:
                    os._exit(0)
            os.close(write_fd)
            try:
                self.assertEqual(os.read(read_fd, 1), b"1")
                with self.assertRaises(BlockingIOError):
                    installer.install(ROOT, home, upgrade=False)
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                pid = 0
                installer.install(ROOT, home, upgrade=False)
            finally:
                os.close(read_fd)
                if pid:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)

    def test_interrupted_restore_is_recoverable_after_every_move_boundary(self):
        installer = load_installer()
        class Death(BaseException):
            pass
        for boundary in range(1, 7):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                home = self._home(Path(tmp))
                self._write_existing_components(installer, home)
                installer.install(ROOT, home, upgrade=True)
                expected = {target: installer._tree_sha256(home / target) for _, target in installer.COMPONENTS}
                real_move = installer._move_path
                calls = 0
                def dying_move(source, destination):
                    nonlocal calls
                    calls += 1
                    result = real_move(source, destination)
                    if calls == boundary:
                        raise Death()
                    return result
                with mock.patch.object(installer, "_move_path", side_effect=dying_move):
                    with self.assertRaises(Death):
                        installer.restore(home)
                installer.recover(home)
                for _, target in installer.COMPONENTS:
                    self.assertEqual(installer._tree_sha256(home / target), expected[target])

    def test_interrupted_uninstall_is_recoverable_after_every_move_boundary(self):
        installer = load_installer()
        class Death(BaseException):
            pass
        for boundary in range(1, 4):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                home = self._home(Path(tmp))
                installer.install(ROOT, home, upgrade=False)
                expected = {target: installer._tree_sha256(home / target) for _, target in installer.COMPONENTS}
                real_move = installer._move_path
                calls = 0
                def dying_move(source, destination):
                    nonlocal calls
                    calls += 1
                    result = real_move(source, destination)
                    if calls == boundary:
                        raise Death()
                    return result
                with mock.patch.object(installer, "_move_path", side_effect=dying_move):
                    with self.assertRaises(Death):
                        installer.uninstall(home)
                installer.recover(home)
                for _, target in installer.COMPONENTS:
                    self.assertEqual(installer._tree_sha256(home / target), expected[target])

    def test_uninstall_backup_cleanup_failure_remains_recoverable(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(Path(tmp))
            self._write_existing_components(installer, home)
            installer.install(ROOT, home, upgrade=True)
            real_remove = installer._remove_path
            failed = False
            def fail_backup(path):
                nonlocal failed
                if ".backup-" in path.name and not failed:
                    failed = True
                    raise OSError("backup cleanup failure")
                return real_remove(path)
            with mock.patch.object(installer, "_remove_path", side_effect=fail_backup):
                with self.assertRaisesRegex(OSError, "backup cleanup failure"):
                    installer.uninstall(home)
            self.assertTrue((home / ".my-journal" / "install-transaction.json").exists())
            installer.recover(home)
            self.assertFalse((home / ".my-journal" / "install-transaction.json").exists())

    def test_restore_success_keeps_managed_state_for_later_uninstall(self):
        installer = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = self._home(Path(tmp))
            self._write_existing_components(installer, home)
            installer.install(ROOT, home, upgrade=True)
            installer.restore(home)
            self.assertTrue((home / ".my-journal" / "install-state.json").is_file())
            removed = installer.uninstall(home)
            self.assertEqual(len(removed), len(installer.COMPONENTS))


if __name__ == "__main__":
    unittest.main()
