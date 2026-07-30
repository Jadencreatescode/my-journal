from __future__ import annotations

import argparse
import gzip
import hashlib
import subprocess
import tarfile
from pathlib import Path

VERSION = "0.1.0-alpha.4"
PREFIX = f"my-journal-v{VERSION}/"
ARCHIVE_NAME = f"my-journal-v{VERSION}.tar.gz"


def _git(repo: Path, *args: str, text: bool = False):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=text,
    ).stdout


def resolve_commit(ref: str, repo: Path) -> str:
    return str(_git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}", text=True)).strip()


def archive_bytes(commit: str, repo: Path) -> bytes:
    tar_bytes = _git(
        repo,
        "archive",
        "--format=tar",
        f"--prefix={PREFIX}",
        commit,
    )
    compressed = gzip.compress(tar_bytes, compresslevel=9, mtime=0)
    return compressed


def build(ref: str, output: Path, *, repo: Path | None = None) -> Path:
    repository = (repo or Path.cwd()).expanduser().absolute()
    commit = resolve_commit(ref, repository)
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
