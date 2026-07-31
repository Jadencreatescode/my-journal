from __future__ import annotations

import importlib.util
import hashlib
import os
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
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
    required_files = {
        "README.md": "My Journal `0.1.0` release fixture\n",
        "LICENSE": "MIT\n",
        "install.py": "print('fixture')\n",
        "pyproject.toml": "version = \"0.1.0\"\nrelease = \"0.1.0\"\n",
        "SECURITY.md": "Version `0.1.0` receives security fixes.\n",
        "COMPATIBILITY.md": "Stable 0.1.0 compatibility contract.\n",
        "PRIVACY.md": "The stable release uses nonreversible references.\n",
        "THREAT_MODEL.md": "Native Windows support is outside scope.\n",
        "plugins/my-journal/plugin.yaml": "version: 0.1.0\n",
        "plugins/my-journal/__init__.py": "VALUE = 1\n",
        "plugins/my-journal/onboarding.py": "VALUE = 1\n",
        "plugins/my-journal/tests/test_onboarding.py": "# onboarding tests\n",
        "plugins/my-journal/tests/test_runtime_hardening.py": "# runtime tests\n",
        "skills/note-taking/my-journal/SKILL.md": "version: 0.1.0\n# Evidence skill\n",
        "skills/note-taking/journal/SKILL.md": "version: 0.1.0\n# Journal skill\n",
    }
    for relative, content in required_files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "fixture")
    return git(root, "rev-parse", "HEAD")


