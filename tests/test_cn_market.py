from types import SimpleNamespace


from qlib_sync.cn import market as collector
from qlib_sync.cn import update as cn_update


from release_fixture import write_dataset as _write_dataset


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


def test_mirror_prefixes_the_package_url_and_env_var_is_honored(monkeypatch):
    url = cn_update.DEFAULT_SOURCE_URL
    assert cn_update.mirrored(url, cn_update.resolve_mirror("https://ghfast.top/")) == f"https://ghfast.top/{url}"
    assert cn_update.resolve_mirror("") is None
    monkeypatch.setenv(cn_update.MIRROR_ENV, "https://gh-proxy.com")
    assert cn_update.resolve_mirror(None) == "https://gh-proxy.com"
    assert cn_update.resolve_mirror("") is None


def test_download_reports_progress_milestones_when_not_a_terminal(tmp_path, capsys):
    source = tmp_path / "blob.bin"
    source.write_bytes(b"x" * (cn_update.CHUNK_SIZE * 4))
    target = tmp_path / "out.part"

    cn_update._download_once(source.as_uri(), target, 5, expected_size=source.stat().st_size)

    err = capsys.readouterr().err
    assert target.read_bytes() == source.read_bytes()
    assert "(25%)" in err and "(75%)" in err and "download finished" in err


def test_tilde_shortens_paths_under_home_only(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cn_update.tilde(tmp_path / ".qlib" / "cn_data") == "~/.qlib/cn_data"
    assert cn_update.tilde(tmp_path) == "~"
    assert cn_update.tilde(f"{tmp_path}-other/x") == f"{tmp_path}-other/x"
    assert cn_update.tilde("/opt/data") == "/opt/data"
