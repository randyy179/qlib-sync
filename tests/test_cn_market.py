import hashlib
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from qlib_sync.cn import market as collector
from qlib_sync.cn import update as cn_update


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
    if active is not None:
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


def _write_manifest(tmp_path: Path, trade_date: str, sha256: str | None = None, size: int | None = None) -> None:
    archive = tmp_path / "qlib_bin.tar.gz"
    payload = {
        "target_trade_date": trade_date,
        "archive_sha256": f"sha256:{sha256 or hashlib.sha256(archive.read_bytes()).hexdigest()}",
        "archive_size_bytes": archive.stat().st_size if size is None else size,
    }
    (tmp_path / cn_update.MANIFEST_NAME).write_text(json.dumps(payload))


def _no_smoke(_data_dir: Path) -> None:
    return None


def _leftovers(tmp_path: Path) -> list[str]:
    names = (".cn_data_update", "cn_data_backup_before_latest_")
    return sorted(path.name for path in tmp_path.iterdir() if path.name.startswith(names))


def test_update_replaces_data_and_removes_previous_directory(tmp_path):
    data_dir, options = _make_update(tmp_path)

    assert cn_update.run_update(options, smoke_test=_no_smoke).latest_date == "2026-07-03"

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

    assert cn_update.run_update(options, smoke_test=_no_smoke).latest_date == "2026-07-03"
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
    assert "previous data directory was restored" in capsys.readouterr().err
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
    assert "previous data directory was restored" in capsys.readouterr().err
    assert _leftovers(tmp_path) == []


def test_manifest_says_up_to_date_so_nothing_is_downloaded(tmp_path):
    data_dir, options = _make_update(tmp_path, active="2026-07-03", package="2026-07-03")
    _write_manifest(tmp_path, "2026-07-03")
    (tmp_path / "qlib_bin.tar.gz").unlink()

    assert cn_update.run_update(options, smoke_test=_no_smoke).latest_date == "2026-07-03"
    assert not (tmp_path / "downloads" / cn_update.DOWNLOAD_NAME).exists()


def test_manifest_checksum_is_verified_before_extracting(tmp_path):
    data_dir, options = _make_update(tmp_path)
    _write_manifest(tmp_path, "2026-07-03")

    assert cn_update.run_update(options, smoke_test=_no_smoke).latest_date == "2026-07-03"
    assert collector.read_latest_date(data_dir) == "2026-07-03"
    assert not list((tmp_path / "downloads").iterdir())


def test_checksum_mismatch_discards_download_and_keeps_data(tmp_path):
    data_dir, options = _make_update(tmp_path)
    _write_manifest(tmp_path, "2026-07-03", sha256="0" * 64)

    with pytest.raises(cn_update.UpdateError, match="sha256 does not match"):
        cn_update.run_update(options, smoke_test=_no_smoke)

    assert collector.read_latest_date(data_dir) == "2026-07-02"
    assert not list((tmp_path / "downloads").iterdir())
    assert _leftovers(tmp_path) == []


def test_package_date_must_match_manifest(tmp_path):
    data_dir, options = _make_update(tmp_path)
    _write_manifest(tmp_path, "2026-07-04")

    with pytest.raises(cn_update.UpdateError, match="manifest says 2026-07-04"):
        cn_update.run_update(options, smoke_test=_no_smoke)

    assert collector.read_latest_date(data_dir) == "2026-07-02"


def test_stale_partial_download_from_another_package_is_discarded(tmp_path):
    _, options = _make_update(tmp_path)
    _write_manifest(tmp_path, "2026-07-03")
    cache = tmp_path / "downloads"
    cache.mkdir()
    (cache / cn_update.DOWNLOAD_NAME).write_bytes(b"partial bytes from an older release")
    (cache / f"{cn_update.DOWNLOAD_NAME}.sha256").write_text("f" * 64)

    assert cn_update.run_update(options, smoke_test=_no_smoke).latest_date == "2026-07-03"


