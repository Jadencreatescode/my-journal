from __future__ import annotations

import importlib.util
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"release_test_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def create_repo(root: Path) -> str:
    git(root, "init", "-q")
    git(root, "config", "user.email", "release-tests@example.invalid")
    git(root, "config", "user.name", "Release Tests")
    (root / "README.md").write_text("release fixture\n", encoding="utf-8")
    package = root / "plugins" / "my-journal"
    package.mkdir(parents=True)
    (package / "plugin.yaml").write_text("version: 0.1.0-alpha.1\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "fixture")
    return git(root, "rev-parse", "HEAD")


class ReleaseToolTests(unittest.TestCase):
    def test_two_builds_of_exact_commit_are_byte_identical_and_safe(self):
        builder = load_script("build_release")
        verifier = load_script("verify_release")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            commit = create_repo(repo)
            first = builder.build(commit, base / "one", repo=repo)
            second = builder.build(commit, base / "two", repo=repo)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            result = verifier.verify(commit, first, repo=repo)
            self.assertEqual(result["commit"], commit)
            self.assertEqual(len(result["sha256"]), 64)
            with tarfile.open(first, "r:gz") as opened:
                names = [member.name for member in opened.getmembers()]
            self.assertEqual(names, sorted(names))
            root_name = "my-journal-v0.1.0-alpha.1"
            self.assertTrue(
                all(name == root_name or name.startswith(root_name + "/") for name in names)
            )

    def test_verifier_rejects_archive_from_different_commit(self):
        builder = load_script("build_release")
        verifier = load_script("verify_release")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            first_commit = create_repo(repo)
            archive = builder.build(first_commit, base / "dist", repo=repo)
            (repo / "README.md").write_text("changed\n", encoding="utf-8")
            git(repo, "add", "README.md")
            git(repo, "commit", "-q", "-m", "changed")
            second_commit = git(repo, "rev-parse", "HEAD")

            with self.assertRaisesRegex(ValueError, "exact deterministic build"):
                verifier.verify(second_commit, archive, repo=repo)

    def test_release_tree_allows_skill_named_journal_but_rejects_runtime_data(self):
        checker = load_script("check_release_tree")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / "skills" / "note-taking" / "journal"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("safe\n", encoding="utf-8")
            self.assertEqual(checker.check_tree(root), 1)
            runtime = root / "journal"
            runtime.mkdir()
            (runtime / "private.md").write_text("private\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "runtime data"):
                checker.check_tree(root)


if __name__ == "__main__":
    unittest.main()
