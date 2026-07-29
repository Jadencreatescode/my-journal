from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "safe_files.py"


def load_module():
    spec = importlib.util.spec_from_file_location("safe_files", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SafeFileTests(unittest.TestCase):
    def test_descriptor_path_canonicalizes_standard_macos_root_alias(self):
        module = load_module()

        with mock.patch("sys.platform", "darwin"), mock.patch.object(
            module.os.path,
            "islink",
            side_effect=lambda value: value == "/var",
        ) as islink, mock.patch.object(
            module.os.path,
            "realpath",
            side_effect=lambda value: "/private/var" if value == "/var" else value,
        ) as realpath:
            canonical = module._canonical_descriptor_path(Path("/var/folders/example/journal"))

        self.assertEqual(canonical, Path("/private/var/folders/example/journal"))
        islink.assert_called_once_with("/var")
        realpath.assert_called_once_with("/var")

    def test_mkdir_tree_remains_anchored_when_output_root_is_swapped(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "journal"
            root.mkdir()
            external = base / "external"
            external.mkdir()
            moved = base / "journal-original"
            real_mkdir = os.mkdir
            swapped = False

            def racing_mkdir(path, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if path == "evidence" and dir_fd is not None and not swapped:
                    swapped = True
                    root.rename(moved)
                    root.symlink_to(external, target_is_directory=True)
                return real_mkdir(path, mode, dir_fd=dir_fd)

            with mock.patch.object(module.os, "mkdir", side_effect=racing_mkdir):
                module.safe_mkdir_tree(root, root / "evidence" / "2026" / "07")

            self.assertTrue(swapped)
            self.assertTrue((moved / "evidence" / "2026" / "07").is_dir())
            self.assertEqual(list(external.iterdir()), [])

    def test_unlink_removes_owned_regular_file_but_rejects_symlink(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            owned = root / "owned.txt"
            owned.write_text("owned", encoding="utf-8")
            module.safe_unlink(root, owned)
            self.assertFalse(owned.exists())

            external = Path(tmp) / "external.txt"
            external.write_text("keep", encoding="utf-8")
            link = root / "link.txt"
            link.symlink_to(external)
            with self.assertRaisesRegex(ValueError, "regular file"):
                module.safe_unlink(root, link)
            self.assertEqual(external.read_text(encoding="utf-8"), "keep")

    def test_atomic_write_remains_anchored_when_parent_is_swapped(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "journal"
            parent = root / "state"
            parent.mkdir(parents=True)
            target = parent / "entry.json"
            target.write_text("old", encoding="utf-8")
            external_parent = base / "external"
            external_parent.mkdir()
            external_target = external_parent / "entry.json"
            external_target.write_text("evil", encoding="utf-8")
            real_open = os.open
            swapped = False

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if str(path).startswith(".entry.json.") and dir_fd is not None and not swapped:
                    swapped = True
                    parent.rename(root / "state-original")
                    parent.symlink_to(external_parent, target_is_directory=True)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch.object(module.os, "open", side_effect=racing_open):
                module.safe_atomic_write_text(root, target, "new")

            self.assertTrue(swapped)
            self.assertEqual((root / "state-original" / "entry.json").read_text(encoding="utf-8"), "new")
            self.assertEqual(external_target.read_text(encoding="utf-8"), "evil")

    def test_read_remains_anchored_when_parent_is_swapped_for_symlink(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "journal"
            parent = root / "evidence"
            parent.mkdir(parents=True)
            target = parent / "entry.json"
            target.write_text("safe", encoding="utf-8")
            external_parent = base / "external"
            external_parent.mkdir()
            (external_parent / "entry.json").write_text("evil", encoding="utf-8")

            real_open = os.open
            swapped = False

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if path == "entry.json" and dir_fd is not None and not swapped:
                    swapped = True
                    parent.rename(root / "evidence-original")
                    parent.symlink_to(external_parent, target_is_directory=True)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch.object(module.os, "open", side_effect=racing_open):
                value = module.safe_read_text(root, target, max_bytes=1024)

            self.assertTrue(swapped)
            self.assertEqual(value, "safe")
            self.assertEqual((external_parent / "entry.json").read_text(encoding="utf-8"), "evil")

    def test_read_rejects_final_symlink(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "journal"
            root.mkdir()
            external = base / "external.txt"
            external.write_text("evil", encoding="utf-8")
            target = root / "entry.txt"
            target.symlink_to(external)

            with self.assertRaisesRegex(ValueError, "symlink|regular file"):
                module.safe_read_text(root, target, max_bytes=1024)


if __name__ == "__main__":
    unittest.main()
