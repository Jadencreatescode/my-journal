#!/usr/bin/env python3
"""Fail closed when a public source tree contains likely private material."""
from __future__ import annotations

import argparse
import re
import struct
from pathlib import Path

IGNORED_ROOTS = {".git"}
FORBIDDEN_TOP_LEVEL = {".venv", "venv", "dist", "build", "__pycache__"}
TEXT_SUFFIXES = {"", ".css", ".html", ".json", ".md", ".ps1", ".py", ".toml", ".txt", ".yaml", ".yml"}
ALLOWED_BINARY = {
    "docs/assets/my-journal-functionality-demo.png",
    "docs/assets/my-journal-social-preview.png",
}
SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "provider token": re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    "bearer credential": re.compile(r"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
}
EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![A-Za-z0-9.-])")
POSIX_HOME = re.compile(r"(?<![A-Za-z0-9_])/(Users|home)/([A-Za-z0-9._-]+)")
WINDOWS_HOME = re.compile(r"(?i)C:\\Users\\([A-Za-z0-9._-]+)")
PRIVATE_ROOT = re.compile(r"(?<![A-Za-z0-9_])/opt/data(?:/|\b)")
PRIVATE_NETWORK = re.compile(r"\b100\.(?:64|[7-9][0-9]|1[01][0-9]|12[0-7])(?:\.\d{1,3}){2}\b")
ALLOWED_EXAMPLE_USERS = {"exampleuser"}
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.invalid", "example.test"}
PATTERN_FIXTURE_FILES = {
    "scripts/check_public_content.py",
    "tests/test_public_content.py",
    "tests/test_release.py",
}


def _png_text_chunks(data: bytes) -> list[str]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid PNG signature")
    chunks: list[str] = []
    offset = 8
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError("truncated PNG chunk")
        if kind in {b"tEXt", b"zTXt", b"iTXt"}:
            chunks.append(kind.decode("ascii"))
        offset = end
        if kind == b"IEND":
            if offset != len(data):
                raise ValueError("PNG has trailing bytes")
            return chunks
    raise ValueError("PNG is missing IEND")


def _iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not relative.parts or relative.parts[0] in IGNORED_ROOTS:
            continue
        if relative.parts[0] in FORBIDDEN_TOP_LEVEL:
            raise ValueError(f"public tree contains build or environment data: {relative.as_posix()}")
        if path.is_symlink():
            raise ValueError(f"public tree contains a symlink: {relative.as_posix()}")
        if path.is_file():
            yield relative, path


def check_public_content(root: Path) -> int:
    root = root.expanduser().absolute()
    violations: list[str] = []
    count = 0
    for relative, path in _iter_files(root):
        count += 1
        name = relative.as_posix()
        data = path.read_bytes()
        if name in ALLOWED_BINARY:
            try:
                text_chunks = _png_text_chunks(data)
            except ValueError as exc:
                violations.append(f"{name}: {exc}")
            else:
                if text_chunks:
                    violations.append(f"{name}: PNG contains textual metadata chunks {text_chunks}")
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES or b"\x00" in data:
            violations.append(f"{name}: unapproved binary content")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            violations.append(f"{name}: text is not UTF8")
            continue
        if name not in PATTERN_FIXTURE_FILES:
            for label, pattern in SECRET_PATTERNS.items():
                if pattern.search(text):
                    violations.append(f"{name}: likely {label}")
            if PRIVATE_ROOT.search(text):
                violations.append(f"{name}: private absolute VPS path")
            if PRIVATE_NETWORK.search(text):
                violations.append(f"{name}: private network address")
            for match in POSIX_HOME.finditer(text):
                if match.group(2).lower() not in ALLOWED_EXAMPLE_USERS:
                    violations.append(f"{name}: personal home path for {match.group(2)}")
            for match in WINDOWS_HOME.finditer(text):
                if match.group(1).lower() not in ALLOWED_EXAMPLE_USERS:
                    violations.append(f"{name}: personal Windows home path for {match.group(1)}")
            for match in EMAIL.finditer(text):
                if match.group(2).lower() not in ALLOWED_EMAIL_DOMAINS:
                    violations.append(f"{name}: nonexample email address")
    if count == 0:
        violations.append("public tree has no files")
    if violations:
        raise ValueError("\n".join(sorted(set(violations))))
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check public source content for likely private material")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        count = check_public_content(args.root)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"public content check passed for {count} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
