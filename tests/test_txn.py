from pathlib import Path

import pytest

from qlib_sync.txn import LockedError, exclusive_lock, replace_directory


def _dir(path: Path, content: str) -> Path:
    path.mkdir(parents=True)
    (path / "marker").write_text(content)
    return path


def _content(path: Path) -> str:
    return (path / "marker").read_text()


def test_replace_swaps_in_staged_and_returns_backup(tmp_path):
    active = _dir(tmp_path / "active", "old")
    staged = _dir(tmp_path / "staged", "new")

    backup = replace_directory(active, staged, tmp_path / "backup")

    assert _content(active) == "new"
    assert backup == tmp_path / "backup" and _content(backup) == "old"
    assert not staged.exists()


def test_replace_without_previous_directory_returns_none(tmp_path):
    staged = _dir(tmp_path / "staged", "new")

    assert replace_directory(tmp_path / "active", staged, tmp_path / "backup") is None
    assert _content(tmp_path / "active") == "new"
    assert not (tmp_path / "backup").exists()


def test_failed_verification_restores_previous_directory(tmp_path):
    active = _dir(tmp_path / "active", "old")
    staged = _dir(tmp_path / "staged", "new")

    def verify(path: Path) -> None:
        assert _content(path) == "new"
        raise RuntimeError("bad data")

    with pytest.raises(RuntimeError, match="bad data"):
        replace_directory(active, staged, tmp_path / "backup", verify=verify)

    assert _content(active) == "old"
    assert not (tmp_path / "backup").exists()


def test_failed_verification_on_first_install_leaves_nothing(tmp_path):
    staged = _dir(tmp_path / "staged", "new")

    with pytest.raises(RuntimeError):
        replace_directory(tmp_path / "active", staged, tmp_path / "backup", verify=_raise)

    assert not (tmp_path / "active").exists()


def test_failed_stage_move_restores_previous_directory(tmp_path):
    active = _dir(tmp_path / "active", "old")

    with pytest.raises(FileNotFoundError):
        replace_directory(active, tmp_path / "missing", tmp_path / "backup")

    assert _content(active) == "old"


def test_existing_backup_path_is_refused_before_touching_anything(tmp_path):
    active = _dir(tmp_path / "active", "old")
    staged = _dir(tmp_path / "staged", "new")
    _dir(tmp_path / "backup", "unrelated")

    with pytest.raises(FileExistsError):
        replace_directory(active, staged, tmp_path / "backup")

    assert _content(active) == "old" and _content(staged) == "new"


def test_exclusive_lock_blocks_a_second_holder_and_releases(tmp_path):
    lock = tmp_path / ".lock"
    with exclusive_lock(lock):
        with pytest.raises(LockedError):
            with exclusive_lock(lock):
                pass
    assert not lock.exists()


def _raise(_path: Path) -> None:
    raise RuntimeError("bad data")
