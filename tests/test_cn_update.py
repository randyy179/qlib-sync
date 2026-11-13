"""Update behavior: transactional replacement, and strict verification with the upstream validator."""

import json
import urllib.parse
from pathlib import Path

import pytest

from qlib_sync.cn import market
from qlib_sync.cn import update as cn_update
from release_fixture import DELETE, POOLS, build_archive, calendar_files, publish, release_files, write_dataset

OLD, NEW = "2026-07-02", "2026-07-03"


def _no_smoke(_data_dir: Path) -> None:
    return None


def _options(tmp_path: Path, url: str, **overrides) -> cn_update.UpdateOptions:
    return cn_update.UpdateOptions(
        data_dir=tmp_path / "qlib_data" / "cn_data",
        protected_dir=tmp_path / "protected",
        source_url=url,
        retries=1,
        retry_delay=0,
        **overrides,
    )


def _setup(tmp_path: Path, active: str | None = OLD, days=(OLD, NEW), **publish_kwargs):
    url = publish(tmp_path / "upstream", list(days), **publish_kwargs)
    options = _options(tmp_path, url)
    if active:
        write_dataset(options.data_dir, active)
    return options


def _run(options, **kwargs):
    return cn_update.run_update(options, smoke_test=_no_smoke, **kwargs)


def _latest(options) -> str | None:
    return market.read_latest_date(options.data_dir)


def _leftovers(options) -> list[str]:
    base = options.data_dir.parent
    names = (".cn_data_update", "cn_data_backup_before_latest_", ".cn_data_update.lock")
    return sorted(path.name for path in base.iterdir() if path.name.startswith(names))


def _download_cache(options) -> list[str]:
    folder = options.data_dir.parent / "downloads"
    return sorted(path.name for path in folder.iterdir()) if folder.is_dir() else []


def _archive_path(url: str) -> Path:
    return Path(urllib.parse.unquote(urllib.parse.urlparse(url).path))


# ---------------------------------------------------------------- transactional behavior


def test_update_replaces_data_verifies_it_and_cleans_up(tmp_path):
    options = _setup(tmp_path)

    result = _run(options)

    assert result.status == "updated" and result.latest_date == NEW
    assert result.verified is True
    assert "validate_archive.py --require-publishable passed for release 2026-07-03" in result.verification
    assert result.summary_lines()[-1].startswith("Verified:")
    assert _latest(options) == NEW
    assert _leftovers(options) == []
    assert _download_cache(options) == []


def test_update_keeps_one_backup_when_requested(tmp_path):
    options = _setup(tmp_path)
    options.keep_backup = True
    stale = options.data_dir.parent / "cn_data_backup_before_latest_2020-01-01_00-00-00"
    stale.mkdir()

    _run(options)

    backups = market.list_backups(options.data_dir)
    assert len(backups) == 1 and backups[0] != stale
    assert market.read_latest_date(backups[0]) == OLD


def test_current_data_costs_no_download(tmp_path):
    options = _setup(tmp_path, active=NEW)
    _archive_path(options.source_url).unlink()  # any download attempt would now fail

    result = _run(options)

    assert result.status == "up_to_date" and result.verified is True
    assert _download_cache(options) == []


def test_force_reinstalls_the_same_date(tmp_path):
    options = _setup(tmp_path, active=NEW)
    marker = options.data_dir / "marker"
    marker.write_text("active")

    options.force = True
    _run(options)

    assert not marker.exists() and _latest(options) == NEW


def test_older_release_is_never_installed(tmp_path):
    options = _setup(tmp_path, active="2026-07-06")

    assert _run(options).status == "up_to_date"
    assert _latest(options) == "2026-07-06"

    options.force = True
    with pytest.raises(cn_update.UpdateError, match="refusing downgrade"):
        _run(options)
    assert _latest(options) == "2026-07-06"


def test_protected_instruments_are_restored(tmp_path):
    options = _setup(tmp_path)
    options.protected = ["my_universe.txt"]
    (options.data_dir / "instruments" / "my_universe.txt").write_text(f"SH600000\t2020-01-01\t{OLD}\n")

    _run(options)

    assert (options.data_dir / "instruments" / "my_universe.txt").read_text().startswith("SH600000")
    assert (options.protected_dir / "my_universe.txt").is_file()


def test_update_refuses_to_run_while_locked(tmp_path):
    options = _setup(tmp_path)
    cn_update.lock_dir_for(options.data_dir).mkdir()

    with pytest.raises(cn_update.UpdateError, match="already running"):
        _run(options)


