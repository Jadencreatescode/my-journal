from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
DEMO_PATH = ROOT / "scripts" / "demo.py"
REQUIRED_HEADINGS = (
    "Overview",
    "Conversation Coverage",
    "Projects and Workstreams",
    "Decisions",
    "Changes and Verification",
    "Completed Work",
    "Blockers and Failures",
    "Corrections and Preference Changes",
    "Open Threads",
    "Context Index",
    "Automation Appendix",
    "Provenance",
)


def load_demo():
    spec = importlib.util.spec_from_file_location("my_journal_synthetic_demo", DEMO_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SyntheticDemoTests(unittest.TestCase):
    def test_builds_a_self_contained_validated_synthetic_journal_tree(self):
        demo = load_demo()
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "example"
            result = demo.generate_demo(destination)

            note_path = destination.resolve() / "journal" / "2026" / "08" / "2026-08-24.md"
            manifest_path = destination / "evidence" / "synthetic-conversations.json"
            receipt_path = destination / "receipts" / "validation.json"
            self.assertEqual(result.note_path, note_path)
            self.assertTrue(note_path.is_file())
            self.assertTrue(manifest_path.is_file())
            self.assertTrue(receipt_path.is_file())

            note = note_path.read_text(encoding="utf-8")
            for heading in REQUIRED_HEADINGS:
                self.assertEqual(note.count(f"## {heading}\n"), 1)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["source"], "embedded-synthetic-conversations")
            self.assertTrue(receipt["validated"])
            self.assertEqual(receipt["validation_errors"], [])
            self.assertEqual(receipt["evidence_sha256"], manifest["evidence_sha256"])
            self.assertNotIn("{{", note)
            self.assertIn("Synthetic demonstration only", note)

    def test_refuses_nonempty_unowned_destination_even_with_overwrite(self):
        demo = load_demo()
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "existing"
            destination.mkdir()
            protected = destination / "keep.txt"
            protected.write_text("keep", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                demo.generate_demo(destination)
            with self.assertRaises(ValueError):
                demo.generate_demo(destination, overwrite=True)
            self.assertEqual(protected.read_text(encoding="utf-8"), "keep")

    def test_overwrite_is_disabled_even_for_a_previous_synthetic_demo(self):
        demo = load_demo()
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "example"
            first = demo.generate_demo(destination)
            first.note_path.write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "overwrite is disabled"):
                demo.generate_demo(destination, overwrite=True)
            self.assertEqual(first.note_path.read_text(encoding="utf-8"), "changed")

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks unavailable")
    def test_symlinked_destination_ancestor_is_rejected_without_external_writes(self):
        demo = load_demo()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            outside = base / "outside"
            outside.mkdir()
            linked = base / "linked"
            try:
                linked.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {exc}")
            with self.assertRaises((ValueError, OSError)):
                demo.generate_demo(linked / "example")
            self.assertEqual(list(outside.iterdir()), [])

    def test_forged_marker_never_authorizes_recursive_deletion(self):
        demo = load_demo()
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "valuable"
            destination.mkdir()
            marker = destination / demo.MARKER_NAME
            marker.write_text(
                json.dumps({"kind": "my-journal-synthetic-demo", "schema_version": 1}),
                encoding="utf-8",
            )
            valuable = destination / "keep.txt"
            valuable.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "overwrite is disabled"):
                demo.generate_demo(destination, overwrite=True)
            self.assertEqual(valuable.read_text(encoding="utf-8"), "keep")

    def test_destination_replacement_after_creation_fails_closed(self):
        demo = load_demo()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            destination = base / "example"
            moved = base / "opened"
            external = base / "external"
            external.mkdir()
            real_write = demo._write_demo_file
            calls = 0

            def replace_after_first(root, descriptor, relative, text):
                nonlocal calls
                real_write(root, descriptor, relative, text)
                calls += 1
                if calls == 1:
                    destination.rename(moved)
                    try:
                        destination.symlink_to(external, target_is_directory=True)
                    except OSError as exc:
                        self.skipTest(f"symlink creation unavailable: {exc}")

            with mock.patch.object(demo, "_write_demo_file", side_effect=replace_after_first):
                with self.assertRaisesRegex(ValueError, "destination changed"):
                    demo.generate_demo(destination)
            self.assertEqual(list(external.iterdir()), [])
            self.assertTrue((moved / demo.MARKER_NAME).is_file())

    def test_module_does_not_discover_private_sources_or_environment(self):
        import ast

        demo = load_demo()
        source = DEMO_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in ([*node.names] if isinstance(node, ast.Import) else [ast.alias(name=node.module or "")])
        }
        self.assertTrue({"sqlite3", "requests", "urllib"}.isdisjoint(imports))
        self.assertNotIn("os.environ", source)
        self.assertNotIn("os.getenv", source)
        self.assertNotIn("Path.home", source)
        self.assertNotIn(".rglob(", source)
        for conversation in demo.SYNTHETIC_CONVERSATIONS:
            self.assertTrue(conversation["conversation"].startswith("synthetic-"))


if __name__ == "__main__":
    unittest.main()
