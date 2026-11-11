import functools
import tarfile
from pathlib import Path

import qlib_sync
from qlib_sync import api
from qlib_sync.cn import update as cn_update


def _dataset(path: Path, latest: str) -> None:
    (path / "calendars").mkdir(parents=True)
    (path / "features" / "sh600000").mkdir(parents=True)
    (path / "instruments").mkdir(parents=True)
    (path / "calendars" / "day.txt").write_text(f"{latest}\n")
    (path / "features" / "sh600000" / "close.day.bin").write_bytes(b"test")
    (path / "instruments" / "all.txt").write_text(f"SH600000\t2020-01-01\t{latest}\n")


def test_public_api_installs_and_reports(tmp_path, monkeypatch):
    staged = tmp_path / "release" / "cn_data"
    _dataset(staged, "2026-07-03")
    archive = tmp_path / "qlib_bin.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(staged, arcname="release/cn_data")
    # The staged files are placeholders, so skip the real Qlib read.
    monkeypatch.setattr(api, "run_update", functools.partial(cn_update.run_update, smoke_test=lambda _path: None))
    data_dir = tmp_path / "qlib_data" / "cn_data"

    result = qlib_sync.update_cn(data_dir, source_url=archive.as_uri())

    assert isinstance(result, qlib_sync.UpdateResult) and result.status == "installed"
    status = qlib_sync.status_cn(data_dir, offline=True)
    assert status["local_latest_date"] == "2026-07-03"
    health = qlib_sync.health_check_cn(data_dir, skip_qlib=True)
    assert health == {"ok": True, "errors": [], "checks": health["checks"]}