def test_failed_smoke_test_restores_previous_data(tmp_path, capsys):
    options = _setup(tmp_path)

    def failing(_path: Path) -> None:
        raise RuntimeError("Qlib calendar is empty")

    with pytest.raises(RuntimeError, match="calendar is empty"):
        cn_update.run_update(options, smoke_test=failing)

    assert _latest(options) == OLD
    assert "previous data directory was restored" in capsys.readouterr().err
    assert _leftovers(options) == []


def test_failed_stage_move_restores_previous_data(tmp_path, monkeypatch, capsys):
    options = _setup(tmp_path)
    real_rename = Path.rename

    def failing_rename(self, target):
        if Path(target) == options.data_dir and "extract" in self.parts:
            raise OSError("simulated stage move failure")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", failing_rename)

    with pytest.raises(OSError, match="simulated"):
        _run(options)

    assert _latest(options) == OLD
    assert "previous data directory was restored" in capsys.readouterr().err
    assert _leftovers(options) == []


def test_first_install_into_a_missing_folder(tmp_path):
    options = _setup(tmp_path, active=None)
    options.data_dir = tmp_path / "fresh" / "qlib_data" / "cn_data"

    result = _run(options)

    assert result.status == "installed" and result.verified is True
    assert _latest(options) == NEW


def test_refuses_to_overwrite_a_folder_that_is_not_a_dataset(tmp_path):
    options = _setup(tmp_path, active=None)
    options.data_dir.mkdir(parents=True)
    (options.data_dir / "notes.txt").write_text("not qlib data")

    with pytest.raises(cn_update.UpdateError, match="not a Qlib daily dataset"):
        _run(options)

    assert (options.data_dir / "notes.txt").exists()


def test_result_lists_new_days_and_symbol_changes(tmp_path):
    days = [OLD, NEW, "2026-07-06"]
    rows = [("SH600000", OLD, "2026-07-06"), ("SZ000002", OLD, NEW), ("SH688999", NEW, "2026-07-06")]
    url = publish(tmp_path / "upstream", days, files=release_files(days, rows))
    options = _options(tmp_path, url)
    write_dataset(options.data_dir, OLD, [("SH600000", OLD, OLD), ("SZ000002", OLD, OLD)])

    result = _run(options)

    assert result.new_trading_days == [NEW, "2026-07-06"]
    assert result.added_symbols == ["SH688999"]
    assert result.delisted_symbols == ["SZ000002"]
    assert result.summary_lines()[1] == "New trading days: 2 (2026-07-03 to 2026-07-06)"


def test_install_record_and_republished_release(tmp_path):
    options = _setup(tmp_path, active=None)
    _run(options)
    record = cn_update.read_install_record(options.data_dir)
    assert record["release_tag"] == NEW and record["verified"] is True and len(record["archive_sha256"]) == 64

    # Upstream fixes the data and republishes the same tag with a new checksum.
    files = release_files([OLD, NEW])
    files["features/sh600000/close.day.bin"] = b"fixed"
    publish(tmp_path / "upstream", [OLD, NEW], files=files)

    result = _run(options)

    assert result.status == "republished"
    assert (options.data_dir / "features" / "sh600000" / "close.day.bin").read_bytes() == b"fixed"
    assert _run(options).status == "up_to_date"


def test_same_date_without_an_install_record_is_treated_as_current(tmp_path):
    options = _setup(tmp_path, active=NEW)

    assert _run(options).status == "up_to_date"


def test_partial_download_from_another_package_is_discarded(tmp_path):
    options = _setup(tmp_path)
    cache = options.data_dir.parent / "downloads"
    cache.mkdir(parents=True)
    (cache / cn_update.DOWNLOAD_NAME).write_bytes(b"bytes from an older release")
    (cache / f"{cn_update.DOWNLOAD_NAME}.sha256").write_text("f" * 64)

    assert _run(options).latest_date == NEW


# ---------------------------------------------------------------- strict verification


def test_missing_manifest_is_refused_by_default(tmp_path):
    options = _setup(tmp_path, manifest=False)

    with pytest.raises(cn_update.UpdateError, match="refusing to install unverified data.*--allow-unverified"):
        _run(options)

    assert _latest(options) == OLD
    assert _download_cache(options) == []
    assert _leftovers(options) == []


def test_malformed_manifest_is_refused(tmp_path):
    options = _setup(tmp_path, manifest="{not json")

    with pytest.raises(cn_update.UpdateError, match=r"upstream validator \(invalid-manifest\)"):
        _run(options)

    assert _latest(options) == OLD


