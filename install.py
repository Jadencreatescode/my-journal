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
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple


COMPONENTS = (
    (Path("skills/note-taking/my-journal"), Path("skills/note-taking/my-journal")),
    (Path("skills/note-taking/journal"), Path("skills/note-taking/journal")),
    (Path("plugins/my-journal"), Path("plugins/my-journal")),
    (Path("scripts/my-journal-daily"), Path("scripts/my-journal-daily")),
)
_METADATA = ".my-journal"
_BACKUPS = "backups"
_STATE = "install-state.json"
_TRANSACTION = "install-transaction.json"
_MAX_JSON_BYTES = 1024 * 1024
_WSL_RUNTIME_CONFIG = "wsl-runtime.json"
_MAX_WSL_RUNTIME_CONFIG_BYTES = 4096
_ALLOWED_DESTINATIONS = frozenset(target.as_posix() for _, target in COMPONENTS)
_STAGE_RE = re.compile(r"^\.my-journal-(?:stage|restore|uninstall)-[A-Za-z0-9_-]+$")


class _LockedHome(NamedTuple):
    anchor: Path
    display: Path


def _home(hermes_home: Path) -> Path:
    home = hermes_home.expanduser().absolute()
    if not home.is_dir() or home.is_symlink():
        raise ValueError(f"Hermes home does not exist or is unsafe: {home}")
    return home


def _effective_journal_root(home: Path) -> Path:
    config = home / _METADATA / _WSL_RUNTIME_CONFIG
    try:
        config_metadata = config.lstat()
    except FileNotFoundError:
        return home / "journal"
    if config.is_symlink() or not config.is_file() or config_metadata.st_size > _MAX_WSL_RUNTIME_CONFIG_BYTES:
        raise ValueError("unsafe My Journal WSL runtime configuration")
    try:
        value = json.loads(config.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed My Journal WSL runtime configuration") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "wsl_hermes_home"}
        or value.get("schema_version") != 1
        or not isinstance(value.get("wsl_hermes_home"), str)
    ):
        raise ValueError("malformed My Journal WSL runtime configuration")
    raw_home = value["wsl_hermes_home"]
    wsl_home = Path(raw_home)
    if (
        not wsl_home.is_absolute()
        or "\x00" in raw_home
        or any(part in (".", "..") for part in wsl_home.parts)
        or wsl_home.as_posix() != raw_home.rstrip("/")
        or raw_home == "/"
    ):
        raise ValueError("malformed My Journal WSL runtime configuration")
    return wsl_home / "journal"


def _assert_no_receipt_bound_cron(home: Path) -> None:
    journal_root = _effective_journal_root(home)
    intent = journal_root / "cron-job-intent.json"
    try:
        intent_metadata = intent.lstat()
    except FileNotFoundError:
        intent_metadata = None
    if intent_metadata is not None:
        if intent.is_symlink() or not intent.is_file():
            raise ValueError("unsafe Journal cron ownership intent; run hermes journal cron-remove")
        if intent_metadata.st_size > _MAX_JSON_BYTES:
            raise ValueError("Journal cron ownership intent is oversized; run hermes journal cron-remove")
        try:
            intent_value = json.loads(intent.read_bytes())
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("malformed Journal cron ownership intent; run hermes journal cron-remove") from exc
        if not isinstance(intent_value, dict):
            raise ValueError("malformed Journal cron ownership intent; run hermes journal cron-remove")
        raise ValueError(
            "My Journal has durable cron ownership intent; run hermes journal cron-remove before restore or uninstall"
        )

    receipt = journal_root / "cron-job.json"
    try:
        receipt_metadata = receipt.lstat()
    except FileNotFoundError:
        return
    if receipt.is_symlink() or not receipt.is_file():
        raise ValueError("unsafe Journal cron ownership receipt; run hermes journal cron-remove")
    if receipt_metadata.st_size > _MAX_JSON_BYTES:
        raise ValueError("Journal cron ownership receipt is oversized; run hermes journal cron-remove")
    data = receipt.read_bytes()
    if len(data) > _MAX_JSON_BYTES:
        raise ValueError("Journal cron ownership receipt is oversized; run hermes journal cron-remove")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed Journal cron ownership receipt; run hermes journal cron-remove") from exc
    if not isinstance(value, dict) or not isinstance(value.get("job_id"), str) or not value["job_id"]:
        raise ValueError("malformed Journal cron ownership receipt; run hermes journal cron-remove")
    raise ValueError(
        "My Journal still owns an active cron job; run hermes journal cron-remove before restore or uninstall"
    )


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
    legacy_pattern = re.compile(
        rf"^{re.escape(destination.name)}\.backup-\d{{8}}T\d{{6}}Z(?:-[0-9a-f]{{8}})?$"
    )
    if path.parent == destination.parent and legacy_pattern.fullmatch(path.name):
        return path
    backup_name = destination.as_posix().replace("/", "__")
    metadata_pattern = re.compile(
        rf"^{re.escape(backup_name)}\.backup-\d{{8}}T\d{{6}}Z(?:-[0-9a-f]{{8}})?$"
    )
    if (
        path.parent == Path(_METADATA) / _BACKUPS
        and metadata_pattern.fullmatch(path.name)
    ):
        return path
    raise ValueError("installation state contains an unexpected backup")


