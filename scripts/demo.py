#!/usr/bin/env python3
"""Generate a private data free My Journal demonstration."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

DEMO_DATE = "2026-08-24"
MARKER_NAME = ".my-journal-synthetic-demo.json"
SYNTHETIC_CONVERSATIONS = (
    {
        "conversation": "synthetic-planning",
        "messages": [
            "Create a small offline reading tracker.",
            "Keep data in portable Markdown and verify the export.",
        ],
    },
    {
        "conversation": "synthetic-verification",
        "messages": [
            "The export test passed with six sample books.",
            "Keyboard navigation still needs review.",
        ],
    },
)
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class DemoResult:
    __slots__ = ("root", "note_path", "evidence_path", "receipt_path")

    def __init__(self, root: Path, note_path: Path, evidence_path: Path, receipt_path: Path) -> None:
        self.root = root
        self.note_path = note_path
        self.evidence_path = evidence_path
        self.receipt_path = receipt_path


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_reparse(metadata: os.stat_result) -> bool:
    attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attribute and getattr(metadata, "st_file_attributes", 0) & attribute)


def _canonical_destination(value: Path) -> Path:
    destination = value.expanduser().absolute()
    if destination.name in {"", ".", ".."}:
        raise ValueError("demo destination must name a new directory")
    if sys.platform == "darwin" and len(destination.parts) > 1 and destination.parts[1] in {"etc", "tmp", "var"}:
        alias = Path(destination.anchor) / destination.parts[1]
        destination = alias.resolve(strict=True).joinpath(*destination.parts[2:])
    return destination


def _open_directory_no_follow(path: Path) -> int:
    anchor = Path(path.anchor)
    descriptor = os.open(str(anchor), _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _create_posix_root(destination: Path) -> int:
    parent_descriptor = _open_directory_no_follow(destination.parent)
    try:
        os.mkdir(destination.name, 0o700, dir_fd=parent_descriptor)
        return os.open(destination.name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _validate_windows_parent(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        metadata = os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise ValueError("demo destination must not use a linked or reparse point ancestor")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("demo destination parent must be a directory")


def _create_fresh_root(
    destination: Path, *, overwrite: bool
) -> tuple[Path, int | None, tuple[int, int]]:
    if overwrite:
        raise ValueError("synthetic demo overwrite is disabled; choose a new destination")
    destination = _canonical_destination(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("synthetic demo destination must not already exist")
    if not destination.parent.exists():
        raise FileNotFoundError("synthetic demo destination parent must already exist")
    if os.name == "posix":
        descriptor = _create_posix_root(destination)
        metadata = os.fstat(descriptor)
        return destination, descriptor, (metadata.st_dev, metadata.st_ino)
    _validate_windows_parent(destination.parent)
    destination.mkdir(mode=0o700)
    metadata = os.lstat(destination)
    if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("synthetic demo destination is unsafe")
    return destination, None, (metadata.st_dev, metadata.st_ino)


def _open_or_create_relative_directory(root_descriptor: int, parts: tuple[str, ...]) -> int:
    descriptor = os.dup(root_descriptor)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _write_posix_file(root_descriptor: int, relative: Path, text: str) -> None:
    parent = _open_or_create_relative_directory(root_descriptor, tuple(relative.parts[:-1]))
    descriptor: int | None = None
    try:
        descriptor = os.open(
            relative.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent,
        )
        data = text.encode("utf-8")
        written = 0
        while written < len(data):
            count = os.write(descriptor, data[written:])
            if count <= 0:
                raise OSError("short synthetic demo write")
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.fsync(parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _write_windows_file(root: Path, relative: Path, text: str) -> None:
    current = root
    for part in relative.parts[:-1]:
        candidate = current / part
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            pass
        metadata = os.lstat(candidate)
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("synthetic demo output tree is unsafe")
        current = candidate
    target = current / relative.name
    with target.open("x", encoding="utf-8", newline="") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _write_demo_file(root: Path, root_descriptor: int | None, relative: Path, text: str) -> None:
    if root_descriptor is not None:
        _write_posix_file(root_descriptor, relative, text)
    else:
        _write_windows_file(root, relative, text)


def _verify_root_identity(
    root: Path,
    root_descriptor: int | None,
    expected: tuple[int, int],
) -> None:
    try:
        current = os.lstat(root)
    except FileNotFoundError as exc:
        raise ValueError("synthetic demo destination changed during generation") from exc
    if (
        stat.S_ISLNK(current.st_mode)
        or _is_reparse(current)
        or not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != expected
    ):
        raise ValueError("synthetic demo destination changed during generation")
    if root_descriptor is not None:
        opened = os.fstat(root_descriptor)
        if (opened.st_dev, opened.st_ino) != expected:
            raise ValueError("synthetic demo destination descriptor changed during generation")


def _note_text(evidence_sha256: str) -> str:
    return f"""# My Journal: {DEMO_DATE}

