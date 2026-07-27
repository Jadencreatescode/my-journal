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