class ReleaseToolTests(unittest.TestCase):
    def test_stable_version_metadata_and_wsl_support_are_synchronized(self):
        builder = load_script("build_release")
        self.assertEqual(builder.VERSION, "0.1.0")
        expected = {
            "pyproject.toml": ("version = \"0.1.0\"", "release = \"0.1.0\""),
            "plugins/my-journal/plugin.yaml": ("version: 0.1.0",),
            "skills/note-taking/journal/SKILL.md": ("version: 0.1.0",),
            "skills/note-taking/my-journal/SKILL.md": ("version: 0.1.0",),
            ".github/workflows/release.yml": (
                "default: v0.1.0",
                'test "$RELEASE_REF" = "v0.1.0"',
                "ref: ${{ inputs.ref }}",
                "python3 scripts/check_release_tree.py --root .",
                "python3 scripts/compile_all.py",
                "python3 -m unittest discover -s plugins/my-journal/tests",
                "python3 -m unittest discover -s skills/note-taking/my-journal/tests",
                "python3 -m unittest discover -s tests",
                "my-journal-v0.1.0.tar.gz",
            ),
            "README.md": (
                "My Journal `0.1.0`",
                "Ubuntu under WSL is supported as a Linux environment",
                "--ref v0.1.0",
            ),
            "COMPATIBILITY.md": ("Ubuntu under WSL is supported as a Linux environment",),
            "SECURITY.md": ("`0.1.0`",),
        }
        for relative, fragments in expected.items():
            content = (ROOT / relative).read_text(encoding="utf-8")
            for fragment in fragments:
                self.assertIn(fragment, content, f"{relative} is missing {fragment}")

    def test_verifier_requires_guided_setup_runtime_and_tests(self):
        verifier = load_script("verify_release")
        self.assertIn("plugins/my-journal/onboarding.py", verifier.REQUIRED_RELEASE_FILES)
        self.assertIn(
            "plugins/my-journal/tests/test_onboarding.py",
            verifier.REQUIRED_RELEASE_FILES,
        )
        self.assertIn(
            "plugins/my-journal/tests/test_runtime_hardening.py",
            verifier.REQUIRED_RELEASE_FILES,
        )

    def test_git_tracks_every_required_installer_component(self):
        worktree = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if worktree.returncode != 0 or worktree.stdout.strip() != "true":
            self.skipTest("release tree is not inside a Git worktree")
        tracked = set(
            subprocess.run(
                ["git", "ls-files"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
        )
        required = {
            "skills/note-taking/my-journal/SKILL.md",
            "skills/note-taking/journal/SKILL.md",
            "plugins/my-journal/plugin.yaml",
        }
        self.assertEqual(required - tracked, set())

    def test_git_component_check_skips_outside_worktree(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            f"{__name__}.ROOT", Path(tmp)
        ):
            result = unittest.TestResult()
            self.__class__("test_git_tracks_every_required_installer_component").run(result)

        self.assertEqual(result.errors, [])
        self.assertEqual(result.failures, [])
        self.assertEqual(len(result.skipped), 1)

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
            root_name = "my-journal-v0.1.0"
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
            (repo / "install.py").write_text("print('changed')\n", encoding="utf-8")
            git(repo, "add", "install.py")
            git(repo, "commit", "-q", "-m", "changed")
            second_commit = git(repo, "rev-parse", "HEAD")

            with self.assertRaisesRegex(ValueError, "exact deterministic build"):
                verifier.verify(second_commit, archive, repo=repo)

    def test_verifier_rejects_exact_archive_missing_installer_component(self):
        builder = load_script("build_release")
        verifier = load_script("verify_release")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            create_repo(repo)
            (repo / "plugins" / "my-journal" / "onboarding.py").unlink()
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "remove required skill")
            commit = git(repo, "rev-parse", "HEAD")
            archive = builder.build(commit, base / "dist", repo=repo)

            with self.assertRaisesRegex(ValueError, "required release member"):
                verifier.verify(commit, archive, repo=repo)

    def test_builder_rejects_stable_named_archive_from_alpha_metadata_ref(self):
        builder = load_script("build_release")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            create_repo(repo)
            (repo / "pyproject.toml").write_text(
                'version = "0.1.0a5"\nrelease = "0.1.0-alpha.5"\n',
                encoding="utf-8",
            )
            (repo / "plugins/my-journal/plugin.yaml").write_text(
                "version: 0.1.0-alpha.5\n", encoding="utf-8"
            )
            git(repo, "add", ".")
            git(repo, "commit", "-q", "-m", "alpha metadata")
            alpha_commit = git(repo, "rev-parse", "HEAD")

            with self.assertRaisesRegex(ValueError, "stable release metadata"):
                builder.build(alpha_commit, base / "dist", repo=repo)

    def test_builder_rejects_alpha_plugin_line_when_other_metadata_is_stable(self):
        builder = load_script("build_release")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            create_repo(repo)
            (repo / "plugins/my-journal/plugin.yaml").write_text(
                "version: 0.1.0-alpha.5\n", encoding="utf-8"
            )
            git(repo, "add", "plugins/my-journal/plugin.yaml")
            git(repo, "commit", "-q", "-m", "alpha plugin only")
            commit = git(repo, "rev-parse", "HEAD")

            with self.assertRaisesRegex(ValueError, "exactly one"):
                builder.build(commit, base / "dist", repo=repo)

    def test_builder_rejects_conflicting_plugin_version_lines(self):
        builder = load_script("build_release")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            create_repo(repo)
            (repo / "plugins/my-journal/plugin.yaml").write_text(
                "version: 0.1.0\nversion: 0.1.0-alpha.5\n", encoding="utf-8"
            )
            git(repo, "add", "plugins/my-journal/plugin.yaml")
            git(repo, "commit", "-q", "-m", "conflicting plugin versions")
            commit = git(repo, "rev-parse", "HEAD")

            with self.assertRaisesRegex(ValueError, "exactly one"):
                builder.build(commit, base / "dist", repo=repo)

    def test_verifier_rejects_runtime_data_inside_exact_archive(self):
        builder = load_script("build_release")
        verifier = load_script("verify_release")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            create_repo(repo)
            private = repo / "journal" / "notes" / "2026-07-30.md"
            private.parent.mkdir(parents=True)
            private.write_text("private journal content\n", encoding="utf-8")
            git(repo, "add", ".")
            git(repo, "commit", "-q", "-m", "runtime data")
            commit = git(repo, "rev-parse", "HEAD")
            archive = builder.build(commit, base / "dist", repo=repo)

            with self.assertRaisesRegex(ValueError, "runtime data"):
                verifier.verify(commit, archive, repo=repo)

    def test_verifier_rejects_tampered_release_sidecars(self):
        builder = load_script("build_release")
        verifier = load_script("verify_release")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            commit = create_repo(repo)
            output = base / "dist"
            archive = builder.build(commit, output, repo=repo)
            (output / f"{archive.name}.commit.txt").write_text("0" * 40 + "\n")

            with self.assertRaisesRegex(ValueError, "commit sidecar"):
                verifier.verify(commit, archive, repo=repo)

    def test_release_sidecars_match_exact_archive(self):
        builder = load_script("build_release")
        verifier = load_script("verify_release")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            commit = create_repo(repo)
            output = base / "dist"
            archive = builder.build(commit, output, repo=repo)
            result = verifier.verify(commit, archive, repo=repo)
            self.assertEqual(
                (output / "SHA256SUMS").read_text(encoding="utf-8"),
                f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n",
            )
            self.assertTrue(result["sidecars_verified"])

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