> Synthetic demonstration only. No Hermes conversations, configuration, credentials, or model provider were accessed.

## Overview

A small offline reading tracker moved from an idea to a verified Markdown export. Keyboard navigation remained open.

## Conversation Coverage

Two embedded synthetic conversations and four synthetic messages were represented. No user data was read.

## Projects and Workstreams

Reading tracker. Defined portable Markdown storage, created a six book sample export, and retained accessibility review as unfinished work.

## Decisions

Use portable Markdown as the canonical format and keep the demonstration completely offline.

## Changes and Verification

A synthetic export containing six sample books was represented as passing its bounded verification check.

## Completed Work

The synthetic project plan and sample export verification were completed.

## Blockers and Failures

No demonstrated implementation failure remained. Keyboard navigation had not yet been reviewed.

## Corrections and Preference Changes

The initial idea of an unspecified local database was narrowed to portable Markdown.

## Open Threads

Review keyboard navigation and decide whether to add a compact weekly summary.

## Context Index

Professional: synthetic software planning and verification. No personal conversation was included.

## Automation Appendix

This entry was generated deterministically by `scripts/demo.py`. It did not invoke Hermes, a model, a network service, or a session database.

## Provenance

Source: embedded synthetic conversations
Evidence SHA256: {evidence_sha256}
Validation: synthetic demonstration receipt
"""


def generate_demo(destination: Path, *, overwrite: bool = False) -> DemoResult:
    root, root_descriptor, root_identity = _create_fresh_root(
        Path(destination), overwrite=overwrite
    )
    try:
        marker_text = json.dumps({"kind": "my-journal-synthetic-demo", "schema_version": 1}, indent=2) + "\n"
        _write_demo_file(root, root_descriptor, Path(MARKER_NAME), marker_text)
        _verify_root_identity(root, root_descriptor, root_identity)

        evidence = {
            "schema_version": 1,
            "source": "embedded-synthetic-conversations",
            "synthetic": True,
            "date": DEMO_DATE,
            "conversations": SYNTHETIC_CONVERSATIONS,
        }
        evidence_sha256 = _sha256_bytes(_canonical_json(evidence).encode("utf-8"))
        evidence["evidence_sha256"] = evidence_sha256
        evidence_relative = Path("evidence/synthetic-conversations.json")
        evidence_text = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        _write_demo_file(root, root_descriptor, evidence_relative, evidence_text)
        _verify_root_identity(root, root_descriptor, root_identity)

        note_relative = Path(f"journal/2026/08/{DEMO_DATE}.md")
        note_text = _note_text(evidence_sha256)
        _write_demo_file(root, root_descriptor, note_relative, note_text)
        _verify_root_identity(root, root_descriptor, root_identity)

        receipt = {
            "schema_version": 1,
            "kind": "synthetic-demonstration-receipt",
            "synthetic": True,
            "validated": True,
            "validation_errors": [],
            "date": DEMO_DATE,
            "evidence_sha256": evidence_sha256,
            "note_sha256": _sha256_bytes(note_text.encode("utf-8")),
        }
        receipt_relative = Path("receipts/validation.json")
        _write_demo_file(
            root,
            root_descriptor,
            receipt_relative,
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        )
        _verify_root_identity(root, root_descriptor, root_identity)
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)
    return DemoResult(root, root / note_relative, root / evidence_relative, root / receipt_relative)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a safe synthetic My Journal example")
    parser.add_argument("--output", type=Path, help="new destination directory; defaults to ./my-journal-demo")
    parser.add_argument("--overwrite", action="store_true", help="deprecated safety check; existing destinations are never overwritten")
    parser.add_argument("--ephemeral", action="store_true", help="print a temporary example and remove it before exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.ephemeral and (args.output is not None or args.overwrite):
        raise SystemExit("--ephemeral cannot be combined with --output or --overwrite")
    if args.ephemeral:
        with tempfile.TemporaryDirectory(prefix="my-journal-demo-parent-") as temporary:
            result = generate_demo(Path(temporary) / "example")
            print(result.note_path.read_text(encoding="utf-8"))
            print("Synthetic demonstration removed. No user data was accessed.")
        return 0

    result = generate_demo(args.output or Path.cwd() / "my-journal-demo", overwrite=args.overwrite)
    print(f"Synthetic Journal created: {result.note_path}")
    print("No Hermes conversations, configuration, credentials, model, or network service was accessed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