def test_malformed_manifest_falls_back_to_validating_the_package(tmp_path, capsys):
    data_dir, options = _make_update(tmp_path)
    (tmp_path / cn_update.MANIFEST_NAME).write_text(json.dumps({"release_tag": "2026-07-03"}))

    assert cn_update.run_update(options, smoke_test=_no_smoke).latest_date == "2026-07-03"
    assert "release manifest unavailable" in capsys.readouterr().err


def test_first_install_into_an_empty_location(tmp_path):
    data_dir, options = _make_update(tmp_path, active=None)
    _write_manifest(tmp_path, "2026-07-03")

    assert cn_update.run_update(options, smoke_test=_no_smoke).latest_date == "2026-07-03"
    assert collector.read_latest_date(data_dir) == "2026-07-03"
    assert _leftovers(tmp_path) == []


def test_first_install_creates_missing_parent_directories(tmp_path):
    _, options = _make_update(tmp_path, active=None)
    options.data_dir = tmp_path / "fresh" / "qlib_data" / "cn_data"

    assert cn_update.run_update(options, smoke_test=_no_smoke).latest_date == "2026-07-03"
    assert collector.read_latest_date(options.data_dir) == "2026-07-03"


def test_refuses_to_overwrite_a_non_dataset_directory(tmp_path):
    data_dir, options = _make_update(tmp_path, active=None)
    data_dir.mkdir()
    (data_dir / "notes.txt").write_text("not qlib data")

    with pytest.raises(cn_update.UpdateError, match="not a Qlib daily dataset"):
        cn_update.run_update(options, smoke_test=_no_smoke)

    assert (data_dir / "notes.txt").exists()


def test_status_reports_update_available_from_manifest(tmp_path):
    data_dir, options = _make_update(tmp_path)
    _write_manifest(tmp_path, "2026-07-03")

    result = collector.collect_status(data_dir, tmp_path / "protected", False, 5, source_url=options.source_url)

    assert result["remote_release"]["trade_date"] == "2026-07-03"
    assert result["update_available"] is True


def _write_pool(data_dir: Path, rows: list[tuple[str, str, str]]) -> None:
    (data_dir / "instruments" / "all.txt").write_text("".join(f"{s}\t{a}\t{b}\n" for s, a, b in rows))


def test_update_result_describes_new_days_and_symbol_changes(tmp_path):
    data_dir, options = _make_update(tmp_path, package="2026-07-06")
    (data_dir / "calendars" / "day.txt").write_text("2026-07-01\n2026-07-02\n")
    _write_pool(data_dir, [("SH600000", "2020-01-01", "2026-07-02"), ("SZ000002", "2020-01-01", "2026-07-02")])
    staged = tmp_path / "release" / "cn_data"
    (staged / "calendars" / "day.txt").write_text("2026-07-01\n2026-07-02\n2026-07-03\n2026-07-06\n")
    _write_pool(
        staged,
        [
            ("SH600000", "2020-01-01", "2026-07-06"),
            ("SZ000002", "2020-01-01", "2026-07-03"),
            ("SH688999", "2026-07-03", "2026-07-06"),
        ],
    )
    with tarfile.open(tmp_path / "qlib_bin.tar.gz", "w:gz") as tar:
        tar.add(staged, arcname="release/cn_data")

    result = cn_update.run_update(options, smoke_test=_no_smoke)

    assert result.status == "updated"
    assert result.previous_date == "2026-07-02"
    assert result.new_trading_days == ["2026-07-03", "2026-07-06"]
    assert result.added_symbols == ["SH688999"]
    assert result.delisted_symbols == ["SZ000002"]
    assert result.summary_lines()[1] == "New trading days: 2 (2026-07-03 to 2026-07-06)"


