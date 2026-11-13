"""qlib-sync installs a release exactly when upstream's validator accepts it.

This is the evidence behind the README's claim. For every case, the vendored
validate_archive.py runs directly (as the investment_data README documents, with
--require-publishable), and qlib-sync runs a full update against the same files. The two
verdicts must agree. Cases follow the categories in upstream's tests/test_release_safety.py.
"""

import hashlib
import io
import json
import tarfile
import urllib.parse
from pathlib import Path

import pytest

from qlib_sync._upstream import SOURCE_BLOB_SHA, validate_archive as upstream
from qlib_sync.cn import update as cn_update
from release_fixture import DELETE, build_archive, calendar_files, manifest_payload, publish, release_files, write_dataset

OLD, NEW = "2026-07-02", "2026-07-03"
DAYS = [OLD, NEW]

MANIFEST_CASES = {
    "valid": {},
    "image-null": {"image_digest": None},
    "image-empty": {"image_digest": ""},
    "image-garbage": {"image_digest": "garbage"},
    "image-prefix-case": {"image_digest": "SHA256:" + "a" * 64},
    "image-short": {"image_digest": "sha256:" + "a" * 63},
    "image-missing": {"image_digest": DELETE},
    "dolt-uppercase": {"dolt_commit": "Z" * 32},
    "dolt-short": {"dolt_commit": "a" * 31},
    "investment-uppercase": {"investment_data_commit": "A" * 40},
    "investment-short": {"investment_data_commit": "a" * 39},
    "qlib-commit-other": {"qlib_commit": "f" * 40},
    "size-zero": {"archive_size_bytes": 0},
    "size-bool": {"archive_size_bytes": True},
    "size-string": {"archive_size_bytes": "123"},
    "sha-uppercase": {"archive_sha256": "SHA256:" + "a" * 64},
    "sha-wrong": {"archive_sha256": "sha256:" + "0" * 64},
    "target-non-canonical": {"target_trade_date": "2026-7-03"},
    "target-mismatch": {"target_trade_date": OLD},
    "future-start-mismatch": {"future_start_date": "2026-12-30"},
    "tag-mismatch": {"release_tag": "2026-07-06"},
    "tag-missing": {"release_tag": DELETE},
}

FILE_CASES = {
    "missing-pool": lambda f: f.pop("instruments/csi800.txt"),
    "missing-future-calendar": lambda f: f.pop("calendars/day_future.txt"),
    "stale-pool": lambda f: f.update({"instruments/csi1000.txt": f"SH600000\t{OLD}\t{OLD}\n"}),
    "unsorted-pool": lambda f: f.update({"instruments/all.txt": f"SZ000001\t{OLD}\t{NEW}\nSH600000\t{OLD}\t{NEW}\n"}),
    "duplicate-pool-row": lambda f: f.update({"instruments/all.txt": f"SH600000\t{OLD}\t{NEW}\n" * 2}),
    "start-after-end": lambda f: f.update({"instruments/csi300.txt": f"SH600000\t{NEW}\t{OLD}\n"}),
    "two-column-row": lambda f: f.update({"instruments/csiall.txt": f"SH600000\t{NEW}\n"}),
    "calendar-unsorted": lambda f: f.update(calendar_files([NEW, OLD])),
    "calendar-crlf": lambda f: f.update({"calendars/day.txt": f"{OLD}\r\n{NEW}\r\n"}),
    "calendar-no-final-newline": lambda f: f.update({"calendars/day.txt": f"{OLD}\n{NEW}"}),
    "future-not-prefix": lambda f: f.update({"calendars/day_future.txt": f"{OLD}\n2026-07-06\n2026-12-31\n"}),
    "future-not-longer": lambda f: f.update({"calendars/day_future.txt": f"{OLD}\n{NEW}\n"}),
}


def _no_smoke(_path: Path) -> None:
    return None


def _upstream_accepts(url: str, tag: str) -> bool:
    archive = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path))
    try:
        upstream.validate(archive, archive.with_name(cn_update.MANIFEST_NAME), tag, require_publishable=True)
    except upstream.ValidationFailure:
        return False
    return True


def _qlib_sync_installs(tmp_path: Path, url: str) -> bool:
    """Whether qlib-sync put the release in place. A refusal and a no-op both count as not installing."""
    options = cn_update.UpdateOptions(
        data_dir=tmp_path / "qlib_data" / "cn_data", protected_dir=tmp_path / "protected", source_url=url, retries=1
    )
    write_dataset(options.data_dir, OLD)
    before = (options.data_dir / "calendars" / "day.txt").read_text()
    try:
        result = cn_update.run_update(options, smoke_test=_no_smoke)
    except cn_update.UpdateError:
        result = None
    installed = result is not None and result.status in ("installed", "updated", "republished")
    if not installed:
        # "Not installed" must mean the active data is exactly as it was.
        assert (options.data_dir / "calendars" / "day.txt").read_text() == before, "data changed without installing"
    return installed


@pytest.mark.parametrize("overrides", MANIFEST_CASES.values(), ids=MANIFEST_CASES.keys())
def test_manifest_cases_match_upstream(tmp_path, overrides):
    url = publish(tmp_path / "upstream", DAYS, **overrides)

    expected = _upstream_accepts(url, NEW)

    assert _qlib_sync_installs(tmp_path, url) is expected
    assert expected is (not overrides), "every mutation in this table is one upstream rejects"


@pytest.mark.parametrize("mutate", FILE_CASES.values(), ids=FILE_CASES.keys())
def test_package_cases_match_upstream(tmp_path, mutate):
    files = release_files(DAYS)
    mutate(files)
    url = publish(tmp_path / "upstream", DAYS, files=files)

    assert _upstream_accepts(url, NEW) is False
    assert _qlib_sync_installs(tmp_path, url) is False


@pytest.mark.parametrize(
    "member",
    ["release/cn_data/calendars/day.txt", "qlib_bin/../escape", "qlib_bin//double", "./qlib_bin/x", "qlib_bin/symlink"],
    ids=["outside-prefix", "dot-dot", "double-slash", "leading-dot", "symlink"],
)
def test_unsafe_archive_members_match_upstream(tmp_path, member):
    url = publish(tmp_path / "upstream", DAYS)
    archive = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path))
    build_archive(archive, release_files(DAYS))
    with tarfile.open(archive, "r:gz") as source:
        members = [(info, source.extractfile(info).read() if info.isfile() else None) for info in source.getmembers()]
    with tarfile.open(archive, "w:gz") as target:
        for info, data in members:
            target.addfile(info, io.BytesIO(data) if data is not None else None)
        extra = tarfile.TarInfo(member)
        if member.endswith("symlink"):
            extra.type, extra.linkname = tarfile.SYMTYPE, "qlib_bin/calendars/day.txt"
            target.addfile(extra)
        else:
            extra.size = 1
            target.addfile(extra, io.BytesIO(b"x"))
    archive.with_name(cn_update.MANIFEST_NAME).write_text(json.dumps(manifest_payload(archive, NEW, DAYS)))

    assert _upstream_accepts(url, NEW) is False
    assert _qlib_sync_installs(tmp_path, url) is False


def test_vendored_validator_is_byte_identical_to_the_recorded_upstream_blob():
    # A git blob hash is sha1("blob <size>\\0" + content); it equals GitHub's file sha.
    content = Path(upstream.__file__).read_bytes()
    blob = hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()
    assert blob == SOURCE_BLOB_SHA
