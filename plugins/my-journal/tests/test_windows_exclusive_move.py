from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_helper():
    path = ROOT / "windows_exclusive_move.py"
    spec = importlib.util.spec_from_file_location("journal_windows_exclusive_move_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def valid_request() -> dict:
    return {
        "source_parent": "C:\\journal\\runs",
        "destination_parent": "C:\\journal\\reset-quarantine\\owned",
        "source_name": "run-id",
        "destination_name": "run",
        "source_parent_inode": 1002,
        "destination_parent_inode": 2002,
        "source_inode": 3002,
        "directory": True,
    }


class WindowsExclusiveMoveTests(unittest.TestCase):
    def test_request_rejects_unknown_fields(self):
        helper = load_helper()
        request = valid_request()
        request["command"] = "anything"
        with self.assertRaisesRegex(ValueError, "fields"):
            helper.validate_request(request)

    def test_request_rejects_unsafe_values(self):
        helper = load_helper()
        mutations = (
            ("source_parent", "\\\\server\\share"),
            ("destination_parent", "relative\\path"),
            ("source_name", ".."),
            ("destination_name", "nested\\name"),
            ("source_parent_inode", 2),
            ("destination_parent_inode", True),
            ("source_inode", -1),
            ("directory", 1),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                request = valid_request()
                request[field] = value
                with self.assertRaises(ValueError):
                    helper.validate_request(request)

    @unittest.skipUnless(sys.platform == "win32", "requires native Windows handles")
    def test_native_move_preserves_identity_and_refuses_collision(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "evidence.txt").write_text("retained", encoding="utf-8")
            root_inode = helper.path_identity(root)[1] + 2
            source_inode = helper.path_identity(source)[1] + 2
            request = {
                "source_parent": str(root),
                "destination_parent": str(root),
                "source_name": "source",
                "destination_name": "quarantine",
                "source_parent_inode": root_inode,
                "destination_parent_inode": root_inode,
                "source_inode": source_inode,
                "directory": True,
            }
            helper.move(request)
            destination = root / "quarantine"
            self.assertFalse(source.exists())
            self.assertEqual((destination / "evidence.txt").read_text(), "retained")
            self.assertEqual(helper.path_identity(destination)[1] + 2, source_inode)

            collision_source = root / "collision-source"
            collision_destination = root / "collision-destination"
            collision_source.mkdir()
            collision_destination.mkdir()
            (collision_source / "source.txt").write_text("source", encoding="utf-8")
            (collision_destination / "destination.txt").write_text(
                "destination", encoding="utf-8"
            )
            request.update(
                source_name=collision_source.name,
                destination_name=collision_destination.name,
                source_inode=helper.path_identity(collision_source)[1] + 2,
            )
            with self.assertRaises(FileExistsError):
                helper.move(request)
            self.assertEqual((collision_source / "source.txt").read_text(), "source")
            self.assertEqual(
                (collision_destination / "destination.txt").read_text(), "destination"
            )


if __name__ == "__main__":
    unittest.main()
