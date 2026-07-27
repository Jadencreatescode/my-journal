from __future__ import annotations

import importlib.util
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
            self.assertFalse(state_path.exists())

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


if __name__ == "__main__":
    unittest.main()
