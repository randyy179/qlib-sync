"""Transactional replacement of a dataset directory.

A dataset is replaced with two renames: the active directory moves aside to a
backup path, then the staged directory moves into place. A verification step
then runs against the new data. If anything fails after the first rename, the
previous directory is put back, so the active path ends up holding either the
complete old dataset or the complete, verified new one.

Renames are atomic only within one filesystem, so callers stage new data next
to the active directory rather than in a system temp directory.
"""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator


class LockedError(RuntimeError):
    """Another operation already holds the lock."""


@contextmanager
def exclusive_lock(lock_dir: Path) -> Iterator[None]:
    """Hold a lock for the duration of the block.

    `mkdir` either creates the directory or fails, atomically, on every platform,
    which makes a directory a dependable cross-process lock without extra packages.
    """
    try:
        lock_dir.mkdir()
    except FileExistsError as exc:
        raise LockedError(f"another operation is already running (lock: {lock_dir})") from exc
    try:
        yield
    finally:
        lock_dir.rmdir()


def replace_directory(
    active: Path,
    staged: Path,
    backup: Path,
    verify: Callable[[Path], None] | None = None,
) -> Path | None:
    """Swap `staged` into the `active` path and verify it.

    Returns the backup path holding the previous directory, or None when there was
    no previous directory. The caller decides whether to keep or delete the backup.
    On any failure the previous state is restored and the exception propagates.
    """
    if backup.exists():
        raise FileExistsError(f"backup path already exists: {backup}")
    had_previous = active.exists()
    if had_previous:
        active.rename(backup)
    try:
        staged.rename(active)
        if verify is not None:
            verify(active)
    except BaseException:
        # Whatever now sits at the active path is the new data that failed; the
        # staged copy is disposable, so remove it before moving the old data back.
        if active.exists():
            shutil.rmtree(active)
        if had_previous:
            backup.rename(active)
        raise
    return backup if had_previous else None
