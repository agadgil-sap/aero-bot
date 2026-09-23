"""Cross-process lock guarding every live execution surface."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class ExecutionLockUnavailableError(RuntimeError):
    """Raised when another process already owns the live execution lock."""


@contextmanager
def exclusive_execution_lock(path: Path) -> Iterator[None]:
    """Hold one nonblocking advisory lock for the complete live action window."""
    resolved = path.expanduser()
    try:
        fd = os.open(resolved, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        raise ExecutionLockUnavailableError(
            f"cannot open execution lock {resolved}: {error}"
        ) from error
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ExecutionLockUnavailableError(
                f"another live process already owns execution lock {resolved}"
            ) from error
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