@pytest.mark.parametrize(
    "image_digest",
    [None, "", "garbage", "SHA256:" + "a" * 64, "sha256:" + "A" * 64, "sha256:" + "a" * 63, DELETE],
    ids=["null", "empty", "garbage", "upper-prefix", "upper-hex", "short", "missing"],
)
def test_release_must_carry_a_valid_image_digest(tmp_path, image_digest):
    # The maintainer's report: "" and "garbage" used to count as publishable.
    options = _setup(tmp_path, image_digest=image_digest)

    with pytest.raises(cn_update.UpdateError, match="invalid-manifest"):
        _run(options)

    assert _latest(options) == OLD


def test_manifest_fields_must_appear_in_the_upstream_order(tmp_path):
    url = publish(tmp_path / "upstream", [OLD, NEW])
    manifest_path = _archive_path(url).with_name(cn_update.MANIFEST_NAME)
    payload = json.loads(manifest_path.read_text())
    manifest_path.write_text(json.dumps(dict(reversed(list(payload.items())))))
    options = _options(tmp_path, url)
    write_dataset(options.data_dir, OLD)

    with pytest.raises(cn_update.UpdateError, match="invalid-manifest"):
        _run(options)


def test_manifest_tag_must_match_the_release_it_came_from(tmp_path):
    options = _setup(tmp_path, release_tag="2026-07-06")

    with pytest.raises(cn_update.UpdateError, match="invalid-manifest"):
        _run(options)


def test_checksum_mismatch_discards_the_download_and_keeps_data(tmp_path):
    options = _setup(tmp_path, archive_sha256="sha256:" + "0" * 64)

    with pytest.raises(cn_update.UpdateError, match="archive-identity-mismatch"):
        _run(options)

    assert _latest(options) == OLD
    assert _download_cache(options) == []


@pytest.mark.parametrize(
    ("mutate", "token"),
    [
        (lambda f: f.pop("instruments/csi300.txt"), "missing-required-member"),
        (lambda f: f.update({"instruments/csi500.txt": f"SH600000\t{OLD}\t{OLD}\n"}), "target-mismatch"),
        (lambda f: f.update({"calendars/day.txt": f"{OLD}\n2026-7-03\n"}), "malformed-required-member"),
        (lambda f: f.update({"instruments/all.txt": f"SZ000001\t{OLD}\t{NEW}\nSH600000\t{OLD}\t{NEW}\n"}),
         "malformed-required-member"),
        (lambda f: f.update({"instruments/csiall.txt": f"SH600000 {OLD} {NEW}\n"}), "malformed-required-member"),
        (lambda f: f.update(calendar_files([OLD, NEW], future=["2026-07-07", "2027-01-29"])), "future-mismatch"),
    ],
    ids=["missing-pool", "stale-pool", "non-canonical-date", "unsorted-pool", "bad-row", "future-mismatch"],
)
def test_package_contents_are_checked_by_the_upstream_validator(tmp_path, mutate, token):
    files = release_files([OLD, NEW])
    mutate(files)
    options = _setup(tmp_path, files=files)

    with pytest.raises(cn_update.UpdateError, match=token):
        _run(options)

    assert _latest(options) == OLD


def test_members_outside_qlib_bin_are_rejected_before_extraction(tmp_path):
    url = publish(tmp_path / "upstream", [OLD, NEW])
    archive = _archive_path(url)
    build_archive(archive, release_files([OLD, NEW]), prefix="release/cn_data")
    manifest_path = archive.with_name(cn_update.MANIFEST_NAME)
    from release_fixture import manifest_payload  # rebuilt for the new archive bytes

    manifest_path.write_text(json.dumps(manifest_payload(archive, NEW, [OLD, NEW])))
    options = _options(tmp_path, url)
    write_dataset(options.data_dir, OLD)

    with pytest.raises(cn_update.UpdateError, match="unsafe-archive-member"):
        _run(options)


def test_allow_unverified_installs_without_a_manifest_and_says_so(tmp_path, capsys):
    options = _setup(tmp_path, manifest=False)
    options.allow_unverified = True

    result = _run(options)

    assert result.status == "updated" and result.verified is False
    assert "--allow-unverified" in result.verification
    assert result.summary_lines()[-1].startswith("WARNING: installed WITHOUT verification")
    assert cn_update.read_install_record(options.data_dir)["verified"] is False
    assert "continuing WITHOUT verification" in capsys.readouterr().err


