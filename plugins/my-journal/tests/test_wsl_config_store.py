from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "wsl_config_store.py"


def load_store():
    spec = importlib.util.spec_from_file_location("my_journal_wsl_config_store_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks unavailable")
class WslConfigStoreTests(unittest.TestCase):
    def test_write_and_read_use_descriptor_anchored_store(self):
        store = load_store()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(store, "_validated_root", return_value=root):
                written = store.dispatch(
                    {
                        "operation": "write",
                        "root": "/mnt/c/Hermes",
                        "wsl_hermes_home": "/home/exampleuser/.hermes",
                    }
                )
                read = store.dispatch({"operation": "read", "root": "/mnt/c/Hermes"})
            self.assertEqual(written, {"ok": True})
            self.assertEqual(
                read,
                {"exists": True, "wsl_hermes_home": "/home/exampleuser/.hermes"},
            )

    def test_symlinked_config_parent_is_rejected_with_zero_external_writes(self):
        store = load_store()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "root"
            external = base / "external"
            root.mkdir()
            external.mkdir()
            try:
                (root / ".my-journal").symlink_to(external, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            with mock.patch.object(store, "_validated_root", return_value=root):
                with self.assertRaises((ValueError, OSError)):
                    store.dispatch(
                        {
                            "operation": "write",
                            "root": "/mnt/c/Hermes",
                            "wsl_hermes_home": "/home/exampleuser/.hermes",
                        }
                    )
            self.assertEqual(list(external.iterdir()), [])

    def test_unknown_or_malformed_operations_fail_closed(self):
        store = load_store()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(store, "_validated_root", return_value=root):
                with self.assertRaises(ValueError):
                    store.dispatch({"operation": "delete", "root": "/mnt/c/Hermes"})
                with self.assertRaises(ValueError):
                    store.dispatch(
                        {
                            "operation": "write",
                            "root": "/mnt/c/Hermes",
                            "wsl_hermes_home": "relative",
                        }
                    )


if __name__ == "__main__":
    unittest.main()
