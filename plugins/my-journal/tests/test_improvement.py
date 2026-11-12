from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[1]
VALIDATOR = REPOSITORY / "skills" / "note-taking" / "my-journal" / "scripts" / "validate_journal.py"
SECTION_KEYS = (
    "overview",
    "conversation_coverage",
    "projects_and_workstreams",
    "decisions",
    "changes_and_verification",
    "completed_work",
    "blockers_and_failures",
    "corrections_and_preference_changes",
    "open_threads",
    "context_index",
    "automation_appendix",
)


def load_plugin():
    package_name = f"journal_improvement_{os.getpid()}_{time.time_ns()}"
    spec = importlib.util.spec_from_file_location(
        package_name,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return module


def load_validator():
    spec = importlib.util.spec_from_file_location("improvement_test_validator", VALIDATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(self):
        self.tools = {}

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_cli_command(self, **kwargs):
        self.cli = kwargs


@unittest.skipIf(sys.platform == "win32", "Linux runtime tools intentionally require WSL")
class ImprovementTestCase(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.tools = sys.modules[f"{self.plugin.__name__}.tools"]
        self.improvement = importlib.import_module(f"{self.plugin.__name__}.improvement")
        self.validator = load_validator()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "journal"
        self.root.mkdir()
        self._env = mock.patch.dict(
            os.environ,
            {
                "MY_JOURNAL_ROOT": str(self.root),
                "MY_JOURNAL_VALIDATOR": str(VALIDATOR),
                "MY_JOURNAL_TIMEZONE": "UTC",
            },
            clear=False,
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def write_note(self, day: str, *, blockers: str = "None", corrections: str = "None") -> Path:
        """Render a synthetic note through the real renderer and real validator contract."""
        manifest = {
            "schema_version": 1,
            "journal_date": day,
            "created_at": f"{day}T23:00:00+00:00",
            "window": {"start_ts": 100.0, "end_ts": 200.0},
            "coverage": {
                "database_count": 0,
                "database_error_count": 0,
                "session_count": 0,
                "message_count": 0,
                "retained_message_count": 0,
                "platforms": [],
                "profiles": [],
            },
            "databases": [],
            "sessions": [],
        }
        manifest["evidence_sha256"] = self.validator.canonical_evidence_sha256(manifest)
        manifest["run_id"] = manifest["evidence_sha256"][:16]
        year, month, _ = day.split("-")
        run_id = manifest["run_id"]
        manifest_path = self.root / "evidence" / year / month / f"{day}-{run_id}.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        digest_dir = self.root / "runs" / run_id / "digests"
        digest_dir.mkdir(parents=True, exist_ok=True)
        sections = {key: "Synthetic placeholder text." for key in SECTION_KEYS}
        sections["blockers_and_failures"] = blockers
        sections["corrections_and_preference_changes"] = corrections
        note = self.tools._render_note(manifest, sections, manifest_path, digest_dir)
        note_path = self.root / "notes" / year / month / f"{day}.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(note, encoding="utf-8")
        return note_path

    def notes_snapshot(self) -> dict[str, str]:
        notes = self.root / "notes"
        if not notes.exists():
            return {}
        return {
            str(path.relative_to(notes)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(notes.rglob("*"))
            if path.is_file()
        }

    def call(self, handler_name: str, args: dict) -> dict:
        return json.loads(getattr(self.tools, handler_name)(args))


class RecurringIssueTests(ImprovementTestCase):
    def test_detects_issue_recurring_on_three_dates_with_citations(self):
        self.write_note("2026-07-01", blockers="- Deploy failed because the cache was stale.\n- Login page timeout on mobile.")
        self.write_note("2026-07-03", blockers="1. Deployment failed again: stale cache.")
        self.write_note("2026-07-05", corrections="* The stale cache made the deploy fail, so cache clearing is now required.")
        self.write_note("2026-07-06", blockers="- None")

        result = self.call("handle_recurring_issues", {"range": "2026-07-01 to 2026-07-06"})

        self.assertNotIn("error", result)
        self.assertIn("candidate recurring issues for review", result["notice"].lower())
        self.assertEqual(len(result["groups"]), 1)
        group = result["groups"][0]
        self.assertEqual(group["distinct_date_count"], 3)
        self.assertIn("cache", group["label"])
        self.assertIn("stale", group["label"])
        self.assertEqual(
            [(item["date"], item["section"]) for item in group["citations"]],
            [
                ("2026-07-01", "blockers_and_failures"),
                ("2026-07-03", "blockers_and_failures"),
                ("2026-07-05", "corrections_and_preference_changes"),
            ],
        )
        self.assertTrue(all(len(item["excerpt"]) <= self.improvement.MAX_EXCERPT_CHARS for item in group["citations"]))
        self.assertFalse(group["citations_truncated"])
        self.assertFalse(result["groups_truncated"])
        self.assertEqual(result["coverage"]["validated_count"], 4)
        self.assertEqual(result["coverage"]["missing_dates"], ["2026-07-02", "2026-07-04"])

    def test_unrelated_items_are_not_merged(self):
        for day in ("2026-07-01", "2026-07-02", "2026-07-03"):
            self.write_note(
                day,
                blockers="- Deploy failed because the cache was stale.\n- Login page timeout on mobile devices.",
            )
        result = self.call("handle_recurring_issues", {"start_date": "2026-07-01", "end_date": "2026-07-03"})
        self.assertEqual(len(result["groups"]), 2)
        for group in result["groups"]:
            self.assertEqual(group["distinct_date_count"], 3)
            excerpts = {item["excerpt"] for item in group["citations"]}
            self.assertEqual(len(excerpts), 1)
        labels = sorted(group["label"] for group in result["groups"])
        self.assertTrue(any("login" in label for label in labels))
        self.assertTrue(any("cache" in label for label in labels))

    def test_min_dates_boundary_and_placeholders(self):
        self.write_note("2026-07-01", blockers="- Flaky integration tests timed out.\n- None")
        self.write_note("2026-07-02", blockers="- Integration tests flaky and timing out.\n- N/A")
        self.write_note("2026-07-03", blockers="None", corrections="No corrections.")

        default = self.call("handle_recurring_issues", {"range": "2026-07-01 to 2026-07-03"})
        self.assertEqual(default["min_dates"], 3)
        self.assertEqual(default["groups"], [])
        self.assertEqual(default["item_count"], 2)

        two = self.call("handle_recurring_issues", {"range": "2026-07-01 to 2026-07-03", "min_dates": 2})
        self.assertEqual(len(two["groups"]), 1)
        self.assertEqual(two["groups"][0]["distinct_date_count"], 2)

        one = self.call("handle_recurring_issues", {"range": "2026-07-01 to 2026-07-03", "min_dates": 1})
        self.assertIn("error", one)

    def test_unvalidated_and_tampered_notes_are_excluded(self):
        for day in ("2026-07-01", "2026-07-02"):
            self.write_note(day, blockers="- Backup job failed on the full disk.")
        tampered = self.write_note("2026-07-03", blockers="- Backup job failed on the full disk.")
        text = tampered.read_text(encoding="utf-8")
        tampered.write_text(text.replace("Evidence SHA256: ", "Evidence SHA256: 0"), encoding="utf-8")
        raw = self.root / "notes" / "2026" / "07" / "2026-07-04.md"
        raw.write_text(
            "# My Journal: 2026-07-04\n\n## Blockers and Failures\n\n- Backup job failed on the full disk.\n",
            encoding="utf-8",
        )

        result = self.call("handle_recurring_issues", {"range": "2026-07-01 to 2026-07-04", "min_dates": 2})

        coverage = result["coverage"]
        self.assertEqual(coverage["validated_dates"], ["2026-07-01", "2026-07-02"])
        self.assertEqual([item["date"] for item in coverage["excluded"]], ["2026-07-03", "2026-07-04"])
        self.assertTrue(all(item["errors"] for item in coverage["excluded"]))
        self.assertEqual(len(result["groups"]), 1)
        cited = [item["date"] for item in result["groups"][0]["citations"]]
        self.assertEqual(cited, ["2026-07-01", "2026-07-02"])

        strict = self.call("handle_recurring_issues", {"range": "2026-07-01 to 2026-07-04"})
        self.assertEqual(strict["groups"], [])

    def test_missing_dates_are_reported(self):
        self.write_note("2026-07-02", blockers="- Something broke.")
        result = self.call("handle_recurring_issues", {"range": "2026-07-01 to 2026-07-04"})
        self.assertEqual(result["coverage"]["calendar_days"], 4)
        self.assertEqual(result["coverage"]["missing_dates"], ["2026-07-01", "2026-07-03", "2026-07-04"])
        self.assertEqual(result["coverage"]["missing_count"], 3)

    def test_output_is_bounded_and_flags_truncation(self):
        issues = (
            "- Deploy failed because the cache was stale.",
            "- Login page timeout on mobile devices.",
            "- Flaky integration tests timed out.",
            "- Backup job failed on the full disk.",
            "- Calendar sync dropped recurring events.",
        )
        start = date(2026, 7, 1)
        for offset in range(12):
            day = (start + timedelta(days=offset)).isoformat()
            body = "\n".join(issues) if offset < 3 else issues[0]
            self.write_note(day, blockers=body)

        result = self.call(
            "handle_recurring_issues",
            {"range": "2026-07-01 to 2026-07-12", "max_groups": 2, "max_citations": 4},
        )

        self.assertEqual(len(result["groups"]), 2)
        self.assertTrue(result["groups_truncated"])
        self.assertEqual(result["candidate_group_count"], 5)
        top = result["groups"][0]
        self.assertEqual(top["distinct_date_count"], 12)
        self.assertEqual(len(top["citations"]), 4)
        self.assertTrue(top["citations_truncated"])
        self.assertEqual([item["date"] for item in top["citations"]], ["2026-07-09", "2026-07-10", "2026-07-11", "2026-07-12"])

        rejected = self.call("handle_recurring_issues", {"range": "2026-07-01 to 2026-07-12", "max_groups": 500})
        self.assertIn("error", rejected)
        too_long = self.call("handle_recurring_issues", {"range": "2024-01-01 to 2026-07-12"})
        self.assertIn("error", too_long)

    def test_output_is_deterministic(self):
        for day in ("2026-07-01", "2026-07-02", "2026-07-03"):
            self.write_note(
                day,
                blockers="- Login page timeout on mobile devices.\n- Deploy failed because the cache was stale.\n- Flaky integration tests timed out.",
                corrections="- User asked for shorter release notes.",
            )
        args = {"range": "2026-07-01 to 2026-07-03"}
        first = self.tools.handle_recurring_issues(dict(args))
        second = self.tools.handle_recurring_issues(dict(args))
        self.assertEqual(first, second)
        self.assertEqual(len(json.loads(first)["groups"]), 4)

    def test_normalization_groups_suffix_variants(self):
        left = self.improvement.significant_terms("Deployments failed because caches were stale")
        right = self.improvement.significant_terms("The deploy is failing; stale cache.")
        self.assertEqual(left, right)
        self.assertNotIn("the", right)
        self.assertGreaterEqual(self.improvement.jaccard(left, right), self.improvement.SIMILARITY_THRESHOLD)


class LessonLedgerTests(ImprovementTestCase):
    def adopt(self, **overrides) -> dict:
        args = {
            "title": "Clear the build cache before deploys",
            "adopted_on": "2026-07-15",
            "any_terms": ["stale cache"],
        }
        args.update(overrides)
        return self.call("handle_lesson_record", args)

    def ledger(self) -> Path:
        return self.root / "improvements" / "lessons.jsonl"

    def test_append_and_duplicate_refusal(self):
        first = self.adopt(all_terms=["deploy"], sections=["blockers_and_failures"], source_dates=["2026-07-01"])
        self.assertTrue(first["ok"], first)
        self.assertRegex(first["lesson_id"], r"^[0-9a-f]{16}$")
        before = self.ledger().read_bytes()
        record = json.loads(before.decode("utf-8").splitlines()[0])
        self.assertEqual(record["schema_version"], 1)
        self.assertEqual(record["event"], "adopt")
        self.assertEqual(record["adopted_on"], "2026-07-15")
        self.assertEqual(record["match"], {"any_terms": ["stale cache"], "all_terms": ["deploy"]})
        self.assertTrue(record["recorded_at"].endswith("Z"))
        self.assertFalse((self.root / "notes" / "improvements").exists())

        duplicate = self.adopt(title="  clear the BUILD cache   before deploys ")
        self.assertIn("error", duplicate)
        self.assertIn("duplicate", duplicate["error"])
        self.assertEqual(self.ledger().read_bytes(), before)

        second = self.adopt(title="Pin the browser version", any_terms=["browser crash"])
        self.assertTrue(second["ok"])
        after = self.ledger().read_bytes()
        self.assertTrue(after.startswith(before))
        self.assertEqual(len(after.decode("utf-8").splitlines()), 2)

    def test_malformed_future_or_invalid_input_is_refused(self):
        tomorrow = (date.today() + timedelta(days=2)).isoformat()
        cases = (
            {"adopted_on": tomorrow},
            {"adopted_on": "2026-02-30"},
            {"adopted_on": "yesterday"},
            {"any_terms": []},
            {"any_terms": ["the and of"]},
            {"title": ""},
            {"title": "x" * 500},
            {"any_terms": ["cache"] * 50},
            {"sections": ["overview"]},
            {"source_dates": [tomorrow]},
            {"unexpected": True},
            {"action": "rewrite"},
        )
        for overrides in cases:
            with self.subTest(overrides=list(overrides)):
                result = self.adopt(**overrides)
                self.assertIn("error", result)
        self.assertFalse(self.ledger().exists())

    def test_retire_is_a_new_append_only_event(self):
        adopted = self.adopt()
        before = self.ledger().read_bytes()
        retired = self.call(
            "handle_lesson_record",
            {"action": "retire", "lesson_id": adopted["lesson_id"], "retired_on": "2026-07-20", "reason": "Superseded."},
        )
        self.assertTrue(retired["ok"], retired)
        after = self.ledger().read_bytes()
        self.assertTrue(after.startswith(before))
        lines = [json.loads(line) for line in after.decode("utf-8").splitlines()]
        self.assertEqual([line["event"] for line in lines], ["adopt", "retire"])

        again = self.call("handle_lesson_record", {"action": "retire", "lesson_id": adopted["lesson_id"], "retired_on": "2026-07-21"})
        self.assertIn("error", again)
        unknown = self.call("handle_lesson_record", {"action": "retire", "lesson_id": "0" * 16, "retired_on": "2026-07-21"})
        self.assertIn("error", unknown)
        early = self.adopt(title="Second lesson")
        before_adoption = self.call("handle_lesson_record", {"action": "retire", "lesson_id": early["lesson_id"], "retired_on": "2026-07-01"})
        self.assertIn("error", before_adoption)

        report = self.call("handle_improvement_report", {"lesson_id": adopted["lesson_id"], "as_of": "2026-07-28"})
        self.assertEqual(report["lessons"][0]["status"], "retired")
        self.assertEqual(report["lessons"][0]["windows"]["since"]["end_date"], "2026-07-20")

    def test_symlinked_ledger_or_directory_is_refused(self):
        outside = Path(self._tmp.name) / "outside.jsonl"
        outside.write_text("", encoding="utf-8")
        improvements = self.root / "improvements"
        improvements.mkdir()
        self.ledger().symlink_to(outside)
        self.assertIn("error", self.adopt())
        self.assertIn("error", self.call("handle_improvement_report", {"as_of": "2026-07-28"}))
        self.assertEqual(outside.read_text(encoding="utf-8"), "")

        self.ledger().unlink()
        improvements.rmdir()
        elsewhere = Path(self._tmp.name) / "elsewhere"
        elsewhere.mkdir()
        improvements.symlink_to(elsewhere, target_is_directory=True)
        self.assertIn("error", self.adopt())
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_malformed_ledger_fails_closed(self):
        self.adopt()
        with self.ledger().open("a", encoding="utf-8") as handle:
            handle.write("{not json}\n")
        self.assertIn("error", self.call("handle_improvement_report", {"as_of": "2026-07-28"}))
        self.assertIn("error", self.adopt(title="Another lesson"))


class ImprovementReportTests(ImprovementTestCase):
    def build_month(self) -> None:
        start = date(2026, 7, 1)
        before_cache = {1, 3, 5, 8, 11}
        since_cache = {20}
        before_login = {2, 9, 12}
        before_flaky = {4}
        since_flaky = {16, 22, 27}
        for offset in range(28):
            day_number = offset + 1
            items = []
            if day_number in before_cache | since_cache:
                items.append("- Deploy failed because the cache was stale.")
            if day_number in before_login:
                items.append("- Login page timeout on mobile devices.")
            if day_number in before_flaky | since_flaky:
                items.append("- Flaky integration tests timed out.")
            self.write_note((start + timedelta(days=offset)).isoformat(), blockers="\n".join(items) or "None")

    def record(self, title: str, terms: list[str], adopted_on: str = "2026-07-15") -> str:
        result = self.call("handle_lesson_record", {"title": title, "adopted_on": adopted_on, "any_terms": terms})
        self.assertTrue(result["ok"], result)
        return result["lesson_id"]

    def test_before_and_since_counts_and_verdicts(self):
        self.build_month()
        cache = self.record("Clear the build cache", ["stale cache"])
        login = self.record("Raise the mobile login timeout", ["login timeout"])
        flaky = self.record("Quarantine flaky tests", ["flaky test"])
        snapshot = self.notes_snapshot()

        report = self.call("handle_improvement_report", {"as_of": "2026-07-28"})

        self.assertNotIn("error", report)
        self.assertIn("not proof of cause", report["notice"])
        lessons = {item["lesson_id"]: item for item in report["lessons"]}
        since = lessons[cache]["windows"]["since"]
        before = lessons[cache]["windows"]["before"]
        self.assertEqual((since["start_date"], since["end_date"], since["days"]), ("2026-07-15", "2026-07-28", 14))
        self.assertEqual((before["start_date"], before["end_date"], before["days"]), ("2026-07-01", "2026-07-14", 14))
        self.assertEqual(before["validated_count"], 14)
        self.assertEqual(since["validated_count"], 14)
        self.assertEqual(before["matching_date_count"], 5)
        self.assertEqual(since["matching_date_count"], 1)
        self.assertEqual(lessons[cache]["verdict"], "reduced")
        self.assertEqual([item["date"] for item in since["citations"]], ["2026-07-20"])
        self.assertEqual(lessons[login]["verdict"], "no recurrence observed since adoption")
        self.assertEqual(lessons[login]["windows"]["before"]["matching_date_count"], 3)
        self.assertEqual(lessons[flaky]["verdict"], "not improved")
        self.assertEqual(lessons[flaky]["windows"]["since"]["matching_date_count"], 3)
        self.assertTrue(all("not proof of cause" in item["interpretation"] for item in report["lessons"]))
        self.assertEqual(self.notes_snapshot(), snapshot)

        single = self.call("handle_improvement_report", {"lesson_id": cache, "as_of": "2026-07-28"})
        self.assertEqual([item["lesson_id"] for item in single["lessons"]], [cache])
        self.assertIn("error", self.call("handle_improvement_report", {"lesson_id": "f" * 16, "as_of": "2026-07-28"}))

    def test_insufficient_evidence_and_window_cap(self):
        self.build_month()
        recent = self.record("Recent lesson", ["stale cache"], adopted_on="2026-07-25")
        old = self.record("Old lesson", ["stale cache"], adopted_on="2026-01-01")
        report = self.call("handle_improvement_report", {"as_of": "2026-07-28"})
        lessons = {item["lesson_id"]: item for item in report["lessons"]}
        self.assertEqual(lessons[recent]["verdict"], "insufficient evidence")
        self.assertEqual(lessons[recent]["windows"]["since"]["validated_count"], 4)
        self.assertEqual(lessons[old]["windows"]["since"]["days"], self.improvement.WINDOW_CAP_DAYS)
        self.assertEqual(lessons[old]["windows"]["before"]["days"], self.improvement.WINDOW_CAP_DAYS)
        self.assertEqual(lessons[old]["windows"]["since"]["end_date"], "2026-07-28")
        self.assertEqual(lessons[old]["windows"]["before"]["end_date"], "2025-12-31")
        self.assertEqual(lessons[old]["verdict"], "insufficient evidence")
        future = self.call("handle_improvement_report", {"as_of": (date.today() + timedelta(days=3)).isoformat()})
        self.assertIn("error", future)

    def test_empty_ledger_reports_no_lessons(self):
        report = self.call("handle_improvement_report", {"as_of": "2026-07-28"})
        self.assertEqual(report["lessons"], [])
        self.assertFalse((self.root / "improvements").exists())

    def test_notes_are_byte_identical_after_every_tool(self):
        self.build_month()
        snapshot = self.notes_snapshot()
        self.call("handle_recurring_issues", {"range": "2026-07-01 to 2026-07-28"})
        lesson = self.record("Clear the build cache", ["stale cache"])
        self.call("handle_improvement_report", {"as_of": "2026-07-28"})
        self.call("handle_lesson_record", {"action": "retire", "lesson_id": lesson, "retired_on": "2026-07-27"})
        self.assertEqual(self.notes_snapshot(), snapshot)


class _FakeReader:
    """Synthetic window reader used to exercise verdict and bound logic directly."""

    def __init__(self, *, validated=None, items=None, excluded=None, adopted=None):
        self.validated = validated or (0, 0)
        self.items = items or []
        self.excluded = excluded or []
        self.adopted = adopted

    def __call__(self, root):  # stands in for the _ValidatedReader class
        return self

    def window(self, start, end):
        days = [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]
        if self.adopted is not None:
            count = self.validated[0] if end < self.adopted else self.validated[1]
            items = [item for item in self.items if start.isoformat() <= item["date"] <= end.isoformat()]
        else:
            count, items = len(days), self.items
        return {
            "validated": days[:count],
            "missing": days[count:],
            "excluded": self.excluded,
            "items": items,
            "calendar_days": len(days),
        }


def _item(day: str, terms: set[str]) -> dict:
    frozen = frozenset(terms)
    return {
        "date": day,
        "section": "blockers_and_failures",
        "excerpt": " ".join(sorted(frozen)),
        "terms": frozen,
        "surface": [(term, term) for term in sorted(frozen)],
    }


class ReviewRegressionTests(ImprovementTestCase):
    LESSON = {
        "lesson_id": "0" * 16,
        "title": "Clear stale cache",
        "adopted_on": "2026-06-01",
        "any_terms": ["stale cache"],
        "all_terms": [],
        "sections": ["blockers_and_failures"],
        "source_dates": [],
        "retired_on": None,
    }

    def adopt(self, **overrides) -> dict:
        args = {"title": "Clear the build cache", "adopted_on": "2026-07-15", "any_terms": ["stale cache"]}
        args.update(overrides)
        return self.call("handle_lesson_record", args)

    # S1
    def test_issue_never_seen_before_adoption_is_not_reported_as_improvement(self):
        report = self.improvement._lesson_report(
            _FakeReader(validated=(7, 7), adopted=date(2026, 6, 1)), self.LESSON, date(2026, 11, 12)
        )
        self.assertEqual(report["verdict"], "no baseline occurrences")
        self.assertIn("cannot be assessed", report["verdict_reason"])
        later = _FakeReader(validated=(30, 30), adopted=date(2026, 6, 1), items=[_item("2026-06-10", set(self.improvement.significant_terms("stale cache")))])
        report = self.improvement._lesson_report(later, self.LESSON, date(2026, 6, 30))
        self.assertEqual(report["windows"]["before"]["matching_date_count"], 0)
        self.assertEqual(report["verdict"], "no baseline occurrences")
        thin = self.improvement._lesson_report(
            _FakeReader(validated=(6, 30), adopted=date(2026, 6, 1)), self.LESSON, date(2026, 11, 12)
        )
        self.assertEqual(thin["verdict"], "insufficient evidence")
        seen = _FakeReader(validated=(30, 30), adopted=date(2026, 6, 1), items=[_item("2026-05-20", set(self.improvement.significant_terms("stale cache")))])
        self.assertEqual(
            self.improvement._lesson_report(seen, self.LESSON, date(2026, 6, 30))["verdict"],
            "no recurrence observed since adoption",
        )

    # S2
    def test_fifo_ledger_is_refused_without_blocking(self):
        directory = self.root / "improvements"
        directory.mkdir()
        os.mkfifo(directory / "lessons.jsonl")
        code = (
            "import importlib.util, json, sys\n"
            "sys.dont_write_bytecode = True\n"
            f"spec = importlib.util.spec_from_file_location('fifo_probe', {str(ROOT / '__init__.py')!r}, "
            f"submodule_search_locations=[{str(ROOT)!r}])\n"
            "module = importlib.util.module_from_spec(spec); sys.modules['fifo_probe'] = module\n"
            "spec.loader.exec_module(module)\n"
            "tools = sys.modules['fifo_probe.tools']\n"
            "report = json.loads(tools.handle_improvement_report({'as_of': '2026-07-01'}))\n"
            "record = json.loads(tools.handle_lesson_record({'title': 'Fifo lesson', 'adopted_on': '2026-07-01', "
            "'any_terms': ['stale cache']}))\n"
            "print(json.dumps([report, record]))\n"
        )
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            completed = subprocess.run(
                [sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=30, env=env
            )
        except subprocess.TimeoutExpired:
            self.fail("a FIFO at the ledger path blocked the report or the writer")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report, record = json.loads(completed.stdout)
        self.assertIn("error", report)
        self.assertIn("error", record)
        self.assertTrue(stat.S_ISFIFO(os.lstat(directory / "lessons.jsonl").st_mode))

    # M1
    def test_non_list_source_dates_are_refused(self):
        for value in ("", {}, 0, False, "2026-07-01", {"2026-07-01": 1}):
            with self.subTest(value=value):
                self.assertIn("error", self.adopt(source_dates=value))
        self.assertFalse((self.root / "improvements" / "lessons.jsonl").exists())
        self.assertTrue(self.adopt(source_dates=None)["ok"])
        self.assertTrue(self.adopt(title="Second lesson")["ok"])

    # M2
    def test_strict_iso_dates_future_end_and_control_characters_are_refused(self):
        future = (date.today() + timedelta(days=30)).isoformat()
        refused = (
            ("handle_recurring_issues", {"start_date": "20260701", "end_date": "20260703"}),
            ("handle_recurring_issues", {"start_date": "2026-07-01", "end_date": "2026-W27-5"}),
            ("handle_recurring_issues", {"start_date": "2026-07-01", "end_date": future}),
            ("handle_improvement_report", {"as_of": "2026-W27-1"}),
            ("handle_improvement_report", {"as_of": "20260701"}),
        )
        for handler, args in refused:
            with self.subTest(handler=handler, args=args):
                self.assertIn("error", self.call(handler, args))
        for overrides in (
            {"adopted_on": "20260701"},
            {"adopted_on": "2026-W27-1"},
            {"title": "Fix \u202ecache"},
            {"title": "Fix\u200bcache"},
            {"title": "Bell \x07 title"},
            {"any_terms": ["stale \u2066cache"]},
            {"all_terms": ["deploy\x1b"]},
        ):
            with self.subTest(overrides=overrides):
                self.assertIn("error", self.adopt(**overrides))
        ok = self.call("handle_recurring_issues", {"start_date": "2026-07-01", "end_date": "2026-07-03"})
        self.assertNotIn("error", ok)
        self.assertTrue(self.adopt(title="Tab\tand newline\nare whitespace")["ok"])

    # M3
    def test_grouping_index_matches_reference_and_item_count_is_bounded(self):
        import random

        rng = random.Random(1234)
        vocabulary = [f"w{index}" for index in range(30)]
        items = [
            _item((date(2026, 1, 1) + timedelta(days=index // 5)).isoformat(), set(rng.sample(vocabulary, rng.randint(1, 5))))
            for index in range(600)
        ]

        def reference(values):
            seeds, members = [], []
            for item in values:
                best_index, best_score = None, -1.0
                for index, seed in enumerate(seeds):
                    if not self.improvement._similar(seed, item["terms"]):
                        continue
                    score = self.improvement.jaccard(seed, item["terms"])
                    if score > best_score:
                        best_index, best_score = index, score
                if best_index is None:
                    seeds.append(item["terms"])
                    members.append([item])
                else:
                    members[best_index].append(item)
            return members

        to_ids = lambda groups: [[id(item) for item in group] for group in groups]  # noqa: E731
        self.assertEqual(to_ids(self.improvement._cluster(items)), to_ids(reference(items)))

        limit = self.improvement.MAX_ITEMS
        self.assertLessEqual(limit, 5000)
        dissimilar = [
            _item((date(2026, 1, 1) + timedelta(days=index % 366)).isoformat(), {f"a{index}x", f"b{index}y", f"c{index}z"})
            for index in range(limit + 500)
        ]
        dissimilar.sort(key=lambda item: item["date"])
        with mock.patch.object(self.improvement, "_ValidatedReader", _FakeReader(items=dissimilar)):
            started = time.perf_counter()
            result = self.improvement.recurring_issues(self.root, date(2026, 1, 1), date(2026, 12, 31))
            elapsed = time.perf_counter() - started
        self.assertEqual(result["item_count"], limit)
        self.assertTrue(result["items_truncated"])
        self.assertEqual(result["max_items"], limit)
        self.assertLess(elapsed, 10.0)

    # M4
    def test_excluded_dates_and_errors_are_bounded_with_flag(self):
        excluded = [
            {"date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(), "errors": ["e" * 500] * 5}
            for index in range(366)
        ]
        with mock.patch.object(self.improvement, "_ValidatedReader", _FakeReader(excluded=excluded)):
            result = self.improvement.recurring_issues(self.root, date(2026, 1, 1), date(2026, 12, 31))
        coverage = result["coverage"]
        self.assertEqual(coverage["excluded_count"], 366)
        self.assertTrue(coverage["excluded_truncated"])
        self.assertEqual(len(coverage["excluded"]), self.improvement.MAX_EXCLUDED_LISTED)
        for entry in coverage["excluded"]:
            self.assertLessEqual(len(entry["errors"]), self.improvement.MAX_EXCLUDED_ERRORS)
            self.assertTrue(all(len(error) <= self.improvement.MAX_EXCLUDED_ERROR_CHARS for error in entry["errors"]))
        self.assertLess(len(json.dumps(coverage["excluded"])), 40_000)
        with mock.patch.object(self.improvement, "_ValidatedReader", _FakeReader(excluded=excluded[:2])):
            small = self.improvement.recurring_issues(self.root, date(2026, 1, 1), date(2026, 1, 31))
        self.assertEqual(len(small["coverage"]["excluded"]), 2)
        self.assertTrue(small["coverage"]["excluded_truncated"])  # error strings were shortened

    # M5
    def test_purge_removes_improvements_directory(self):
        self.assertTrue(self.adopt()["ok"])
        self.assertTrue((self.root / "improvements" / "lessons.jsonl").is_file())
        operations = sys.modules[f"{self.plugin.__name__}.operations"]
        self.assertIn("improvements", operations.PURGE_DIRECTORIES)
        self.assertIn("improvements", operations.purge()["candidates"])
        self.assertTrue((self.root / "improvements").exists())
        result = operations.purge("DELETE MY JOURNAL DATA", apply=True)
        self.assertIn("improvements", result["removed"])
        self.assertFalse((self.root / "improvements").exists())

    def test_durable_append_is_reported_as_success_when_verification_read_differs(self):
        original = self.improvement._read_ledger_text
        calls = []

        def flaky_read(root):
            calls.append(root)
            if len(calls) == 2:
                raise ValueError("lesson ledger path is unsafe")
            return original(root)

        with mock.patch.object(self.improvement, "_read_ledger_text", side_effect=flaky_read):
            result = self.adopt()
        self.assertTrue(result.get("ok"), result)
        self.assertIn("warning", result)
        self.assertEqual(len((self.root / "improvements" / "lessons.jsonl").read_text().splitlines()), 1)
        clean = self.adopt(title="Clean append")
        self.assertTrue(clean["ok"])
        self.assertNotIn("warning", clean)


class RegistrationTests(ImprovementTestCase):
    NEW_TOOLS = {"journal_recurring_issues", "journal_lesson_record", "journal_improvement_report"}
    GENERATION_TOOLS = {
        "journal_generation_collect",
        "journal_generation_resume",
        "journal_generation_get_chunk",
        "journal_generation_record_digest",
        "journal_generation_complete",
    }

    def test_tools_register_in_interactive_toolset_only(self):
        ctx = FakeContext()
        self.plugin.register(ctx)
        for name in self.NEW_TOOLS:
            self.assertEqual(ctx.tools[name]["toolset"], "journal")
        self.assertEqual(self.tools.GENERATION_TOOLSET, "my-journal-generation")
        generation = {name for name, item in ctx.tools.items() if item["toolset"] == "my-journal-generation"}
        self.assertEqual(generation, self.GENERATION_TOOLS)
        manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        for name in self.NEW_TOOLS:
            self.assertIn(f"  - {name}\n", manifest)

    def test_windows_bridge_and_wsl_runtime_map_new_tools(self):
        runtime = importlib.import_module(f"{self.plugin.__name__}.wsl_runtime")
        bridge = importlib.import_module(f"{self.plugin.__name__}.windows_bridge")
        schemas = importlib.import_module(f"{self.plugin.__name__}.schemas")
        for name in self.NEW_TOOLS:
            self.assertTrue(callable(getattr(self.tools, runtime._TOOL_HANDLERS[name])))
            self.assertIn(name, bridge.TOOL_SCHEMAS)
        for attribute in ("RECURRING_ISSUES_SCHEMA", "LESSON_RECORD_SCHEMA", "IMPROVEMENT_REPORT_SCHEMA"):
            self.assertEqual(getattr(schemas, attribute), getattr(self.tools, attribute))
        self.assertEqual(set(schemas.GENERATION_TOOL_NAMES), self.GENERATION_TOOLS)

        class Plugin:
            tools = self.tools

        request = {
            "schema_version": 1,
            "operation": "journal_recurring_issues",
            "args": {"range": "2026-07-01 to 2026-07-03"},
            "windows_home": "C:\\Users\\example\\AppData\\Local\\hermes",
            "windows_journal_root": "C:\\Users\\example\\AppData\\Local\\hermes\\journal",
            "wsl_hermes_home": str(self.root.parent),
            "wsl_journal_root": str(self.root.parent / "journal"),
            "windows_executable": "C:\\Program Files\\Hermes\\hermes.exe",
            "windows_python": "C:\\Python\\python.exe",
            "windows_plugin_root": "C:\\Users\\example\\plugin",
        }
        result = runtime.dispatch(request, plugin=Plugin)
        self.assertEqual(result["coverage"]["calendar_days"], 3)

    def test_cli_commands_forward_read_only_operations(self):
        import argparse
        import contextlib
        import io

        bridge = importlib.import_module(f"{self.plugin.__name__}.windows_bridge")
        parser = argparse.ArgumentParser()
        bridge.register_cli(parser)
        calls = []

        def forward(operation, payload):
            calls.append((operation, payload))
            return json.dumps({"ok": True})

        with mock.patch.object(bridge, "_forward", side_effect=forward), contextlib.redirect_stdout(io.StringIO()):
            bridge.journal_command(parser.parse_args(["recurring", "last 7 days", "--min-dates", "2"]))
            bridge.journal_command(parser.parse_args(["improvements", "--as-of", "2026-07-28"]))
        self.assertEqual(calls, [
            ("journal_recurring_issues", {"range": "last 7 days", "min_dates": 2}),
            ("journal_improvement_report", {"as_of": "2026-07-28"}),
        ])

        self.write_note("2026-07-01", blockers="- Deploy failed because the cache was stale.")
        cli = sys.modules[f"{self.plugin.__name__}.cli"]
        native = argparse.ArgumentParser()
        cli.register_cli(native)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.journal_command(native.parse_args(["recurring", "2026-07-01 to 2026-07-02", "--min-dates", "2"]))
            cli.journal_command(native.parse_args(["improvements", "--as-of", "2026-07-02"]))
        self.assertIn('"candidate_recurring_issues"', output.getvalue())
        self.assertIn('"improvement_report"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
