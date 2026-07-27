#!/usr/bin/env python3
"""Install the My Journal skills and plugin into a Hermes home."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
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
_METADATA = ".my-journal"
_STATE = "install-state.json"
_TRANSACTION = "install-transaction.json"
_LOCK = "lifecycle.lock"
_MAX_JSON_BYTES = 1024 * 1024
_ALLOWED_DESTINATIONS = frozenset(target.as_posix() for _, target in COMPONENTS)
_STAGE_RE = re.compile(r"^\.my-journal-(?:stage|restore|uninstall)-[A-Za-z0-9_-]+$")


def _home(hermes_home: Path) -> Path:
    home = hermes_home.expanduser().absolute()
    if not home.is_dir() or home.is_symlink():
        raise ValueError(f"Hermes home does not exist or is unsafe: {home}")
    return home


def _relative(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"{label} must be a normalized relative path")
    if path.as_posix() != value:
        raise ValueError(f"{label} must be a normalized relative path")
    return path


def _destination(value: object) -> Path:
    path = _relative(value, label="destination")
    if path.as_posix() not in _ALLOWED_DESTINATIONS:
        raise ValueError("installation state contains an unexpected destination")
    return path


def _backup(value: object, destination: Path) -> Path | None:
    if value is None:
        return None
    path = _relative(value, label="backup")
    pattern = re.compile(
        rf"^{re.escape(destination.name)}\.backup-\d{{8}}T\d{{6}}Z(?:-[0-9a-f]{{8}})?$"
    )
    if path.parent != destination.parent or not pattern.fullmatch(path.name):
        raise ValueError("installation state contains an unexpected backup")
    return path


def _stage(value: object, action: str) -> Path:
    path = _relative(value, label="stage root")
    expected_prefix = {"install": ".my-journal-stage-", "restore": ".my-journal-restore-", "uninstall": ".my-journal-uninstall-"}[action]
    if len(path.parts) != 1 or not _STAGE_RE.fullmatch(path.name) or not path.name.startswith(expected_prefix):
        raise ValueError("transaction contains an unexpected stage root")
    return path


def _reject_symlink_chain(hermes_home: Path, destination: Path) -> None:
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
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(absolute.anchor or "/", flags)
    try:
        for part in absolute.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _metadata_descriptor(hermes_home: Path, *, create: bool) -> int:
    home_fd = _open_directory_descriptor(hermes_home)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        if create:
            try:
                os.mkdir(_METADATA, 0o700, dir_fd=home_fd)
                os.fsync(home_fd)
            except FileExistsError:
                pass
        return os.open(_METADATA, flags, dir_fd=home_fd)
    finally:
        os.close(home_fd)


def _move_path(source: Path, destination: Path) -> None:
    source_parent = _open_directory_descriptor(source.parent)
    destination_parent: int | None = None
    try:
        destination_parent = _open_directory_descriptor(destination.parent)
        os.rename(source.name, destination.name, src_dir_fd=source_parent, dst_dir_fd=destination_parent)
        os.fsync(destination_parent)
        if source.parent != destination.parent:
            os.fsync(source_parent)
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


def _exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _state_path(hermes_home: Path) -> Path:
    return hermes_home / _METADATA / _STATE


def _transaction_path(hermes_home: Path) -> Path:
    return hermes_home / _METADATA / _TRANSACTION


def _atomic_json(hermes_home: Path, name: str, payload: dict) -> None:
    if name not in {_STATE, _TRANSACTION}:
        raise ValueError("unexpected metadata destination")
    directory_fd = _metadata_descriptor(hermes_home, create=True)
    temporary: str | None = None
    descriptor: int | None = None
    stem = name.removesuffix(".json")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        for _ in range(128):
            candidate = f".{stem}.{secrets.token_hex(16)}.tmp"
            try:
                descriptor = os.open(candidate, flags, 0o600, dir_fd=directory_fd)
                temporary = candidate
                break
            except FileExistsError:
                continue
        if descriptor is None or temporary is None:
            raise FileExistsError("could not allocate a unique metadata temporary file")
        data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temporary = None
        os.fsync(directory_fd)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory_fd)
            os.fsync(directory_fd)
        raise
    finally:
        os.close(directory_fd)


def _write_state(hermes_home: Path, payload: dict) -> None:
    _atomic_json(hermes_home, _STATE, payload)


def _write_transaction(hermes_home: Path, payload: dict) -> None:
    _atomic_json(hermes_home, _TRANSACTION, payload)


def _persist_transaction(hermes_home: Path, stage: Path, payload: dict) -> None:
    try:
        _write_transaction(hermes_home, payload)
    except BaseException:
        try:
            directory_fd = _metadata_descriptor(hermes_home, create=False)
        except FileNotFoundError:
            durable = False
        else:
            try:
                try:
                    os.stat(_TRANSACTION, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    durable = False
                else:
                    durable = True
            finally:
                os.close(directory_fd)
        if not durable:
            _remove_path(stage)
        raise


def _read_json(hermes_home: Path, name: str, *, missing_message: str) -> dict:
    if name not in {_STATE, _TRANSACTION}:
        raise ValueError("unexpected metadata source")
    try:
        directory_fd = _metadata_descriptor(hermes_home, create=False)
    except FileNotFoundError:
        raise FileNotFoundError(missing_message) from None
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(name, flags, dir_fd=directory_fd)
        except FileNotFoundError:
            raise FileNotFoundError(missing_message) from None
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_JSON_BYTES:
            raise ValueError("metadata file is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = _MAX_JSON_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > _MAX_JSON_BYTES:
            raise ValueError("metadata file is too large")
        value = json.loads(data.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("metadata must be a JSON object")
        return value
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _remove_metadata(hermes_home: Path, name: str, *, missing_ok: bool = True) -> None:
    if name not in {_STATE, _TRANSACTION}:
        raise ValueError("unexpected metadata destination")
    try:
        directory_fd = _metadata_descriptor(hermes_home, create=False)
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    try:
        try:
            os.unlink(name, dir_fd=directory_fd)
        except FileNotFoundError:
            if not missing_ok:
                raise
        else:
            os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@contextlib.contextmanager
def _lifecycle_lock(hermes_home: Path):
    home = _home(hermes_home)
    directory_fd = _metadata_descriptor(home, create=True)
    descriptor: int | None = None
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(_LOCK, flags, 0o600, dir_fd=directory_fd)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BlockingIOError("another My Journal lifecycle operation is in progress") from None
        yield home
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _tree_sha256(root: Path) -> str:
    """Fingerprint one exact descriptor anchored component tree snapshot."""
    digest = hashlib.sha256()
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    root_descriptor = _open_directory_descriptor(root)

    def walk(descriptor: int, prefix: Path) -> None:
        for name in sorted(os.listdir(descriptor)):
            relative = prefix / name
            if "__pycache__" in relative.parts or relative.suffix == ".pyc":
                continue
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            mode = stat.S_IMODE(metadata.st_mode)
            encoded = relative.as_posix().encode("utf-8")
            if stat.S_ISDIR(metadata.st_mode):
                digest.update(b"D\0" + encoded + b"\0" + f"{mode:o}".encode() + b"\0")
                child = os.open(name, directory_flags, dir_fd=descriptor)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                        raise ValueError("installed component changed during fingerprinting")
                    walk(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(metadata.st_mode):
                file_descriptor = os.open(name, file_flags, dir_fd=descriptor)
                try:
                    opened = os.fstat(file_descriptor)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
                    ):
                        raise ValueError("installed component changed during fingerprinting")
                    digest.update(b"F\0" + encoded + b"\0" + f"{mode:o}".encode() + b"\0")
                    while chunk := os.read(file_descriptor, 65536):
                        digest.update(chunk)
                    digest.update(b"\0")
                finally:
                    os.close(file_descriptor)
            else:
                raise ValueError(f"installed component contains an unsupported entry: {relative}")

    try:
        root_metadata = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise ValueError(f"installed component is unsafe: {root}")
        root_mode = stat.S_IMODE(root_metadata.st_mode)
        digest.update(f"D\0.\0{root_mode:o}\0".encode())
        walk(root_descriptor, Path())
        return digest.hexdigest()
    finally:
        os.close(root_descriptor)


def _parse_components(raw: object, home: Path, *, require_hash: bool) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("installation metadata has no components")
    parsed: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("installation metadata component is malformed")
        relative = _destination(item.get("destination"))
        if relative.as_posix() in seen:
            raise ValueError("installation metadata contains a duplicate destination")
        seen.add(relative.as_posix())
        backup = _backup(item.get("backup"), relative)
        expected = item.get("installed_sha256")
        if require_hash and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise ValueError("installation state contains an invalid fingerprint")
        destination = home / relative
        backup_path = home / backup if backup is not None else None
        _reject_symlink_chain(home, destination)
        if backup_path is not None:
            _reject_symlink_chain(home, backup_path)
        parsed.append({"relative": relative, "destination": destination, "backup_relative": backup, "backup": backup_path, "installed_sha256": expected})
    return parsed


def _new_stage(home: Path, action: str) -> Path:
    stage = Path(tempfile.mkdtemp(prefix=f".my-journal-{action}-", dir=home))
    home_fd = _open_directory_descriptor(home)
    try:
        os.fsync(home_fd)
    finally:
        os.close(home_fd)
    return stage


def _transaction_payload(action: str, stage: Path, home: Path, components: list[dict], previous_state: dict | None, final_state: dict | None = None) -> dict:
    payload = {
        "schema_version": 1,
        "action": action,
        "status": "active",
        "stage_root": stage.relative_to(home).as_posix(),
        "components": [
            {"destination": item["relative"].as_posix(), "backup": item["backup_relative"].as_posix() if item["backup_relative"] else None}
            for item in components
        ],
        "previous_state": previous_state,
    }
    if final_state is not None:
        payload["final_state"] = final_state
    return payload


def _set_status(home: Path, transaction: dict, status: str) -> None:
    transaction["status"] = status
    _write_transaction(home, transaction)


def _restore_previous_state(home: Path, transaction: dict) -> None:
    previous = transaction.get("previous_state")
    if previous is None:
        _remove_metadata(home, _STATE)
    elif isinstance(previous, dict):
        _write_state(home, previous)
    else:
        raise ValueError("transaction contains malformed previous state")


def _validated_transaction(home: Path) -> tuple[dict, str, str, Path, list[dict]]:
    transaction = _read_json(home, _TRANSACTION, missing_message="no interrupted My Journal transaction")
    action = transaction.get("action")
    status = transaction.get("status", "active")
    if action not in {"install", "restore", "uninstall"} or status not in {"active", "committing", "committed"}:
        raise ValueError("unsupported installation transaction")
    stage_relative = _stage(transaction.get("stage_root"), action)
    stage = home / stage_relative
    _reject_symlink_chain(home, stage)
    components = _parse_components(transaction.get("components"), home, require_hash=False)
    return transaction, action, status, stage, components


def _recover_locked(home: Path) -> list[str]:
    transaction, action, status, stage, components = _validated_transaction(home)
    if status == "active":
        if action == "install":
            for item in reversed(components):
                destination, backup = item["destination"], item["backup"]
                staged = stage / item["relative"]
                if backup is not None and _exists(backup):
                    if _exists(destination):
                        _remove_path(destination)
                    _move_path(backup, destination)
                elif backup is None and not _exists(staged) and _exists(destination):
                    _remove_path(destination)
        else:
            for item in reversed(components):
                destination, backup = item["destination"], item["backup"]
                staged = stage / item["relative"]
                if not _exists(staged):
                    continue
                if action == "restore" and backup is not None and not _exists(backup) and _exists(destination):
                    _move_path(destination, backup)
                elif _exists(destination):
                    _remove_path(destination)
                _move_path(staged, destination)
        _restore_previous_state(home, transaction)
    else:
        if action == "install":
            final = transaction.get("final_state")
            if isinstance(final, dict):
                _write_state(home, final)
        elif action == "restore":
            final = transaction.get("final_state")
            if final is None:
                _remove_metadata(home, _STATE)
            elif isinstance(final, dict):
                _write_state(home, final)
            else:
                raise ValueError("restore transaction has malformed final state")
        else:
            _remove_metadata(home, _STATE)
            for item in components:
                backup = item["backup"]
                if backup is not None and _exists(backup):
                    _remove_path(backup)
    if _exists(stage):
        _remove_path(stage)
    _remove_metadata(home, _TRANSACTION, missing_ok=False)
    return [str(item["destination"]) for item in components]


def install(package_root: Path, hermes_home: Path, upgrade: bool) -> list[str]:
    with _lifecycle_lock(hermes_home) as home:
        try:
            _read_json(home, _TRANSACTION, missing_message="")
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError("an interrupted lifecycle transaction must be recovered first")
        destinations = [(package_root / source, home / target) for source, target in COMPONENTS]
        for _, destination in destinations:
            _reject_symlink_chain(home, destination)
        missing = [str(source) for source, _ in destinations if not source.is_dir()]
        if missing:
            raise ValueError("package is incomplete: " + ", ".join(missing))
        existing = [destination for _, destination in destinations if _exists(destination)]
        if existing and not upgrade:
            raise FileExistsError("components already exist; rerun with --upgrade: " + ", ".join(str(path) for path in existing))
        try:
            previous_state = _read_json(home, _STATE, missing_message="")
        except FileNotFoundError:
            previous_state = None
        stage = _new_stage(home, "stage")
        try:
            staged: list[tuple[Path, Path]] = []
            for source, destination in destinations:
                staged_source = stage / destination.relative_to(home)
                staged_source.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, staged_source, symlinks=True)
                staged.append((staged_source, destination))
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
            components: list[dict] = []
            for staged_source, destination in staged:
                relative = destination.relative_to(home)
                backup_relative = relative.with_name(f"{relative.name}.backup-{stamp}") if _exists(destination) else None
                components.append({"relative": relative, "destination": destination, "backup_relative": backup_relative, "backup": home / backup_relative if backup_relative else None, "staged": staged_source})
            final_state = {"schema_version": 1, "components": [{"destination": item["relative"].as_posix(), "backup": item["backup_relative"].as_posix() if item["backup_relative"] else None, "installed_sha256": _tree_sha256(item["staged"])} for item in components]}
            transaction = _transaction_payload("install", stage, home, components, previous_state, final_state)
        except BaseException:
            _remove_path(stage)
            raise
        _persist_transaction(home, stage, transaction)
        try:
            for item in components:
                item["destination"].parent.mkdir(parents=True, exist_ok=True)
                if item["backup"] is not None:
                    if _exists(item["backup"]):
                        raise FileExistsError(f"backup destination already exists: {item['backup']}")
                    _move_path(item["destination"], item["backup"])
                _move_path(item["staged"], item["destination"])
            _set_status(home, transaction, "committing")
            _write_state(home, final_state)
            _set_status(home, transaction, "committed")
            if _exists(stage):
                _remove_path(stage)
            _remove_metadata(home, _TRANSACTION, missing_ok=False)
            return [str(item["destination"]) for item in components]
        except Exception:
            _recover_locked(home)
            raise


def recover(hermes_home: Path) -> list[str]:
    with _lifecycle_lock(hermes_home) as home:
        return _recover_locked(home)


def restore(hermes_home: Path, *, force: bool = False) -> list[str]:
    with _lifecycle_lock(hermes_home) as home:
        state = _read_json(home, _STATE, missing_message="no restorable My Journal installation state")
        components = _parse_components(state.get("components"), home, require_hash=True)
        for item in components:
            if not force and _tree_sha256(item["destination"]) != item["installed_sha256"]:
                raise ValueError(f"installed component was modified: {item['destination']}")
            if item["backup"] is not None and not _exists(item["backup"]):
                raise FileNotFoundError(f"installation backup is missing: {item['backup']}")
        managed = [item for item in components if item["backup"] is not None]
        final_state = None
        if managed:
            final_state = {"schema_version": 1, "components": [{"destination": item["relative"].as_posix(), "backup": None, "installed_sha256": _tree_sha256(item["backup"])} for item in managed]}
        stage = _new_stage(home, "restore")
        transaction = _transaction_payload("restore", stage, home, components, state, final_state)
        _persist_transaction(home, stage, transaction)
        try:
            for item in components:
                staged = stage / item["relative"]
                staged.parent.mkdir(parents=True, exist_ok=True)
                if _exists(item["destination"]):
                    _move_path(item["destination"], staged)
                if item["backup"] is not None:
                    _move_path(item["backup"], item["destination"])
            _set_status(home, transaction, "committing")
            if final_state is None:
                _remove_metadata(home, _STATE, missing_ok=False)
            else:
                _write_state(home, final_state)
            _set_status(home, transaction, "committed")
            if _exists(stage):
                _remove_path(stage)
            _remove_metadata(home, _TRANSACTION, missing_ok=False)
            return [str(item["destination"]) for item in components]
        except Exception:
            _recover_locked(home)
            raise


def uninstall(hermes_home: Path, *, force: bool = False) -> list[str]:
    with _lifecycle_lock(hermes_home) as home:
        state = _read_json(home, _STATE, missing_message="My Journal installation state was not found")
        components = _parse_components(state.get("components"), home, require_hash=True)
        for item in components:
            if not force and _tree_sha256(item["destination"]) != item["installed_sha256"]:
                raise ValueError(f"installed component was modified: {item['destination']}")
        stage = _new_stage(home, "uninstall")
        transaction = _transaction_payload("uninstall", stage, home, components, state)
        _persist_transaction(home, stage, transaction)
        try:
            for item in components:
                if not _exists(item["destination"]):
                    continue
                staged = stage / item["relative"]
                staged.parent.mkdir(parents=True, exist_ok=True)
                _move_path(item["destination"], staged)
            _set_status(home, transaction, "committing")
            _remove_metadata(home, _STATE, missing_ok=False)
            _set_status(home, transaction, "committed")
            if _exists(stage):
                _remove_path(stage)
            for item in components:
                if item["backup"] is not None and _exists(item["backup"]):
                    _remove_path(item["backup"])
            _remove_metadata(home, _TRANSACTION, missing_ok=False)
            return [str(item["destination"]) for item in components]
        except Exception:
            if transaction.get("status") == "active":
                _recover_locked(home)
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", required=True, type=Path)
    parser.add_argument("--upgrade", action="store_true")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--restore", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    action.add_argument("--recover", action="store_true")
    parser.add_argument("--force", action="store_true", help="allow restore or removal of modified installed code")
    args = parser.parse_args(argv)
    if args.force and not (args.restore or args.uninstall):
        parser.error("--force is valid only with --restore or --uninstall")
    if args.upgrade and (args.restore or args.uninstall or args.recover):
        parser.error("--upgrade cannot be combined with lifecycle actions")
    if args.restore:
        paths = restore(args.hermes_home, force=args.force)
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
