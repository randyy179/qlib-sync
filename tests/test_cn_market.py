import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from qdata.cn import market as collector
from qdata.cn import update as cn_update


def _write_dataset(path: Path, latest: str) -> None:
    (path / "calendars").mkdir(parents=True)
    (path / "features" / "sh600000").mkdir(parents=True)
    (path / "instruments").mkdir(parents=True)
    (path / "calendars" / "day.txt").write_text(f"{latest}\n")
    (path / "features" / "sh600000" / "close.day.bin").write_bytes(b"test")
    (path / "instruments" / "all.txt").write_text(f"SH600000\t2020-01-01\t{latest}\n")


def test_read_latest_date_and_instrument_stats(tmp_path):
    data_dir = tmp_path / "cn_data"
    (data_dir / "calendars").mkdir(parents=True)
    (data_dir / "instruments").mkdir()
    (data_dir / "calendars" / "day.txt").write_text("2026-07-02\n2026-07-03\n")
    pool = data_dir / "instruments" / "custom.txt"
    pool.write_text(
        "SH600000\t2020-01-01\t2026-07-03\n" "SH600000\t2010-01-01\t2019-12-31\n" "SZ000001\t2020-01-01\t2026-07-03\n"
    )

    assert collector.read_latest_date(data_dir) == "2026-07-03"
    assert collector.instrument_stats(pool) == {"name": "custom", "records": 3, "unique_symbols": 2}


def test_list_backups_returns_newest_name_first(tmp_path):
    data_dir = tmp_path / "cn_data"
    older = tmp_path / "cn_data_backup_before_latest_2026-07-01_10-00-00"
    newer = tmp_path / "cn_data_backup_before_latest_2026-07-02_10-00-00"
    older.mkdir()
    newer.mkdir()

    assert collector.list_backups(data_dir) == [newer, older]


def test_check_protected_detects_match_and_difference(tmp_path):
    data_dir = tmp_path / "cn_data"
    active_dir = data_dir / "instruments"
    protected_dir = tmp_path / "protected"
    active_dir.mkdir(parents=True)
    protected_dir.mkdir()
    (active_dir / "csi100.txt").write_text("SH600000\t2020-01-01\t2026-07-03\n")
    (protected_dir / "csi100.txt").write_text("SH600000\t2020-01-01\t2026-07-03\n")

    errors, notes = collector.check_protected(data_dir, protected_dir)
    assert errors == []
    assert notes == ["protected_pool_ok=csi100.txt"]

    (active_dir / "csi100.txt").write_text("SZ000001\t2020-01-01\t2026-07-03\n")
    errors, _ = collector.check_protected(data_dir, protected_dir)
    assert errors == ["protected pool differs from active data: csi100.txt"]


def test_check_protected_allows_no_local_pools(tmp_path):
    errors, notes = collector.check_protected(tmp_path / "cn_data", tmp_path / "protected")

    assert errors == []
    assert notes == []


def test_resolve_backup_rejects_unmanaged_path(tmp_path):
    data_dir = tmp_path / "cn_data"
    unmanaged = tmp_path / "arbitrary_backup"
    unmanaged.mkdir()

    try:
        collector.resolve_backup(data_dir, unmanaged)
    except ValueError as exc:
        assert "managed CN data backup" in str(exc)
    else:
        raise AssertionError("expected unmanaged backup to be rejected")


def test_restore_works_without_an_active_dataset(tmp_path):
    data_dir = tmp_path / "cn_data"
    backup = tmp_path / "cn_data_backup_before_latest_2026-07-03_10-00-00"
    _write_dataset(backup, "2026-07-03")

    collector.run_restore(SimpleNamespace(data_dir=data_dir, backup=backup, list=False, yes=True))

    assert collector.read_latest_date(data_dir) == "2026-07-03"
    assert not backup.exists()


def _make_update(tmp_path, active="2026-07-02", package="2026-07-03", **overrides):
    data_dir = tmp_path / "cn_data"
    staged = tmp_path / "release" / "cn_data"
    _write_dataset(data_dir, active)
    _write_dataset(staged, package)
    archive = tmp_path / "qlib_bin.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(staged, arcname="release/cn_data")
    options = cn_update.UpdateOptions(
        data_dir=data_dir,
        protected_dir=tmp_path / "protected",
        source_url=archive.as_uri(),
        retries=1,
        **overrides,
    )
    return data_dir, options


def _no_smoke(_data_dir: Path) -> None:
    return None