def test_allow_unverified_never_bypasses_a_checksum_mismatch(tmp_path):
    # A valid manifest that disagrees with the package means tampering or corruption.
    options = _setup(tmp_path, archive_sha256="sha256:" + "0" * 64)
    options.allow_unverified = True

    with pytest.raises(cn_update.UpdateError, match="archive-identity-mismatch"):
        _run(options)

    assert _latest(options) == OLD


def test_tag_comes_from_the_manifest_when_the_url_names_none(tmp_path, capsys):
    options = _setup(tmp_path, tagged_path=False)

    result = _run(options)

    assert result.verified is True
    assert "tag was taken from the manifest itself" in capsys.readouterr().err


# ---------------------------------------------------------------- mirror and status


def test_manifest_falls_back_to_the_mirror_when_github_is_unreachable(tmp_path, monkeypatch, capsys):
    url = publish(tmp_path / "upstream", [OLD, NEW])
    manifest_bytes = _archive_path(url).with_name(cn_update.MANIFEST_NAME).read_bytes()
    calls = []

    def fake_read(target, timeout):
        calls.append(target)
        if not target.startswith("https://mirror.example/"):
            raise OSError("connection reset")
        return manifest_bytes

    monkeypatch.setattr(cn_update, "_read_bytes", fake_read)

    release = cn_update.fetch_release(url, 5, "https://mirror.example", tmp_path / "scratch")

    assert release.trade_date == NEW and release.tag == NEW
    assert calls[1].startswith("https://mirror.example/")
    assert "read through the mirror" in capsys.readouterr().err


def test_latest_url_is_pinned_to_the_tag_github_redirects_to(monkeypatch):
    seen = {}

    class FakeOpener:
        def open(self, request, timeout):
            seen["method"], seen["url"] = request.get_method(), request.full_url
            headers = {"Location": "https://github.com/o/r/releases/download/2026-11-12/qlib_bin.manifest.json"}
            raise cn_update.urllib.error.HTTPError(request.full_url, 302, "Found", headers, None)

    monkeypatch.setattr(cn_update.urllib.request, "build_opener", lambda *_: FakeOpener())

    tag = cn_update.resolve_release_tag(cn_update.DEFAULT_SOURCE_URL, 5)

    assert tag == "2026-11-12" and seen["method"] == "HEAD"
    assert cn_update.tagged_url(cn_update.DEFAULT_SOURCE_URL, tag).endswith(
        "/releases/download/2026-11-12/qlib_bin.tar.gz"
    )


def test_status_reports_the_verified_release(tmp_path):
    options = _setup(tmp_path)

    status = market.collect_status(options.data_dir, options.protected_dir, False, 5, source_url=options.source_url)

    assert status["remote_release"]["tag"] == NEW and status["update_available"] is True
    assert status["remote_error"] is None


def test_status_explains_an_unverifiable_release(tmp_path):
    options = _setup(tmp_path, image_digest="")

    status = market.collect_status(options.data_dir, options.protected_dir, False, 5, source_url=options.source_url)

    assert status["remote_release"] is None and "invalid-manifest" in status["remote_error"]


def test_every_required_pool_is_in_the_fixture():
    # Guards the fixture: upstream requires exactly these pool files.
    from qlib_sync._upstream import validate_archive as upstream

    assert {f"qlib_bin/instruments/{pool}.txt" for pool in POOLS} <= set(upstream.REQUIRED_MEMBERS)


# ---------------------------------------------------------------- command line


def _cli(monkeypatch, argv):
    monkeypatch.setattr(cn_update, "qlib_calendar_smoke_test", _no_smoke)
    monkeypatch.setattr(cn_update.run_update, "__defaults__", (_no_smoke,))
    market.run(market.get_parser().parse_args(argv))


def test_cli_update_prints_json_with_the_verification(tmp_path, monkeypatch, capsys):
    options = _setup(tmp_path)

    _cli(monkeypatch, ["update", "--json", "--data-dir", str(options.data_dir), "--source-url", options.source_url])

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "updated" and payload["verified"] is True
    assert "validate_archive.py" in payload["verification"]


def test_cli_update_prints_the_summary(tmp_path, monkeypatch, capsys):
    options = _setup(tmp_path)

    _cli(monkeypatch, ["update", "--data-dir", str(options.data_dir), "--source-url", options.source_url])

    out = capsys.readouterr().out
    assert "Updated CN data from 2026-07-02 to 2026-07-03." in out and "Verified:" in out


