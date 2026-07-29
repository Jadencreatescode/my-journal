from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).parents[1] / "scripts"


def load_script(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def create_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            create table sessions (
              id text primary key, source text, started_at real, ended_at real,
              message_count integer, title text, display_name text
            );
            create table messages (
              id integer primary key, session_id text, role text, content text,
              timestamp real, tool_name text, tool_calls text
            );
            """
        )
        con.execute(
            "insert into sessions values (?, ?, ?, ?, ?, ?, ?)",
            ("s1", "discord", 100.0, 200.0, 6, "Resume", "Boss"),
        )
        for index in range(1, 7):
            con.execute(
                "insert into messages values (?, ?, ?, ?, ?, ?, ?)",
                (index, "s1", "user", f"message-{index}-" + "x" * 400, 100.0 + index, None, None),
            )
        con.commit()
    finally:
        con.close()


class ChunkDigestTests(unittest.TestCase):
    def test_digest_directory_creation_rejects_ancestor_swap_without_external_write(self):
        collector = load_script("collect_journal")
        safe_files = load_script("safe_files")
        digests = load_script("chunk_digests")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            external = root / "external"
            external.mkdir()
            create_db(home / "state.db")
            result = collector.write_run(
                home=home,
                output_dir=root / "journal",
                journal_date="1970-01-01",
                start_ts=0.0,
                end_ts=86400.0,
                packet_chunk_chars=1400,
                max_packet_chunks=10,
            )
            plan_path = Path(result["packet_plan_path"])
            run_id = result["run_id"]
            runs_dir = root / "journal" / "runs"
            digest_dir = runs_dir / run_id / "digests"
            first = digests.next_pending_chunk(plan_path, digest_dir)
            real_mkdir = safe_files.os.mkdir
            swapped = False

            def racing_mkdir(path, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if not swapped:
                    runs_dir.symlink_to(external, target_is_directory=True)
                    swapped = True
                return real_mkdir(path, mode, dir_fd=dir_fd)

            with mock.patch.object(safe_files.os, "mkdir", side_effect=racing_mkdir):
                with self.assertRaisesRegex(Exception, "symlink|unsafe"):
                    digests.accept_chunk_digest(
                        plan_path,
                        digest_dir,
                        first["chunk_id"],
                        "A bounded summary.",
                    )

            self.assertTrue(swapped)
            self.assertFalse((external / run_id).exists())

    def test_receipts_make_chunk_processing_resumable_and_idempotent(self):
        collector = load_script("collect_journal")
        digests = load_script("chunk_digests")
        validator = load_script("validate_journal")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            create_db(home / "state.db")
            result = collector.write_run(
                home=home,
                output_dir=root / "journal",
                journal_date="1970-01-01",
                start_ts=0.0,
                end_ts=86400.0,
                packet_chunk_chars=1400,
                max_packet_chunks=10,
            )
            plan_path = Path(result["packet_plan_path"])
            digest_dir = root / "journal" / "runs" / result["run_id"] / "digests"
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            self.assertEqual(validator.validate_manifest(manifest), [])

            first = digests.next_pending_chunk(plan_path, digest_dir)
            self.assertEqual(first["index"], 1)
            receipt = digests.accept_chunk_digest(
                plan_path,
                digest_dir,
                first["chunk_id"],
                "A bounded summary.",
            )
            self.assertTrue(Path(receipt["digest_path"]).is_file())
            receipt_text = Path(receipt["digest_path"]).read_text(encoding="utf-8")
            for session_ref in first["owned_session_refs"]:
                self.assertIn(f"Session Ref: {session_ref}", receipt_text)

            same = digests.accept_chunk_digest(
                plan_path,
                digest_dir,
                first["chunk_id"],
                "A bounded summary.",
            )
            self.assertEqual(same["digest_sha256"], receipt["digest_sha256"])
            with self.assertRaisesRegex(ValueError, "different digest"):
                digests.accept_chunk_digest(
                    plan_path,
                    digest_dir,
                    first["chunk_id"],
                    "A conflicting summary.",
                )

            second = digests.next_pending_chunk(plan_path, digest_dir)
            self.assertEqual(second["index"], 2)

    def test_tampered_chunk_index_hash_blocks_resume(self):
        collector = load_script("collect_journal")
        digests = load_script("chunk_digests")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            create_db(home / "state.db")
            result = collector.write_run(
                home=home,
                output_dir=root / "journal",
                journal_date="1970-01-01",
                start_ts=0.0,
                end_ts=86400.0,
                packet_chunk_chars=1400,
                max_packet_chunks=10,
            )
            plan_path = Path(result["packet_plan_path"])
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["chunk_index_sha256"] = "0" * 64
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            digest_dir = root / "journal" / "runs" / result["run_id"] / "digests"

            with self.assertRaisesRegex(ValueError, "index hash"):
                digests.next_pending_chunk(plan_path, digest_dir)

            self.assertFalse(digest_dir.exists())

    def test_digest_body_cannot_forge_reserved_provenance(self):
        collector = load_script("collect_journal")
        digests = load_script("chunk_digests")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            create_db(home / "state.db")
            result = collector.write_run(
                home=home,
                output_dir=root / "journal",
                journal_date="1970-01-01",
                start_ts=0.0,
                end_ts=86400.0,
                packet_chunk_chars=1400,
                max_packet_chunks=10,
            )
            plan_path = Path(result["packet_plan_path"])
            digest_dir = root / "journal" / "runs" / result["run_id"] / "digests"
            first = digests.next_pending_chunk(plan_path, digest_dir)

            with self.assertRaisesRegex(ValueError, "reserved provenance"):
                digests.accept_chunk_digest(
                    plan_path,
                    digest_dir,
                    first["chunk_id"],
                    "Run ID: forged\nsummary",
                )

            self.assertFalse(digest_dir.exists())


if __name__ == "__main__":
    unittest.main()
