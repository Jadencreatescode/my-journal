"""File descriptor anchored filesystem operations for My Journal."""

from __future__ import annotations

import os
import errno
import secrets
import stat
from pathlib import Path


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _absolute_parts(path: Path) -> tuple[Path, tuple[str, ...]]:
    absolute = path.expanduser().absolute()
    return Path(absolute.anchor), tuple(absolute.parts[1:])


def _open_directory(path: Path) -> int:
    anchor, parts = _absolute_parts(path)
    descriptor = os.open(str(anchor), _DIRECTORY_FLAGS)
    try:
        for part in parts:
            next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_or_create_directory(path: Path, mode: int) -> int:
    """Open a directory path, creating missing components through held parents."""
    anchor, parts = _absolute_parts(path)
    descriptor = os.open(str(anchor), _DIRECTORY_FLAGS)
    try:
        for part in parts:
            try:
                next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode, dir_fd=descriptor)
                except FileExistsError:
                    pass
                next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _relative_target(root: Path, target: Path) -> tuple[Path, tuple[str, ...]]:
    root_absolute = root.expanduser().absolute()
    target_absolute = target.expanduser().absolute()
    try:
        relative = target_absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise ValueError("target is outside trusted root") from exc
    if not relative.parts:
        raise ValueError("target must be below trusted root")
    return root_absolute, tuple(relative.parts)


def _open_parent(root: Path, target: Path) -> tuple[int, str]:
    root_absolute, parts = _relative_target(root, target)
    descriptor = _open_directory(root_absolute)
    try:
        for part in parts[:-1]:
            next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor, parts[-1]
    except Exception:
        os.close(descriptor)
        raise


def safe_mkdir_tree(root: Path, target: Path, *, mode: int = 0o700) -> None:
    """Create a directory tree below root using only held directory descriptors."""
    if mode < 0 or mode > 0o777:
        raise ValueError("invalid directory mode")
    root_absolute, parts = _relative_target(root, target)
    descriptor: int | None = None
    try:
        descriptor = _open_or_create_directory(root_absolute, mode)
        for part in parts:
            try:
                next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, mode, dir_fd=descriptor)
                except FileExistsError:
                    pass
                next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError("directory path contains a symlink or unsafe component") from exc
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def safe_open_regular_fd(root: Path, target: Path) -> int:
    """Open one regular file beneath root and return an anchored read only descriptor."""
    parent_descriptor: int | None = None
    file_descriptor: int | None = None
    try:
        parent_descriptor, name = _open_parent(root, target)
        file_descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_descriptor)
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("target is not a regular file")
        result = file_descriptor
        file_descriptor = None
        return result
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("target must not be a symlink") from exc
        raise ValueError("unsafe or unavailable file path") from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def descriptor_sqlite_uri(descriptor: int) -> str:
    """Return a read only SQLite URI for a held descriptor on Linux or macOS."""
    for base in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(base):
            return f"file:{base}/{descriptor}?mode=ro"
    raise RuntimeError("descriptor based SQLite access is unavailable on this platform")


def safe_read_text(root: Path, target: Path, *, max_bytes: int) -> str:
    """Read one regular UTF8 file beneath root without following symlinks."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    parent_descriptor: int | None = None
    file_descriptor: int | None = None
    try:
        parent_descriptor, name = _open_parent(root, target)
        file_descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_descriptor)
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("target is not a regular file")
        if metadata.st_size > max_bytes:
            raise ValueError("file exceeds configured byte limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_descriptor, min(65536, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("file exceeds configured byte limit")
        return b"".join(chunks).decode("utf-8")
    except FileNotFoundError:
        raise
    except UnicodeDecodeError as exc:
        raise ValueError("file is not valid UTF8") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("target must not be a symlink") from exc
        raise ValueError("unsafe or unavailable file path") from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def safe_unlink(root: Path, target: Path, *, missing_ok: bool = False) -> None:
    """Remove one regular file below root through its anchored parent descriptor."""
    parent_descriptor: int | None = None
    try:
        parent_descriptor, name = _open_parent(root, target)
        try:
            metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError("unlink target must be a regular file")
        os.unlink(name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def safe_atomic_write_text(root: Path, target: Path, text: str) -> None:
    """Atomically replace one UTF8 file beneath root without path re-resolution."""
    parent_descriptor: int | None = None
    temporary_name: str | None = None
    temporary_descriptor: int | None = None
    try:
        parent_descriptor, name = _open_parent(root, target)
        try:
            existing = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and stat.S_ISLNK(existing.st_mode):
            raise ValueError("target must not be a symlink")
        temporary_name = f".{name}.{secrets.token_hex(12)}.tmp"
        temporary_descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        data = text.encode("utf-8")
        written = 0
        while written < len(data):
            count = os.write(temporary_descriptor, data[written:])
            if count <= 0:
                raise OSError("short write")
            written += count
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = None
        os.replace(
            temporary_name,
            name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        temporary_name = None
        os.fsync(parent_descriptor)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("target must not be a symlink") from exc
        raise
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_name is not None and parent_descriptor is not None:
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
        if parent_descriptor is not None:
            os.close(parent_descriptor)