def test_up_to_date_and_install_results(tmp_path):
    data_dir, options = _make_update(tmp_path, active=None)
    installed = cn_update.run_update(options, smoke_test=_no_smoke)
    assert installed.status == "installed"

    again = cn_update.run_update(options, smoke_test=_no_smoke)
    assert again.status == "up_to_date"
    assert again.summary_lines() == ["Already up to date: data ends at 2026-07-03."]


def test_mirror_prefixes_the_package_url_and_env_var_is_honored(monkeypatch):
    url = cn_update.DEFAULT_SOURCE_URL
    assert cn_update.mirrored(url, cn_update.resolve_mirror("https://ghfast.top/")) == f"https://ghfast.top/{url}"
    assert cn_update.resolve_mirror("") is None
    monkeypatch.setenv(cn_update.MIRROR_ENV, "https://gh-proxy.com")
    assert cn_update.resolve_mirror(None) == "https://gh-proxy.com"
    assert cn_update.resolve_mirror("") is None


def test_manifest_falls_back_to_mirror_when_direct_fails(tmp_path, monkeypatch, capsys):
    calls = []

    def fake_read(url, timeout):
        calls.append(url)
        if not url.startswith("https://mirror.example"):
            raise OSError("connection reset")
        return cn_update.Manifest(trade_date="2026-07-03")

    monkeypatch.setattr(cn_update, "_read_manifest", fake_read)

    manifest = cn_update.fetch_manifest(cn_update.DEFAULT_SOURCE_URL, mirror="https://mirror.example")

    assert manifest.trade_date == "2026-07-03"
    assert calls[0].startswith("https://github.com/") and calls[1].startswith("https://mirror.example/")
    assert "read through the mirror" in capsys.readouterr().err


def test_download_reports_progress_milestones_when_not_a_terminal(tmp_path, capsys):
    source = tmp_path / "blob.bin"
    source.write_bytes(b"x" * (cn_update.CHUNK_SIZE * 4))
    target = tmp_path / "out.part"

    cn_update._download_once(source.as_uri(), target, 5, expected_size=source.stat().st_size)

    err = capsys.readouterr().err
    assert target.read_bytes() == source.read_bytes()
    assert "(25%)" in err and "(75%)" in err and "download finished" in err


def test_install_records_the_release_checksum(tmp_path):
    data_dir, options = _make_update(tmp_path, active=None)
    _write_manifest(tmp_path, "2026-07-03")

    cn_update.run_update(options, smoke_test=_no_smoke)

    record = cn_update.read_install_record(data_dir)
    assert record["trade_date"] == "2026-07-03"
    assert record["archive_sha256"] == hashlib.sha256((tmp_path / "qlib_bin.tar.gz").read_bytes()).hexdigest()


def test_republished_release_for_the_same_date_replaces_local_data(tmp_path):
    data_dir, options = _make_update(tmp_path, active=None)
    _write_manifest(tmp_path, "2026-07-03")
    cn_update.run_update(options, smoke_test=_no_smoke)

    # Upstream fixes the data and republishes the same trade date.
    staged = tmp_path / "release" / "cn_data"
    (staged / "features" / "sh600000" / "close.day.bin").write_bytes(b"fixed")
    with tarfile.open(tmp_path / "qlib_bin.tar.gz", "w:gz") as tar:
        tar.add(staged, arcname="release/cn_data")
    _write_manifest(tmp_path, "2026-07-03")

    result = cn_update.run_update(options, smoke_test=_no_smoke)

    assert result.status == "republished"
    assert (data_dir / "features" / "sh600000" / "close.day.bin").read_bytes() == b"fixed"
    assert cn_update.run_update(options, smoke_test=_no_smoke).status == "up_to_date"


def test_same_date_without_an_install_record_is_treated_as_current(tmp_path):
    # Data installed by other means has no record, so a checksum cannot be compared.
    data_dir, options = _make_update(tmp_path, active="2026-07-03", package="2026-07-03")
    _write_manifest(tmp_path, "2026-07-03")

    assert cn_update.run_update(options, smoke_test=_no_smoke).status == "up_to_date"