def _leftovers(tmp_path: Path) -> list[str]:
    names = (".cn_data_update", "cn_data_backup_before_latest_")
    return sorted(path.name for path in tmp_path.iterdir() if path.name.startswith(names))


def test_update_replaces_data_and_removes_previous_directory(tmp_path):
    data_dir, options = _make_update(tmp_path)

    assert cn_update.run_update(options, smoke_test=_no_smoke) == "2026-07-03"

    assert collector.read_latest_date(data_dir) == "2026-07-03"
    assert _leftovers(tmp_path) == []
    assert not (tmp_path / "downloads" / cn_update.DOWNLOAD_NAME).exists()


def test_update_keeps_one_backup_when_requested(tmp_path):
    data_dir, options = _make_update(tmp_path, keep_backup=True)
    stale = tmp_path / "cn_data_backup_before_latest_2020-01-01_00-00-00"
    stale.mkdir()

    cn_update.run_update(options, smoke_test=_no_smoke)

    backups = collector.list_backups(data_dir)
    assert len(backups) == 1 and backups[0] != stale
    assert collector.read_latest_date(backups[0]) == "2026-07-02"


def test_update_is_a_noop_when_dates_match_unless_forced(tmp_path):
    data_dir, options = _make_update(tmp_path, active="2026-07-03", package="2026-07-03")
    marker = data_dir / "marker"
    marker.write_text("active")

    assert cn_update.run_update(options, smoke_test=_no_smoke) == "2026-07-03"
    assert marker.exists()

    options.force = True
    cn_update.run_update(options, smoke_test=_no_smoke)
    assert not marker.exists()


def test_update_refuses_downgrade(tmp_path):
    data_dir, options = _make_update(tmp_path, active="2026-07-03", package="2026-07-02")

    with pytest.raises(cn_update.UpdateError, match="refusing downgrade"):
        cn_update.run_update(options, smoke_test=_no_smoke)

    assert collector.read_latest_date(data_dir) == "2026-07-03"
    assert _leftovers(tmp_path) == []


def test_update_restores_protected_instruments(tmp_path):
    data_dir, options = _make_update(tmp_path, protected=["my_universe.txt"])
    (data_dir / "instruments" / "my_universe.txt").write_text("SH600000\t2020-01-01\t2026-07-02\n")

    cn_update.run_update(options, smoke_test=_no_smoke)

    assert (data_dir / "instruments" / "my_universe.txt").read_text().startswith("SH600000")
    assert (tmp_path / "protected" / "my_universe.txt").is_file()


def test_update_rejects_unsafe_archive_members(tmp_path):
    data_dir, options = _make_update(tmp_path)
    archive = tmp_path / "qlib_bin.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(tmp_path / "release" / "cn_data", arcname="../escape/cn_data")

    with pytest.raises(cn_update.UpdateError, match="unsafe archive member"):
        cn_update.run_update(options, smoke_test=_no_smoke)

    assert collector.read_latest_date(data_dir) == "2026-07-02"
    assert not (tmp_path.parent / "escape").exists()


def test_update_refuses_to_run_while_locked(tmp_path):
    _, options = _make_update(tmp_path)
    (tmp_path / ".cn_data_update.lock").mkdir()

    with pytest.raises(cn_update.UpdateError, match="already running"):
        cn_update.run_update(options, smoke_test=_no_smoke)


def test_update_rolls_back_when_smoke_test_fails(tmp_path, capsys):
    data_dir, options = _make_update(tmp_path)

    def failing_smoke(_data_dir: Path) -> None:
        raise RuntimeError("Qlib calendar is empty")

    with pytest.raises(RuntimeError, match="calendar is empty"):
        cn_update.run_update(options, smoke_test=failing_smoke)

    assert collector.read_latest_date(data_dir) == "2026-07-02"
    assert "restoring the previous data directory" in capsys.readouterr().out
    assert _leftovers(tmp_path) == []


def test_update_rolls_back_when_stage_move_fails(tmp_path, monkeypatch, capsys):
    data_dir, options = _make_update(tmp_path)
    real_rename = Path.rename

    def failing_rename(self, target):
        if Path(target) == data_dir and "extract" in self.parts:
            raise OSError("simulated stage move failure")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", failing_rename)

    with pytest.raises(OSError, match="simulated stage move failure"):
        cn_update.run_update(options, smoke_test=_no_smoke)

    assert collector.read_latest_date(data_dir) == "2026-07-02"
    assert "restoring the previous data directory" in capsys.readouterr().out
    assert _leftovers(tmp_path) == []
