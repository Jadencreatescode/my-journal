from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    name = "my_journal_security_corrections"
    for key in list(sys.modules):
        if key == name or key.startswith(name + "."):
            del sys.modules[key]
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_ROOT / "__init__.py", submodule_search_locations=[str(PLUGIN_ROOT)]
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ScheduledBindingTests(unittest.TestCase):
    def test_empty_coverage_renders_canonical_nonblank_provenance(self):
        plugin = load_plugin()
        tools = sys.modules[f"{plugin.__name__}.tools"]
        manifest = {
            "journal_date": "2026-08-08", "run_id": "a" * 16,
            "evidence_sha256": "b" * 64,
            "coverage": {
                "database_count": 0, "database_error_count": 0,
                "session_count": 0, "message_count": 0,
                "platforms": [], "profiles": [],
            },
        }
        sections = {key: "None recorded." for key, _heading in tools._SECTION_HEADINGS}
        note = tools._render_note(manifest, sections, Path("manifest.json"), Path("digests"))
        self.assertIn("Platforms: (none)", note.splitlines())
        self.assertIn("Profiles: (none)", note.splitlines())

    def test_precollection_emits_exact_frozen_artifact_digests(self):
        plugin = load_plugin()
        operations = sys.modules[f"{plugin.__name__}.operations"]
        frozen = {
            "binding_id": "b" * 64,
            "run_id": "a" * 16,
            "journal_date": "2026-07-27",
            "receipt_sha256": "1" * 64,
            "manifest_sha256": "2" * 64,
            "packet_plan_sha256": "3" * 64,
        }
        with mock.patch.object(operations, "resolve_date_range", return_value=(__import__("datetime").date(2026, 7, 27), None)), mock.patch.object(
            operations, "_active_scheduled_bindings", return_value=[]
        ), mock.patch.object(
            operations, "_generation_collect", return_value={"ok": True, "run_id": "a" * 16, "journal_date": "2026-07-27", "chunk_count": 2}
        ), mock.patch.object(operations, "_create_scheduled_binding", return_value=frozen):
            result = operations.precollect_daily_generation()
        for key, value in frozen.items():
            self.assertEqual(result[key], value)

    def test_resume_requires_exact_binding_and_is_idempotent(self):
        plugin = load_plugin()
        tools = sys.modules[f"{plugin.__name__}.tools"]
        binding = {
            "schema_version": 1, "status": "active", "binding_id": "b" * 64,
            "run_id": "a" * 16, "journal_date": "2026-07-27",
            "receipt_sha256": "1" * 64, "manifest_sha256": "2" * 64,
            "packet_plan_sha256": "3" * 64,
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"MY_JOURNAL_ROOT": tmp}), mock.patch.object(
            tools, "_load_scheduled_binding", return_value=binding
        ), mock.patch.object(
            tools, "_verify_scheduled_binding", return_value={"run_id": "a" * 16, "journal_date": "2026-07-27"}
        ), mock.patch.object(
            tools, "_collection_summary", return_value={"ok": True, "run_id": "a" * 16, "journal_date": "2026-07-27"}
        ) as summary:
            args = {key: binding[key] for key in ("binding_id", "run_id", "journal_date", "receipt_sha256", "manifest_sha256", "packet_plan_sha256")}
            first = tools._generation_resume(**args)
            second = tools._generation_resume(**args)
        self.assertFalse(first["already_resumed"])
        self.assertTrue(second["already_resumed"])
        summary.assert_called_once()

    def test_resume_claim_rejects_binding_parent_swap_without_external_write(self):
        plugin = load_plugin()
        tools = sys.modules[f"{plugin.__name__}.tools"]
        binding = {
            "schema_version": 1, "status": "active", "binding_id": "b" * 64,
            "run_id": "a" * 16, "journal_date": "2026-07-27",
            "receipt_sha256": "1" * 64, "manifest_sha256": "2" * 64,
            "packet_plan_sha256": "3" * 64,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "journal"
            external = Path(tmp) / "external"
            external.mkdir()
            moved = root / "scheduled-bindings-original"
            real_mkdir = tools._safe_files.safe_mkdir_tree

            def swap_after_create(trusted_root, target):
                real_mkdir(trusted_root, target)
                parent = root / "scheduled-bindings"
                parent.rename(moved)
                parent.symlink_to(external, target_is_directory=True)

            with mock.patch.dict(os.environ, {"MY_JOURNAL_ROOT": str(root)}), mock.patch.object(
                tools, "_load_scheduled_binding", return_value=binding
            ), mock.patch.object(
                tools, "_verify_scheduled_binding", return_value={"run_id": "a" * 16, "journal_date": "2026-07-27"}
            ), mock.patch.object(
                tools, "_collection_summary", return_value={"ok": True, "run_id": "a" * 16, "journal_date": "2026-07-27"}
            ), mock.patch.object(
                tools._safe_files, "safe_mkdir_tree", side_effect=swap_after_create
            ):
                args = {key: binding[key] for key in ("binding_id", "run_id", "journal_date", "receipt_sha256", "manifest_sha256", "packet_plan_sha256")}
                with self.assertRaises((OSError, ValueError)):
                    tools._generation_resume(**args)
            self.assertEqual(list(external.iterdir()), [])

    def test_resume_rejects_digest_mismatch(self):
        plugin = load_plugin()
        tools = sys.modules[f"{plugin.__name__}.tools"]
        binding = {
            "schema_version": 1, "status": "active", "binding_id": "b" * 64,
            "run_id": "a" * 16, "journal_date": "2026-07-27",
            "receipt_sha256": "1" * 64, "manifest_sha256": "2" * 64,
            "packet_plan_sha256": "3" * 64,
        }
        with mock.patch.object(tools, "_load_scheduled_binding", return_value=binding):
            with self.assertRaisesRegex(ValueError, "binding metadata"):
                tools._generation_resume(**{**{key: binding[key] for key in ("binding_id", "run_id", "journal_date", "receipt_sha256", "manifest_sha256", "packet_plan_sha256")}, "manifest_sha256": "9" * 64})

    def test_fresh_collection_refuses_while_active_binding_exists(self):
        plugin = load_plugin()
        tools = sys.modules[f"{plugin.__name__}.tools"]
        with mock.patch.object(tools, "_active_scheduled_binding_for_date", return_value={"binding_id": "b" * 64}):
            with self.assertRaisesRegex(ValueError, "scheduled binding"):
                tools._generation_collect("2026-07-27")

    def test_schedule_create_rejects_linked_missing_ancestor_without_external_directory(self):
        plugin = load_plugin()
        operations = sys.modules[f"{plugin.__name__}.operations"]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "owned" / "journal"
            external = base / "external"
            external.mkdir()
            real_mkdir = operations._safe_files.os.mkdir

            def linked_mkdir(path, mode=0o777, *, dir_fd=None):
                if path == "owned" and dir_fd is not None:
                    (base / "owned").symlink_to(external, target_is_directory=True)
                    return None
                return real_mkdir(path, mode, dir_fd=dir_fd)

            with mock.patch.dict(os.environ, {"MY_JOURNAL_ROOT": str(root)}), mock.patch.object(
                operations._safe_files.os, "mkdir", side_effect=linked_mkdir
            ), mock.patch.object(operations, "_create_cron_job") as create_job:
                with self.assertRaises((OSError, ValueError)):
                    operations.schedule_create("0 11 * * *", "local")
            create_job.assert_not_called()
            self.assertEqual(list(external.iterdir()), [])

    def test_completion_requires_binding_for_bound_run(self):
        plugin = load_plugin()
        tools = sys.modules[f"{plugin.__name__}.tools"]
        with mock.patch.object(tools, "_active_scheduled_binding_for_run", return_value={"binding_id": "b" * 64, "journal_date": "2026-07-27"}):
            with self.assertRaisesRegex(ValueError, "binding_id"):
                tools._generation_complete("a" * 16, "2026-07-27", {}, None)

    def test_postvalidation_accepts_only_exact_completed_frozen_run(self):
        plugin = load_plugin()
        operations = sys.modules[f"{plugin.__name__}.operations"]
        binding = {"binding_id": "b" * 64, "run_id": "a" * 16, "journal_date": "2026-07-27"}
        with mock.patch.object(
            operations, "_active_scheduled_bindings", return_value=[binding]
        ), mock.patch.object(operations, "_postvalidate_scheduled_binding", return_value={**binding, "ok": True}):
            result = operations.postvalidate_daily_generation()
        self.assertTrue(result["ok"])
        self.assertEqual(result["run_id"], "a" * 16)

    def test_postvalidation_uses_frozen_binding_date_across_midnight(self):
        plugin = load_plugin()
        operations = sys.modules[f"{plugin.__name__}.operations"]
        binding = {"binding_id": "b" * 64, "run_id": "a" * 16, "journal_date": "2026-08-08"}
        with mock.patch.object(operations, "_active_scheduled_bindings", return_value=[binding]), mock.patch.object(
            operations, "_postvalidate_scheduled_binding", return_value={**binding, "ok": True}
        ), mock.patch.object(operations, "resolve_date_range") as resolve:
            result = operations.postvalidate_daily_generation()
        self.assertEqual(result["journal_date"], "2026-08-08")
        resolve.assert_not_called()


class CronMutationOwnershipTests(unittest.TestCase):
    def test_mutated_pending_job_requires_force_and_is_removed_before_evidence(self):
        plugin = load_plugin()
        operations = sys.modules[f"{plugin.__name__}.operations"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = "c" * 48
            spec = operations._cron_job_spec("0 11 * * *", "local", token)
            (root / "cron-job-intent.json").write_text(json.dumps({
                "schema_version": 1,
                "ownership_token": token,
                "job_spec": spec,
                "normalized_schedule": operations._normalize_cron_schedule(spec["schedule"]),
                "pending_job_id": "leaked",
            }))
            mutated = {"id": "leaked", **spec, "enabled_toolsets": ["terminal"]}
            with mock.patch.dict(os.environ, {"MY_JOURNAL_ROOT": tmp}), mock.patch.object(
                operations, "_list_cron_jobs", return_value=[mutated]
            ), mock.patch.object(operations, "_remove_cron_job", return_value=True) as remove:
                refused = operations.schedule_remove()
                self.assertTrue((root / "cron-job-intent.json").exists())
                forced = operations.schedule_remove(
                    force_job_id="leaked",
                    confirmation="FORCE REMOVE MY JOURNAL CRON leaked",
                )
            self.assertFalse(refused["ok"])
            self.assertTrue(forced["ok"])
            self.assertTrue(forced["removed"])
            remove.assert_called_once_with("leaked")
            self.assertFalse((root / "cron-job-intent.json").exists())

    def test_pending_removal_failure_retains_all_ownership_evidence(self):
        plugin = load_plugin()
        operations = sys.modules[f"{plugin.__name__}.operations"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = "c" * 48
            spec = operations._cron_job_spec("0 11 * * *", "local", token)
            intent = {
                "schema_version": 1,
                "ownership_token": token,
                "job_spec": spec,
                "normalized_schedule": operations._normalize_cron_schedule(spec["schedule"]),
                "pending_job_id": "leaked",
            }
            (root / "cron-job-intent.json").write_text(json.dumps(intent))
            exact = {"id": "leaked", **spec, "schedule": intent["normalized_schedule"]}
            with mock.patch.dict(os.environ, {"MY_JOURNAL_ROOT": tmp}), mock.patch.object(
                operations, "_list_cron_jobs", return_value=[exact]
            ), mock.patch.object(operations, "_remove_cron_job", return_value=False):
                result = operations.schedule_remove()
            self.assertFalse(result["ok"])
            self.assertTrue((root / "cron-job-intent.json").exists())
    def test_failed_rollback_retains_pending_job_and_blocks_duplicate_create(self):
        plugin = load_plugin()
        operations = sys.modules[f"{plugin.__name__}.operations"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = "c" * 48
            spec = operations._cron_job_spec("0 11 * * *", "local", token)
            (root / "cron-job-intent.json").write_text(json.dumps({
                "schema_version": 1,
                "ownership_token": token,
                "job_spec": spec,
                "normalized_schedule": operations._normalize_cron_schedule(spec["schedule"]),
                "pending_job_id": "leaked",
            }))
            renamed = {"id": "renamed", **spec, "enabled_toolsets": ["terminal"]}
            with mock.patch.dict(os.environ, {"MY_JOURNAL_ROOT": tmp}), mock.patch.object(
                operations, "_list_cron_jobs", return_value=[renamed]
            ), mock.patch.object(operations, "_remove_cron_job", return_value=True) as remove:
                refused = operations.schedule_remove()
                self.assertTrue((root / "cron-job-intent.json").exists())
                forced = operations.schedule_remove(
                    force_job_id="renamed",
                    confirmation="FORCE REMOVE MY JOURNAL CRON renamed",
                )
            self.assertFalse(refused["ok"])
            self.assertEqual(refused["job_id"], "renamed")
            self.assertTrue(forced["ok"])
            remove.assert_called_once_with("renamed")
            self.assertFalse((root / "cron-job-intent.json").exists())

    def test_failed_rollback_retains_pending_job_and_blocks_duplicate_create_original_case(self):
        plugin = load_plugin()
        operations = sys.modules[f"{plugin.__name__}.operations"]
        with tempfile.TemporaryDirectory() as tmp:
            token_spec = None

            def create(**spec):
                nonlocal token_spec
                token_spec = spec
                return {"id": "a1b2c3d4e5f6"}

            def listed(*, include_disabled=True):
                if token_spec is None:
                    return []
                return [{"id": "a1b2c3d4e5f6", **token_spec, "enabled_toolsets": ["terminal"]}]

            with mock.patch.dict(os.environ, {"MY_JOURNAL_ROOT": tmp}), mock.patch.object(
                operations, "_create_cron_job", side_effect=create
            ) as create_job, mock.patch.object(
                operations, "_list_cron_jobs", side_effect=listed
            ), mock.patch.object(operations, "_remove_cron_job", return_value=False):
                first = operations.schedule_create("0 11 * * *", "local")
                second = operations.schedule_create("0 11 * * *", "local")

            self.assertFalse(first["ok"])
            self.assertFalse(second["ok"])
            self.assertIn("pending", second["error"])
            create_job.assert_called_once()
            intent = json.loads((Path(tmp) / "cron-job-intent.json").read_text())
            self.assertEqual(intent["pending_job_id"], "a1b2c3d4e5f6")

    def _records(self, root: Path, operations, spec: dict):
        token = "c" * 48
        (root / "cron-job-intent.json").write_text(json.dumps({
            "schema_version": 1, "ownership_token": token, "job_spec": spec,
            "normalized_schedule": operations._normalize_cron_schedule(spec["schedule"]),
        }))
        (root / "cron-job.json").write_text(json.dumps({
            "schema_version": 1, "job_id": "owned", "ownership_token": token, "job_spec": spec,
        }))

    def test_every_mutated_owned_field_blocks_setup_without_duplication(self):
        plugin = load_plugin()
        operations = sys.modules[f"{plugin.__name__}.operations"]
        token = "c" * 48
        spec = operations._cron_job_spec("0 11 * * *", "local", token)
        mutations = {
            "name": "changed", "prompt": "changed", "deliver": "changed",
            "skills": [], "enabled_toolsets": ["terminal"], "script": "other.py",
            "required_prerun": False, "required_postrun": False,
            "post_script": "other.py", "schedule": "5 5 * * *",
        }
        for field, value in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self._records(root, operations, spec)
                job = {"id": "owned", **spec, field: value}
                with mock.patch.dict(os.environ, {"MY_JOURNAL_ROOT": tmp}), mock.patch.object(
                    operations, "_list_cron_jobs", return_value=[job]
                ), mock.patch.object(operations, "_create_cron_job") as create:
                    result = operations.schedule_create("0 11 * * *", "local")
                self.assertFalse(result["ok"])
                self.assertIn("mutated", result["error"])
                create.assert_not_called()
                self.assertTrue((root / "cron-job.json").exists())

    def test_mutated_owned_job_requires_explicit_force_removal(self):
        plugin = load_plugin()
        operations = sys.modules[f"{plugin.__name__}.operations"]
        token = "c" * 48
        spec = operations._cron_job_spec("0 11 * * *", "local", token)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._records(root, operations, spec)
            mutated = {"id": "owned", **spec, "enabled_toolsets": ["terminal"]}
            with mock.patch.dict(os.environ, {"MY_JOURNAL_ROOT": tmp}), mock.patch.object(
                operations, "_list_cron_jobs", return_value=[mutated]
            ), mock.patch.object(operations, "_remove_cron_job", return_value=True) as remove:
                refused = operations.schedule_remove()
                forced = operations.schedule_remove(force_job_id="owned", confirmation="FORCE REMOVE MY JOURNAL CRON owned")
            self.assertFalse(refused["ok"])
            self.assertTrue(forced["ok"])
            remove.assert_called_once_with("owned")


if __name__ == "__main__":
    unittest.main()
