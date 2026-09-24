from __future__ import annotations

import argparse
import hashlib
import importlib.util
import tarfile
from pathlib import Path

FORBIDDEN_NAMES = {".env", "credentials.json", "config.local.json"}
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".pyc", ".zip"}
RUNTIME_ROOTS = {"journal", "evidence", "runs", "pending", "backups"}
MAX_MEMBERS = 5000
MAX_MEMBER_BYTES = 20_000_000
MAX_TOTAL_BYTES = 100_000_000
REQUIRED_RELEASE_FILES = {
    "README.md",
    "LICENSE",
    "install.py",
    "install-windows.ps1",
    "plugins/my-journal/plugin.yaml",
    "plugins/my-journal/__init__.py",
    "plugins/my-journal/onboarding.py",
    "plugins/my-journal/schemas.py",
    "plugins/my-journal/windows_bridge.py",
    "plugins/my-journal/wsl_runtime.py",
    "plugins/my-journal/tests/test_onboarding.py",
    "plugins/my-journal/tests/test_runtime_hardening.py",
    "plugins/my-journal/tests/test_windows_bridge.py",
    "plugins/my-journal/tests/test_wsl_runtime.py",
    "skills/note-taking/my-journal/SKILL.md",
    "skills/note-taking/journal/SKILL.md",
}


def _builder_module():
    path = Path(__file__).resolve().with_name("build_release.py")
    spec = importlib.util.spec_from_file_location("my_journal_release_builder", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("release builder could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify(ref: str, archive: Path, *, repo: Path | None = None) -> dict:
    repository = (repo or Path.cwd()).expanduser().absolute()
    archive = archive.expanduser().absolute()
    builder = _builder_module()
    commit = builder.resolve_commit(ref, repository)
    builder.validate_stable_metadata(commit, repository)
    expected = builder.archive_bytes(commit, repository)
    actual = archive.read_bytes()
    if actual != expected:
        raise ValueError("archive is not the exact deterministic build of the requested commit")

    total_bytes = 0
    with tarfile.open(archive, "r:gz") as opened:
        members = opened.getmembers()
        if not members or len(members) > MAX_MEMBERS:
            raise ValueError("archive member count is outside the supported range")
        names = [member.name for member in members]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("archive members must be unique and sorted")
        missing_required = sorted(
            relative
            for relative in REQUIRED_RELEASE_FILES
            if builder.PREFIX + relative not in names
        )
        if missing_required:
            raise ValueError(
                "archive is missing required release member: " + ", ".join(missing_required)
            )
        for member in members:
            path = Path(member.name)
            archive_root = builder.PREFIX.rstrip("/")
            if member.name != archive_root and not member.name.startswith(builder.PREFIX):
                raise ValueError(f"archive member has the wrong prefix: {member.name}")
            relative_name = member.name.removeprefix(builder.PREFIX)
            relative = Path(relative_name)
            if relative.parts and relative.parts[0] in RUNTIME_ROOTS:
                raise ValueError(f"archive contains runtime data: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"archive contains a link: {member.name}")
            if member.isdev() or member.isfifo():
                raise ValueError(f"archive contains a special file: {member.name}")
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"archive path is unsafe: {member.name}")
            if path.name in FORBIDDEN_NAMES or path.suffix in FORBIDDEN_SUFFIXES:
                raise ValueError(f"archive contains forbidden content: {member.name}")
            if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                raise ValueError(f"archive member is oversized: {member.name}")
            total_bytes += member.size
            if total_bytes > MAX_TOTAL_BYTES:
                raise ValueError("archive expanded size is oversized")

    sidecars = {
        "SHA256SUMS": f"{hashlib.sha256(actual).hexdigest()}  {archive.name}\n",
        f"{archive.name}.inventory.txt": "\n".join(names) + "\n",
        f"{archive.name}.commit.txt": commit + "\n",
    }
    for name, expected_text in sidecars.items():
        path = archive.parent / name
        label = "commit sidecar" if name.endswith(".commit.txt") else "release sidecar"
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"{label} is missing or unsafe: {name}")
        if path.read_text(encoding="utf-8") != expected_text:
            raise ValueError(f"{label} does not match the exact archive: {name}")

    return {
        "commit": commit,
        "sha256": hashlib.sha256(actual).hexdigest(),
        "members": len(members),
        "expanded_bytes": total_bytes,
        "sidecars_verified": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify an exact My Journal release archive")
    parser.add_argument("--ref", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = verify(args.ref, args.archive, repo=args.repo)
    for key in ("commit", "sha256", "members", "expanded_bytes", "sidecars_verified"):
        print(f"{key}={result[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
