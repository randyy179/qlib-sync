import functools

import qlib_sync
from qlib_sync import api
from qlib_sync.cn import update as cn_update
from release_fixture import publish


def test_public_api_installs_and_reports(tmp_path, monkeypatch):
    url = publish(tmp_path / "upstream", ["2026-07-02", "2026-07-03"])
    # The staged files are placeholders, so skip the real Qlib read.
    monkeypatch.setattr(api, "run_update", functools.partial(cn_update.run_update, smoke_test=lambda _path: None))
    data_dir = tmp_path / "qlib_data" / "cn_data"

    result = qlib_sync.update_cn(data_dir, source_url=url)

    assert isinstance(result, qlib_sync.UpdateResult) and result.status == "installed"
    assert result.verified is True
    status = qlib_sync.status_cn(data_dir, offline=True)
    assert status["local_latest_date"] == "2026-07-03"
    health = qlib_sync.health_check_cn(data_dir, skip_qlib=True)
    assert health == {"ok": True, "errors": [], "checks": health["checks"]}