def _backup_relative(destination: Path, stamp: str) -> Path:
    backup_name = destination.as_posix().replace("/", "__")
    return Path(_METADATA) / _BACKUPS / f"{backup_name}.backup-{stamp}"


def _stage(value: object, action: str) -> Path:
    path = _relative(value, label="stage root")
    expected_prefix = {"install": ".my-journal-stage-", "restore": ".my-journal-restore-", "uninstall": ".my-journal-uninstall-"}[action]
    if len(path.parts) != 1 or not _STAGE_RE.fullmatch(path.name) or not path.name.startswith(expected_prefix):
        raise ValueError("transaction contains an unexpected stage root")
    return path


def _reject_symlink_chain(hermes_home: Path, destination: Path) -> None:
    relative = destination.relative_to(hermes_home)
    descriptor = _open_directory_descriptor(hermes_home)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        for index, part in enumerate(relative.parts):
            try:
                metadata = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"installation destination contains a symlink: {destination}")
            if index < len(relative.parts) - 1:
                next_descriptor = os.open(part, directory_flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
    finally:
        os.close(descriptor)


def _canonical_descriptor_path(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    parts = absolute.parts
    if sys.platform == "darwin" and len(parts) > 1 and parts[1] in {"etc", "tmp", "var"}:
        root_child = os.path.join(absolute.anchor or "/", parts[1])
        if os.path.islink(root_child):
            resolved_root_child = Path(os.path.realpath(root_child))
            absolute = resolved_root_child.joinpath(*parts[2:])
    return absolute


def _open_directory_descriptor(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    expanded = path.expanduser()
    if not expanded.is_absolute():
        descriptor = os.open(".", flags)
        try:
            for part in expanded.parts:
                if part in ("", "."):
                    continue
                if part == "..":
                    raise ValueError("relative descriptor path cannot traverse upward")
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    absolute = _canonical_descriptor_path(expanded)
    parts = absolute.parts
    if len(parts) >= 5 and parts[:4] == ("/", "proc", "self", "fd") and parts[4].isdigit():
        descriptor = os.dup(int(parts[4]))
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("locked Hermes home descriptor is not a directory")
            for part in parts[5:]:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
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


def _mkdir_anchored(root: Path, destination: Path) -> None:
    relative = destination.relative_to(root)
    descriptor = _open_directory_descriptor(root)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        for part in relative.parts:
            try:
                os.mkdir(part, 0o755, dir_fd=descriptor)
            except FileExistsError:
                pass
            else:
                os.fsync(descriptor)
            try:
                metadata = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise ValueError(f"directory path is unsafe: {destination}")
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
            except OSError:
                raise ValueError(f"directory path is unsafe: {destination}") from None
            os.close(descriptor)
            descriptor = next_descriptor
    finally:
        os.close(descriptor)


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
    descriptor = _open_directory_descriptor(home)
    caller_directory: int | None = None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BlockingIOError("another My Journal lifecycle operation is in progress") from None
        if sys.platform == "darwin":
            caller_directory = os.open(".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
            os.fchdir(descriptor)
            try:
                yield _LockedHome(anchor=Path("."), display=home)
            finally:
                os.fchdir(caller_directory)
        else:
            yield _LockedHome(anchor=Path(f"/proc/self/fd/{descriptor}"), display=home)
    finally:
        if caller_directory is not None:
            os.close(caller_directory)
        os.close(descriptor)


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
        required_keys = {"destination", "backup", "installed_sha256"} if require_hash else {"destination", "backup"}
        if set(item) != required_keys:
            raise ValueError("installation metadata component is malformed")
        relative = _destination(item["destination"])
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
    required = {destination.as_posix() for _, destination in COMPONENTS}
    if seen != required:
        raise ValueError("installation metadata does not contain the complete component set")
    return parsed


def _validated_state(raw: object, home: Path) -> list[dict]:
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "components"}:
        raise ValueError("installation state is malformed")
    version = raw["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ValueError("unsupported installation state schema version")
    parsed = _parse_components(raw["components"], home, require_hash=True)
    backup_presence = [item["backup"] is not None for item in parsed]
    if any(backup_presence) and not all(backup_presence):
        raise ValueError("installation state contains mixed backup ownership")
    return parsed


def _remove_state_backups(home: Path, raw: object) -> None:
    if raw is None:
        return
    for item in _validated_state(raw, home):
        backup = item["backup"]
        if backup is not None and _exists(backup):
            _remove_path(backup)


def _new_stage(home: Path, action: str) -> Path:
    created = Path(tempfile.mkdtemp(prefix=f".my-journal-{action}-", dir=home))
    stage = home / created.name
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
    base_keys = {"schema_version", "action", "status", "stage_root", "components", "previous_state"}
    if set(transaction) not in (base_keys, base_keys | {"final_state"}):
        raise ValueError("installation transaction is malformed")
    version = transaction["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        raise ValueError("unsupported installation transaction schema version")
    previous_state = transaction.get("previous_state")
    if previous_state is not None:
        _validated_state(previous_state, home)
    if "final_state" in transaction and transaction["final_state"] is not None:
        _validated_state(transaction["final_state"], home)
    action = transaction.get("action")
    status = transaction.get("status", "active")
    if action not in {"install", "restore", "uninstall"} or status not in {"active", "committing", "committed"}:
        raise ValueError("unsupported installation transaction")
    stage_relative = _stage(transaction.get("stage_root"), action)
    stage = home / stage_relative
    _reject_symlink_chain(home, stage)
    components = _parse_components(transaction.get("components"), home, require_hash=False)
    return transaction, action, status, stage, components


def _recover_locked(home: Path, display_home: Path | None = None) -> list[str]:
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
            _remove_state_backups(home, transaction.get("previous_state"))
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
    output_home = display_home if display_home is not None else home
    return [str(output_home / item["relative"]) for item in components]


def install(package_root: Path, hermes_home: Path, upgrade: bool) -> list[str]:
    with _lifecycle_lock(hermes_home) as locked_home:
        home = locked_home.anchor
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
        if upgrade and existing:
            _assert_no_receipt_bound_cron(home)
        if existing and not upgrade:
            displayed_existing = [
                str(locked_home.display / destination.relative_to(home))
                for destination in existing
            ]
            raise FileExistsError(
                "components already exist; rerun with --upgrade: " + ", ".join(displayed_existing)
            )
        if upgrade and existing and len(existing) != len(destinations):
            missing_destinations = [
                str(locked_home.display / destination.relative_to(home))
                for _, destination in destinations
                if not _exists(destination)
            ]
            raise ValueError(
                "partial preexisting component set; repair or remove it before upgrade; "
                "missing: " + ", ".join(missing_destinations)
            )
        try:
            previous_state = _read_json(home, _STATE, missing_message="")
            _validated_state(previous_state, home)
        except FileNotFoundError:
            previous_state = None
        stage = _new_stage(home, "stage")
        try:
            staged: list[tuple[Path, Path]] = []
            for source, destination in destinations:
                staged_source = stage / destination.relative_to(home)
                _mkdir_anchored(stage, staged_source.parent)
                shutil.copytree(source, staged_source, symlinks=True)
                staged.append((staged_source, destination))
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
            components: list[dict] = []
            for staged_source, destination in staged:
                relative = destination.relative_to(home)
                backup_relative = _backup_relative(relative, stamp) if _exists(destination) else None
                components.append({"relative": relative, "destination": destination, "backup_relative": backup_relative, "backup": home / backup_relative if backup_relative else None, "staged": staged_source})
            final_state = {"schema_version": 1, "components": [{"destination": item["relative"].as_posix(), "backup": item["backup_relative"].as_posix() if item["backup_relative"] else None, "installed_sha256": _tree_sha256(item["staged"])} for item in components]}
            transaction = _transaction_payload("install", stage, home, components, previous_state, final_state)
        except BaseException:
            _remove_path(stage)
            raise
        _persist_transaction(home, stage, transaction)
        try:
            for item in components:
                _mkdir_anchored(home, item["destination"].parent)
                if item["backup"] is not None:
                    _mkdir_anchored(home, item["backup"].parent)
                    if _exists(item["backup"]):
                        raise FileExistsError(f"backup destination already exists: {item['backup']}")
                    _move_path(item["destination"], item["backup"])
                _move_path(item["staged"], item["destination"])
            _set_status(home, transaction, "committing")
            _write_state(home, final_state)
            _set_status(home, transaction, "committed")
            if _exists(stage):
                _remove_path(stage)
            _remove_state_backups(home, previous_state)
            _remove_metadata(home, _TRANSACTION, missing_ok=False)
            return [str(locked_home.display / item["relative"]) for item in components]
        except Exception:
            _recover_locked(home)
            raise


def recover(hermes_home: Path) -> list[str]:
    with _lifecycle_lock(hermes_home) as locked_home:
        return _recover_locked(locked_home.anchor, locked_home.display)


def restore(hermes_home: Path, *, force: bool = False) -> list[str]:
    with _lifecycle_lock(hermes_home) as locked_home:
        home = locked_home.anchor
        _assert_no_receipt_bound_cron(home)
        state = _read_json(home, _STATE, missing_message="no restorable My Journal installation state")
        components = _validated_state(state, home)
        for item in components:
            if not force and _tree_sha256(item["destination"]) != item["installed_sha256"]:
                raise ValueError(
                    f"installed component was modified: {locked_home.display / item['relative']}"
                )
            if item["backup"] is not None and not _exists(item["backup"]):
                displayed_backup = locked_home.display / item["backup"].relative_to(home)
                raise FileNotFoundError(f"installation backup is missing: {displayed_backup}")
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
                _mkdir_anchored(stage, staged.parent)
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
            return [str(locked_home.display / item["relative"]) for item in components]
        except Exception:
            _recover_locked(home)
            raise


def uninstall(hermes_home: Path, *, force: bool = False) -> list[str]:
    with _lifecycle_lock(hermes_home) as locked_home:
        home = locked_home.anchor
        _assert_no_receipt_bound_cron(home)
        state = _read_json(home, _STATE, missing_message="My Journal installation state was not found")
        components = _validated_state(state, home)
        for item in components:
            if not force and _tree_sha256(item["destination"]) != item["installed_sha256"]:
                raise ValueError(
                    f"installed component was modified: {locked_home.display / item['relative']}"
                )
        stage = _new_stage(home, "uninstall")
        transaction = _transaction_payload("uninstall", stage, home, components, state)
        _persist_transaction(home, stage, transaction)
        try:
            for item in components:
                if not _exists(item["destination"]):
                    continue
                staged = stage / item["relative"]
                _mkdir_anchored(stage, staged.parent)
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
            return [str(locked_home.display / item["relative"]) for item in components]
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
