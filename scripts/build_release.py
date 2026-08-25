from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import subprocess
import tarfile
from pathlib import Path

VERSION = "0.2.0"
PYTHON_VERSION = "0.2.0"
PREFIX = f"my-journal-v{VERSION}/"
ARCHIVE_NAME = f"my-journal-v{VERSION}.tar.gz"
REQUIRED_STABLE_FIELDS = {
    "pyproject.toml": {
        "version =": f'version = "{PYTHON_VERSION}"',
        "release =": f'release = "{VERSION}"',
    },
    "plugins/my-journal/plugin.yaml": {"version:": f"version: {VERSION}"},
    "skills/note-taking/journal/SKILL.md": {"version:": f"version: {VERSION}"},
    "skills/note-taking/my-journal/SKILL.md": {"version:": f"version: {VERSION}"},
}
REQUIRED_STABLE_FRAGMENTS = {
    "README.md": (f"My Journal `{VERSION}`",),
    "SECURITY.md": (f"`{VERSION}`",),
}



def _git(repo: Path, *args: str, text: bool = False):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=text,
    ).stdout


def resolve_commit(ref: str, repo: Path) -> str:
    return str(_git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}", text=True)).strip()


def _ref_text(commit: str, relative: str, repo: Path) -> str:
    try:
        return str(_git(repo, "show", f"{commit}:{relative}", text=True))
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"stable release metadata file is missing: {relative}") from exc


def validate_stable_metadata(commit: str, repo: Path) -> None:
    errors: list[str] = []
    for relative, fields in REQUIRED_STABLE_FIELDS.items():
        content = _ref_text(commit, relative, repo)
        actual_lines = [line.strip() for line in content.splitlines()]
        for prefix, expected in fields.items():
            matches = [line for line in actual_lines if line.startswith(prefix)]
            if matches != [expected]:
                errors.append(
                    f"{relative} must contain exactly one {expected}; found {matches}"
                )
    for relative, fragments in REQUIRED_STABLE_FRAGMENTS.items():
        content = _ref_text(commit, relative, repo)
        for fragment in fragments:
            if fragment not in content:
                errors.append(f"{relative} is missing {fragment}")
    if errors:
        raise ValueError("release metadata mismatch: " + "; ".join(errors))


def archive_bytes(commit: str, repo: Path) -> bytes:
    tar_bytes = _git(
        repo,
        "archive",
        "--format=tar",
        f"--prefix={PREFIX}",
        commit,
    )
    stream = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        compresslevel=9,
        fileobj=stream,
        mtime=0,
    ) as compressed:
        compressed.write(tar_bytes)
    payload = bytearray(stream.getvalue())
    if len(payload) < 10:
        raise ValueError("gzip release payload is truncated")
    payload[9] = 255
    return bytes(payload)


def build(ref: str, output: Path, *, repo: Path | None = None) -> Path:
    repository = (repo or Path.cwd()).expanduser().absolute()
    commit = resolve_commit(ref, repository)
    validate_stable_metadata(commit, repository)
    payload = archive_bytes(commit, repository)
    output = output.expanduser().absolute()
    output.mkdir(parents=True, exist_ok=True)
    target = output / ARCHIVE_NAME
    target.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (output / "SHA256SUMS").write_text(
        f"{digest}  {target.name}\n", encoding="utf-8"
    )
    with tarfile.open(target, "r:gz") as opened:
        members = [item.name for item in opened.getmembers()]
    (output / f"{target.name}.inventory.txt").write_text(
        "\n".join(members) + "\n", encoding="utf-8"
    )
    (output / f"{target.name}.commit.txt").write_text(
        commit + "\n", encoding="utf-8"
    )
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a deterministic My Journal archive")
    parser.add_argument("--ref", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(build(args.ref, args.output, repo=args.repo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
