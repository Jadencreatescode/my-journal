#!/usr/bin/env python3
"""Install the My Journal skills and plugin into a Hermes home."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path


COMPONENTS = (
    (Path("skills/note-taking/my-journal"), Path("skills/note-taking/my-journal")),
    (Path("skills/note-taking/journal"), Path("skills/note-taking/journal")),
    (Path("plugins/my-journal"), Path("plugins/my-journal")),
)


def _reject_symlink_chain(hermes_home: Path, destination: Path) -> None:
    """Reject existing symlinks from the Hermes home through a destination."""
    relative = destination.relative_to(hermes_home)
    current = hermes_home
    if current.is_symlink():
        raise ValueError(f"Hermes home must not be a symlink: {current}")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"installation destination contains a symlink: {current}")


def _open_directory_descriptor(path: Path) -> int:
    absolute = path.expanduser().absolute()
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(absolute.anchor or "/", flags)
    try:
        for part in absolute.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _move_path(source: Path, destination: Path) -> None:
    """Rename inside descriptor anchored parent directories."""
    source_parent = _open_directory_descriptor(source.parent)
    destination_parent: int | None = None
    try:
        destination_parent = _open_directory_descriptor(destination.parent)
        os.rename(
            source.name,
            destination.name,
            src_dir_fd=source_parent,
            dst_dir_fd=destination_parent,
        )
        os.fsync(destination_parent)
    finally:
        os.close(source_parent)
        if destination_parent is not None:
            os.close(destination_parent)


def _remove_path(path: Path) -> None:
    parent_descriptor = _open_directory_descriptor(path.parent)
    try:
        try:
            metadata = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(metadata.st_mode):
            shutil.rmtree(path.name, dir_fd=parent_descriptor)
        else:
            os.unlink(path.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _state_path(hermes_home: Path) -> Path:
    return hermes_home / ".my-journal" / "install-state.json"


def _transaction_path(hermes_home: Path) -> Path:
    return hermes_home / ".my-journal" / "install-transaction.json"


def _write_state(hermes_home: Path, payload: dict) -> None:
    state_path = _state_path(hermes_home)
    _reject_symlink_chain(hermes_home, state_path)
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".install-state.", suffix=".tmp", dir=state_path.parent
    )
    temporary = Path(temporary_name)
    try:
        with open(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        temporary.replace(state_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_transaction(hermes_home: Path, payload: dict) -> None:
    transaction_path = _transaction_path(hermes_home)
    _reject_symlink_chain(hermes_home, transaction_path)
    transaction_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = transaction_path.with_name(".install-transaction.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(transaction_path)


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"installed component is unsafe: {root}")
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError(f"installed component contains a symlink: {path}")
        if path.is_file():
            digest.update(relative.as_posix().encode("utf-8") + b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def install(package_root: Path, hermes_home: Path, upgrade: bool) -> list[str]:
    hermes_home = hermes_home.expanduser().absolute()
    if not hermes_home.exists() or not hermes_home.is_dir():
        raise ValueError(f"Hermes home does not exist: {hermes_home}")
    destinations = [(package_root / source, hermes_home / target) for source, target in COMPONENTS]
    for _, destination in destinations:
        _reject_symlink_chain(hermes_home, destination)
    missing = [str(source) for source, _ in destinations if not source.is_dir()]
    if missing:
        raise ValueError("package is incomplete: " + ", ".join(missing))
    existing = [destination for _, destination in destinations if destination.exists()]
    if existing and not upgrade:
        raise FileExistsError(
            "components already exist; rerun with --upgrade: "
            + ", ".join(str(path) for path in existing)
        )
    stage_root = Path(tempfile.mkdtemp(prefix=".my-journal-stage-", dir=hermes_home))
    try:
        staged_destinations = []
        for source, destination in destinations:
            relative = destination.relative_to(hermes_home)
            staged_source = stage_root / relative
            staged_source.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, staged_source)
            staged_destinations.append((staged_source, destination))
    except Exception:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    installed: list[str] = []
    activated: list[Path] = []
    backups: list[tuple[Path, Path]] = []
    planned = [
        (
            source,
            destination,
            destination.with_name(f"{destination.name}.backup-{stamp}")
            if destination.exists()
            else None,
        )
        for source, destination in staged_destinations
    ]
    _write_transaction(
        hermes_home,
        {
            "schema_version": 1,
            "action": "install",
            "stage_root": str(stage_root.relative_to(hermes_home)),
            "components": [
                {
                    "destination": str(destination.relative_to(hermes_home)),
                    "backup": str(backup.relative_to(hermes_home)) if backup else None,
                }
                for _, destination, backup in planned
            ],
        },
    )
    try:
        for source, destination, backup in planned:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if backup is not None:
                if backup.exists():
                    raise FileExistsError(f"backup destination already exists: {backup}")
                _move_path(destination, backup)
                backups.append((destination, backup))
            _move_path(source, destination)
            activated.append(destination)
            installed.append(str(destination))
        _write_state(
            hermes_home,
            {
                "schema_version": 1,
                "components": [
                    {
                        "destination": str((hermes_home / target).relative_to(hermes_home)),
                        "backup": next(
                            (
                                str(backup.relative_to(hermes_home))
                                for destination, backup in backups
                                if destination == hermes_home / target
                            ),
                            None,
                        ),
                        "installed_sha256": _tree_sha256(hermes_home / target),
                    }
                    for _, target in COMPONENTS
                ],
            },
        )
        _transaction_path(hermes_home).unlink()
        return installed
    except Exception:
        for destination in reversed(activated):
            _remove_path(destination)
        for destination, backup in reversed(backups):
            if destination.exists() or destination.is_symlink():
                _remove_path(destination)
            if backup.exists():
                _move_path(backup, destination)
        _transaction_path(hermes_home).unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def recover(hermes_home: Path) -> list[str]:
    """Roll back an install transaction interrupted by process death."""
    hermes_home = hermes_home.expanduser().absolute()
    transaction_path = _transaction_path(hermes_home)
    _reject_symlink_chain(hermes_home, transaction_path)
    if not transaction_path.is_file() or transaction_path.is_symlink():
        raise FileNotFoundError("no interrupted My Journal transaction")
    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    if not isinstance(transaction, dict) or transaction.get("action") != "install":
        raise ValueError("unsupported installation transaction")
    raw_components = transaction.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise ValueError("installation transaction has no components")
    allowed = {target.as_posix() for _, target in COMPONENTS}
    recovered: list[str] = []
    for item in reversed(raw_components):
        if not isinstance(item, dict) or item.get("destination") not in allowed:
            raise ValueError("installation transaction contains an unexpected destination")
        destination = hermes_home / item["destination"]
        _reject_symlink_chain(hermes_home, destination)
        backup_value = item.get("backup")
        backup = hermes_home / backup_value if isinstance(backup_value, str) else None
        if backup is not None:
            _reject_symlink_chain(hermes_home, backup)
        if backup is not None and backup.exists() and not backup.is_symlink():
            if destination.exists() or destination.is_symlink():
                _remove_path(destination)
            _move_path(backup, destination)
        elif backup is None and (destination.exists() or destination.is_symlink()):
            _remove_path(destination)
        recovered.append(str(destination))
    stage_value = transaction.get("stage_root")
    if isinstance(stage_value, str):
        stage_root = hermes_home / stage_value
        _reject_symlink_chain(hermes_home, stage_root)
        if stage_root.exists() and not stage_root.is_symlink():
            shutil.rmtree(stage_root)
    transaction_path.unlink()
    return list(reversed(recovered))


def restore(hermes_home: Path) -> list[str]:
    """Restore every component captured by the most recent successful upgrade."""
    hermes_home = hermes_home.expanduser().absolute()
    state_path = _state_path(hermes_home)
    _reject_symlink_chain(hermes_home, state_path)
    if not state_path.is_file() or state_path.is_symlink():
        raise FileNotFoundError("no restorable My Journal installation state")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    raw_components = state.get("components") if isinstance(state, dict) else None
    if not isinstance(raw_components, list) or not raw_components:
        raise ValueError("installation state has no restorable components")
    pairs: list[tuple[Path, Path | None]] = []
    for item in raw_components:
        if not isinstance(item, dict):
            raise ValueError("installation state component is malformed")
        destination = hermes_home / str(item.get("destination", ""))
        backup_value = item.get("backup")
        backup = hermes_home / backup_value if isinstance(backup_value, str) else None
        _reject_symlink_chain(hermes_home, destination)
        if backup is not None:
            _reject_symlink_chain(hermes_home, backup)
        if backup is not None and (not backup.exists() or backup.is_symlink()):
            raise FileNotFoundError(f"installation backup is missing: {backup}")
        pairs.append((destination, backup))

    stage_root = Path(tempfile.mkdtemp(prefix=".my-journal-restore-", dir=hermes_home))
    displaced: list[tuple[Path, Path]] = []
    restored: list[tuple[Path, Path | None]] = []
    try:
        for destination, backup in pairs:
            relative = destination.relative_to(hermes_home)
            staged_current = stage_root / relative
            staged_current.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                _move_path(destination, staged_current)
                displaced.append((destination, staged_current))
            if backup is not None:
                _move_path(backup, destination)
            restored.append((destination, backup))
        state_path.unlink()
        return [str(destination) for destination, _ in restored]
    except Exception:
        for destination, backup in reversed(restored):
            if backup is not None and (destination.exists() or destination.is_symlink()):
                _move_path(destination, backup)
        for destination, staged_current in reversed(displaced):
            if staged_current.exists():
                _move_path(staged_current, destination)
        raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def uninstall(hermes_home: Path, *, force: bool = False) -> list[str]:
    """Transactionally remove installed code while preserving journal data."""
    hermes_home = hermes_home.expanduser().absolute()
    state_path = _state_path(hermes_home)
    _reject_symlink_chain(hermes_home, state_path)
    if not state_path.is_file() or state_path.is_symlink():
        raise FileNotFoundError("My Journal installation state was not found")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    raw_components = state.get("components") if isinstance(state, dict) else None
    if not isinstance(raw_components, list) or not raw_components:
        raise ValueError("installation state has no components")
    allowed = {target.as_posix() for _, target in COMPONENTS}
    components: list[tuple[Path, Path | None]] = []
    for item in raw_components:
        if not isinstance(item, dict) or item.get("destination") not in allowed:
            raise ValueError("installation state contains an unexpected destination")
        destination = hermes_home / item["destination"]
        _reject_symlink_chain(hermes_home, destination)
        expected_hash = item.get("installed_sha256")
        if not force:
            if not isinstance(expected_hash, str) or _tree_sha256(destination) != expected_hash:
                raise ValueError(f"installed component was modified: {destination}")
        backup_value = item.get("backup")
        backup = hermes_home / backup_value if isinstance(backup_value, str) else None
        if backup is not None:
            _reject_symlink_chain(hermes_home, backup)
        components.append((destination, backup))

    stage_root = Path(tempfile.mkdtemp(prefix=".my-journal-uninstall-", dir=hermes_home))
    displaced: list[tuple[Path, Path]] = []
    try:
        for destination, _ in components:
            if not destination.exists() and not destination.is_symlink():
                continue
            staged = stage_root / destination.relative_to(hermes_home)
            staged.parent.mkdir(parents=True, exist_ok=True)
            _move_path(destination, staged)
            displaced.append((destination, staged))
        for _, backup in components:
            if backup is not None and (backup.exists() or backup.is_symlink()):
                _remove_path(backup)
        state_path.unlink()
        return [str(destination) for destination, _ in components]
    except Exception:
        for destination, staged in reversed(displaced):
            if staged.exists():
                _move_path(staged, destination)
        raise
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", required=True, type=Path)
    parser.add_argument("--upgrade", action="store_true")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--restore", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    action.add_argument("--recover", action="store_true")
    parser.add_argument("--force", action="store_true", help="allow removal of modified installed code")
    args = parser.parse_args(argv)
    if args.force and not args.uninstall:
        parser.error("--force is valid only with --uninstall")
    if args.upgrade and (args.restore or args.uninstall or args.recover):
        parser.error("--upgrade cannot be combined with lifecycle actions")
    if args.restore:
        paths = restore(args.hermes_home)
    elif args.uninstall:
        paths = uninstall(args.hermes_home, force=args.force)
    elif args.recover:
        paths = recover(args.hermes_home)
    else:
        package_root = Path(__file__).resolve().parent
        paths = install(package_root, args.hermes_home, args.upgrade)
    for path in paths:
        print(path)
    if not args.restore and not args.uninstall and not args.recover:
        print("Enable with: hermes plugins enable my-journal")
        print("Then restart Hermes or start a new Hermes process.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
