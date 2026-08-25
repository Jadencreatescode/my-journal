from __future__ import annotations

import importlib.util
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_public_content.py"


def load_checker():
    spec = importlib.util.spec_from_file_location("my_journal_public_content_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


class PublicContentTests(unittest.TestCase):
    def test_current_source_tree_passes_public_content_check(self):
        checker = load_checker()
        with tempfile.TemporaryDirectory() as tmp:
            copied = Path(tmp) / "source"
            shutil.copytree(
                ROOT,
                copied,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "dist", "build", ".venv", "venv"),
            )
            self.assertGreater(checker.check_public_content(copied), 0)

    def test_high_confidence_secret_and_private_paths_are_rejected(self):
        checker = load_checker()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "unsafe.md").write_text(
                "token ghp_abcdefghijklmnopqrstuvwxyz123456\n"
                "path /opt/data/private\n"
                "home C:\\Users\\realperson\\secrets\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "GitHub token|private absolute|personal Windows"):
                checker.check_public_content(root)

    def test_unapproved_binary_is_rejected(self):
        checker = load_checker()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "payload.bin").write_bytes(b"\x00private")
            with self.assertRaisesRegex(ValueError, "unapproved binary"):
                checker.check_public_content(root)

    def test_png_text_metadata_is_rejected(self):
        checker = load_checker()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "docs/assets/my-journal-social-preview.png"
            target.parent.mkdir(parents=True)
            source = (ROOT / "docs/assets/my-journal-social-preview.png").read_bytes()
            iend = source.rfind(b"\x00\x00\x00\x00IEND")
            self.assertGreater(iend, 0)
            target.write_bytes(source[:iend] + png_chunk(b"tEXt", b"private\x00value") + source[iend:])
            with self.assertRaisesRegex(ValueError, "textual metadata"):
                checker.check_public_content(root)


if __name__ == "__main__":
    unittest.main()
