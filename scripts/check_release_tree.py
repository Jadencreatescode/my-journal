from __future__ import annotations

import argparse
from pathlib import Path

FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".zip", ".pyc"}
FORBIDDEN_NAMES = {".env", "config.local.json", "credentials.json"}
RUNTIME_ROOTS = {"journal", "evidence", "runs", "pending", "backups"}
IGNORED_ROOTS = {".git"}
FORBIDDEN_TOP_LEVEL = {".venv", "venv", "dist", "build", "__pycache__"}


def check_tree(root: Path) -> int:
    root = root.expanduser().absolute()
    violations: list[str] = []
    files = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not relative.parts or relative.parts[0] in IGNORED_ROOTS:
            continue
        if relative.parts[0] in FORBIDDEN_TOP_LEVEL:
            violations.append(f"build or environment data path: {relative.as_posix()}")
            continue
        if relative.parts[0] in RUNTIME_ROOTS:
            violations.append(f"runtime data path: {relative.as_posix()}")
            continue
        if path.is_symlink():
            violations.append(f"symlink: {relative.as_posix()}")
        elif path.is_file():
            files += 1
            if path.name in FORBIDDEN_NAMES or path.suffix in FORBIDDEN_SUFFIXES:
                violations.append(f"forbidden release member: {relative.as_posix()}")
    if files == 0:
        violations.append("release tree has no files")
    if violations:
        raise ValueError("\n".join(violations))
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the source tree for private runtime data")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        count = check_tree(args.root)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"release tree check passed for {count} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
