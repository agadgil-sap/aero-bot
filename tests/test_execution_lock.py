"""Cross-process execution lock tests."""

from pathlib import Path

import pytest

from aero_bot.execution_lock import ExecutionLockUnavailableError, exclusive_execution_lock


def test_execution_lock_refuses_a_second_owner(tmp_path: Path) -> None:
    """A concurrent live process fails closed instead of overlapping broadcasts."""
    path = tmp_path / "execution.lock"
    with (
        exclusive_execution_lock(path),
        pytest.raises(ExecutionLockUnavailableError, match="already owns"),
        exclusive_execution_lock(path),
    ):
        raise AssertionError("the second owner must never enter")


def test_execution_lock_releases_for_the_next_process(tmp_path: Path) -> None:
    """A completed owner releases the lock for the next live command."""
    path = tmp_path / "execution.lock"
    with exclusive_execution_lock(path):
        pass
    with exclusive_execution_lock(path):
        pass
