#!/usr/bin/env python3
"""Enter an inherited directory descriptor, then execute a fixed child command."""
from __future__ import annotations

import os
import stat
import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[1] != "--" or not args[0].isdigit():
        raise SystemExit("usage: descriptor_exec.py DIRECTORY_FD -- COMMAND [ARG ...]")
    descriptor = int(args[0])
    if descriptor < 0:
        raise SystemExit("directory descriptor must be nonnegative")
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise SystemExit("inherited descriptor is not a directory")
    command = args[2:]
    if not command or not command[0] or "\x00" in command[0]:
        raise SystemExit("child command is invalid")
    os.fchdir(descriptor)
    os.close(descriptor)
    os.execvpe(command[0], command, os.environ.copy())
    raise AssertionError("exec returned unexpectedly")


if __name__ == "__main__":
    raise SystemExit(main())
