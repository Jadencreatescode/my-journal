"""Native Windows handle based exclusive move helper.

The WSL runtime sends one bounded JSON request on standard input. Validation remains
platform neutral so the request boundary is testable on every host. Filesystem work
uses Windows parent and source handles, never an ordinary path based rename.
"""

from __future__ import annotations

import ctypes
import json
import re
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any


_REQUEST_FIELDS = {
    "source_parent",
    "destination_parent",
    "source_name",
    "destination_name",
    "source_parent_inode",
    "destination_parent_inode",
    "source_inode",
    "directory",
}
_DELETE = 0x00010000
_FILE_READ_ATTRIBUTES = 0x00000080
_SYNCHRONIZE = 0x00100000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_FILE_SHARE_DELETE = 0x4
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_OPEN = 1
_FILE_DIRECTORY_FILE = 0x1
_FILE_NON_DIRECTORY_FILE = 0x40
_FILE_SYNCHRONOUS_IO_NONALERT = 0x20
_FILE_OPEN_REPARSE_POINT = 0x00200000
_OBJ_CASE_INSENSITIVE = 0x40
_FILE_ID_INFO_CLASS = 18
_FILE_RENAME_INFORMATION_CLASS = 10
_STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
_STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_MAX_REQUEST_BYTES = 65_536


class _FILE_ID_128(ctypes.Structure):
    _fields_ = [("Identifier", ctypes.c_ubyte * 16)]


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_ulonglong),
        ("FileId", _FILE_ID_128),
    ]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]


def _validated_drive_path(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) > 32767 or re.fullmatch(
        r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\x00-\x1f]+(?:\\|$))*", value
    ) is None:
        raise ValueError(f"exclusive move request has invalid {field}")
    if any(component in {".", ".."} for component in value[3:].split("\\")):
        raise ValueError(f"exclusive move request has invalid {field}")
    return value


def _validated_name(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or value in {".", ".."}
        or re.search(r"[\\/:*?\"<>|\x00-\x1f]", value) is not None
    ):
        raise ValueError(f"exclusive move request has invalid {field}")
    return value


def validate_request(request: object) -> dict:
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise ValueError("exclusive move request has invalid fields")
    result = dict(request)
    source_parent = _validated_drive_path(result["source_parent"], "source_parent")
    destination_parent = _validated_drive_path(
        result["destination_parent"], "destination_parent"
    )
    if source_parent[:2].casefold() != destination_parent[:2].casefold():
        raise ValueError("exclusive move request crosses Windows volumes")
    _validated_name(result["source_name"], "source_name")
    _validated_name(result["destination_name"], "destination_name")
    for field in (
        "source_parent_inode",
        "destination_parent_inode",
        "source_inode",
    ):
        value = result[field]
        if type(value) is not int or not 2 < value < 2**128:
            raise ValueError(f"exclusive move request has invalid {field}")
    if type(result["directory"]) is not bool:
        raise ValueError("exclusive move request has invalid directory")
    return result


def _last_windows_error() -> OSError:
    win_error: Any = getattr(ctypes, "WinError")
    get_last_error: Any = getattr(ctypes, "get_last_error")
    return win_error(get_last_error())