def test_cli_refuses_unverifiable_releases_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    options = _setup(tmp_path, manifest=False)

    with pytest.raises(SystemExit) as exit_info:
        _cli(monkeypatch, ["update", "--data-dir", str(options.data_dir), "--source-url", options.source_url])

    assert exit_info.value.code == 1
    assert "--allow-unverified" in capsys.readouterr().err
    assert _latest(options) == OLD


def test_cli_allow_unverified_flag_reaches_the_update(tmp_path, monkeypatch, capsys):
    options = _setup(tmp_path, manifest=False)

    _cli(monkeypatch, ["update", "--allow-unverified", "--data-dir", str(options.data_dir),
                       "--source-url", options.source_url])

    assert "WARNING: installed WITHOUT verification" in capsys.readouterr().out


# ---------------------------------------------------------------- review follow-ups


def test_unverified_install_is_replaced_once_a_verified_release_exists(tmp_path):
    options = _setup(tmp_path, manifest=False)
    options.allow_unverified = True
    _run(options)
    assert cn_update.read_install_record(options.data_dir)["verified"] is False

    # The same release becomes verifiable (the manifest appears).
    publish(tmp_path / "upstream", [OLD, NEW])
    options.allow_unverified = False

    result = _run(options)

    assert result.status == "republished" and result.verified is True
    assert cn_update.read_install_record(options.data_dir)["verified"] is True
    assert _run(options).status == "up_to_date"


def test_rejected_archive_does_not_stay_in_the_download_cache(tmp_path):
    files = release_files([OLD, NEW])
    files["instruments/csi500.txt"] = f"SH600000\t{OLD}\t{OLD}\n"
    options = _setup(tmp_path, files=files)

    with pytest.raises(cn_update.UpdateError, match="target-mismatch"):
        _run(options)

    assert _download_cache(options) == []


def test_mirror_fallback_skips_the_direct_fetch_when_github_was_unreachable(tmp_path, monkeypatch):
    url = publish(tmp_path / "upstream", [OLD, NEW], tagged_path=False)
    manifest_bytes = _archive_path(url).with_name(cn_update.MANIFEST_NAME).read_bytes()
    latest = "https://github.com/o/r/releases/latest/download/qlib_bin.tar.gz"
    calls = []

    def unreachable(*_args, **_kwargs):
        raise OSError("network unreachable")

    def fake_read(target, timeout):
        calls.append(target)
        if target.startswith("https://mirror.example/"):
            return manifest_bytes
        raise AssertionError(f"direct fetch attempted after GitHub was unreachable: {target}")

    monkeypatch.setattr(cn_update, "resolve_release_tag", unreachable)
    monkeypatch.setattr(cn_update, "_read_bytes", fake_read)

    release = cn_update.fetch_release(latest, 5, "https://mirror.example", tmp_path / "scratch")

    assert release.tag == NEW and release.tag_source == "manifest"
    assert len(calls) == 1 and calls[0].startswith("https://mirror.example/")
    assert release.archive_url.endswith(f"/releases/download/{NEW}/qlib_bin.tar.gz")


@pytest.mark.parametrize("code", [200, 404])
def test_latest_url_that_does_not_redirect_is_an_error(monkeypatch, code):
    class FakeOpener:
        def open(self, request, timeout):
            if code == 200:
                class Response:
                    def close(self):
                        pass
                return Response()
            raise cn_update.urllib.error.HTTPError(request.full_url, code, "Not Found", {}, None)

    monkeypatch.setattr(cn_update.urllib.request, "build_opener", lambda *_: FakeOpener())

    with pytest.raises((OSError, cn_update.urllib.error.HTTPError)):
        cn_update.resolve_release_tag(cn_update.DEFAULT_SOURCE_URL, 5)


def test_status_output_names_the_verified_release(tmp_path, capsys):
    options = _setup(tmp_path)

    market.print_status(
        market.collect_status(options.data_dir, options.protected_dir, False, 5, source_url=options.source_url)
    )

    out = capsys.readouterr().out
    assert f"Community release {NEW}: trade date {NEW}" in out and "(update available)" in out


def test_status_output_explains_an_unverifiable_release(tmp_path, capsys):
    options = _setup(tmp_path, image_digest="garbage")

    market.print_status(
        market.collect_status(options.data_dir, options.protected_dir, False, 5, source_url=options.source_url)
    )

    out = capsys.readouterr().out
    assert "Community release: cannot be verified" in out and "invalid-manifest" in out