class _WindowsApi:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("native Windows exclusive move helper requires Windows")
        win_dll: Any = getattr(ctypes, "WinDLL")
        self.kernel32: Any = win_dll("kernel32", use_last_error=True)
        self.ntdll: Any = win_dll("ntdll", use_last_error=True)
        self.kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.kernel32.CreateFileW.restype = wintypes.HANDLE
        self.kernel32.GetFileInformationByHandleEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.ntdll.NtCreateFile.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            ctypes.POINTER(_OBJECT_ATTRIBUTES),
            ctypes.POINTER(_IO_STATUS_BLOCK),
            ctypes.c_void_p,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
        ]
        self.ntdll.NtCreateFile.restype = ctypes.c_long
        self.ntdll.NtSetInformationFile.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_IO_STATUS_BLOCK),
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.c_int,
        ]
        self.ntdll.NtSetInformationFile.restype = ctypes.c_long
        self.ntdll.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
        self.ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG

    def close(self, handle: int | None) -> None:
        if handle not in (None, 0, _INVALID_HANDLE_VALUE):
            self.kernel32.CloseHandle(handle)

    def error_from_status(self, status: int, message: str) -> OSError:
        code = self.ntdll.RtlNtStatusToDosError(ctypes.c_long(status).value)
        if code in {80, 183}:
            return FileExistsError(code, message)
        return OSError(code, message)

    def identity(self, handle: int) -> tuple[int, int]:
        info = _FILE_ID_INFO()
        if not self.kernel32.GetFileInformationByHandleEx(
            handle,
            _FILE_ID_INFO_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            raise _last_windows_error()
        return info.VolumeSerialNumber, int.from_bytes(
            bytes(info.FileId.Identifier), "little"
        )

    def open_absolute(self, path: str) -> int:
        handle = self.kernel32.CreateFileW(
            path,
            _FILE_READ_ATTRIBUTES,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            raise _last_windows_error()
        return handle

    def open_relative(
        self,
        parent: int,
        name: str,
        *,
        directory: bool,
        desired_access: int = _FILE_READ_ATTRIBUTES,
    ) -> tuple[int | None, int]:
        name_buffer = ctypes.create_unicode_buffer(name)
        name_bytes = len(name.encode("utf-16-le"))
        unicode_name = _UNICODE_STRING(
            name_bytes,
            name_bytes,
            ctypes.cast(name_buffer, wintypes.LPWSTR),
        )
        attributes = _OBJECT_ATTRIBUTES(
            ctypes.sizeof(_OBJECT_ATTRIBUTES),
            parent,
            ctypes.pointer(unicode_name),
            _OBJ_CASE_INSENSITIVE,
            None,
            None,
        )
        status_block = _IO_STATUS_BLOCK()
        result = wintypes.HANDLE()
        options = (
            _FILE_SYNCHRONOUS_IO_NONALERT
            | _FILE_OPEN_REPARSE_POINT
            | (_FILE_DIRECTORY_FILE if directory else _FILE_NON_DIRECTORY_FILE)
        )
        status = self.ntdll.NtCreateFile(
            ctypes.byref(result),
            desired_access | _SYNCHRONIZE,
            ctypes.byref(attributes),
            ctypes.byref(status_block),
            None,
            0,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            _FILE_OPEN,
            options,
            None,
            0,
        )
        if status < 0:
            return None, status & 0xFFFFFFFF
        return result.value, 0

    def rename_relative(self, source: int, destination_parent: int, name: str) -> None:
        encoded = name.encode("utf-16-le")
        name_offset = 20
        structure_size = 24
        total = structure_size + len(encoded)
        raw = ctypes.create_string_buffer(total)
        ctypes.c_ubyte.from_buffer(raw, 0).value = 0
        ctypes.c_void_p.from_buffer(raw, 8).value = destination_parent
        ctypes.c_uint32.from_buffer(raw, 16).value = len(encoded)
        raw[name_offset : name_offset + len(encoded)] = encoded
        status_block = _IO_STATUS_BLOCK()
        status = self.ntdll.NtSetInformationFile(
            source,
            ctypes.byref(status_block),
            raw,
            total,
            _FILE_RENAME_INFORMATION_CLASS,
        )
        if status < 0:
            raise self.error_from_status(status, "exclusive move rename failed")


def _require_wsl_identity(
    native_identity: tuple[int, int], expected_wsl_inode: int, label: str
) -> None:
    if native_identity[1] + 2 != expected_wsl_inode:
        raise ValueError(f"exclusive move {label} identity mismatch")


def path_identity(path: str | Path) -> tuple[int, int]:
    api = _WindowsApi()
    handle = api.open_absolute(str(path))
    try:
        return api.identity(handle)
    finally:
        api.close(handle)


def move(request: object) -> dict:
    value = validate_request(request)
    api = _WindowsApi()
    source_parent = destination_parent = source = destination = None
    try:
        source_parent = api.open_absolute(value["source_parent"])
        destination_parent = api.open_absolute(value["destination_parent"])
        source_parent_identity = api.identity(source_parent)
        destination_parent_identity = api.identity(destination_parent)
        _require_wsl_identity(
            source_parent_identity, value["source_parent_inode"], "source parent"
        )
        _require_wsl_identity(
            destination_parent_identity,
            value["destination_parent_inode"],
            "destination parent",
        )
        if source_parent_identity[0] != destination_parent_identity[0]:
            raise ValueError("exclusive move parents are on different volumes")

        source, status = api.open_relative(
            source_parent,
            value["source_name"],
            directory=value["directory"],
            desired_access=_DELETE | _FILE_READ_ATTRIBUTES,
        )
        if source is None:
            raise api.error_from_status(status, "exclusive move source open failed")
        source_identity = api.identity(source)
        _require_wsl_identity(source_identity, value["source_inode"], "source")
        if source_identity[0] != source_parent_identity[0]:
            raise ValueError("exclusive move source is on a different volume")

        existing, status = api.open_relative(
            destination_parent,
            value["destination_name"],
            directory=value["directory"],
        )
        if existing is not None:
            api.close(existing)
            raise FileExistsError(183, "exclusive move destination already exists")
        if status not in {_STATUS_OBJECT_NAME_NOT_FOUND, _STATUS_OBJECT_PATH_NOT_FOUND}:
            raise api.error_from_status(status, "exclusive move destination probe failed")

        api.rename_relative(source, destination_parent, value["destination_name"])

        old_source, status = api.open_relative(
            source_parent, value["source_name"], directory=value["directory"]
        )
        if old_source is not None:
            api.close(old_source)
            raise RuntimeError("exclusive move source still exists after rename")
        if status not in {_STATUS_OBJECT_NAME_NOT_FOUND, _STATUS_OBJECT_PATH_NOT_FOUND}:
            raise api.error_from_status(status, "exclusive move source verification failed")
        destination, status = api.open_relative(
            destination_parent,
            value["destination_name"],
            directory=value["directory"],
        )
        if destination is None:
            raise api.error_from_status(status, "exclusive move destination verification failed")
        if api.identity(destination) != source_identity:
            raise ValueError("exclusive move destination identity mismatch")
        return {
            "ok": True,
            "volume": source_identity[0],
            "file_id": source_identity[1],
        }
    finally:
        api.close(destination)
        api.close(source)
        api.close(destination_parent)
        api.close(source_parent)


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
        if len(raw) > _MAX_REQUEST_BYTES:
            raise ValueError("exclusive move request is too large")
        result = move(json.loads(raw.decode("utf-8")))
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
        return 0
    except FileExistsError:
        print('{"error":"destination_exists","ok":false}')
        return 17
    except ValueError:
        print('{"error":"validation_failed","ok":false}')
        return 18
    except Exception as exc:
        code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
        print(
            json.dumps(
                {"error": "native_move_failed", "ok": False, "code": code},
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 19


if __name__ == "__main__":
    raise SystemExit(main())
