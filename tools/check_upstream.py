"""Check that the upstream community release still matches what qlib-sync verifies against.

Run weekly in CI so a change upstream is noticed before users hit it:
    python tools/check_upstream.py
Exits non-zero with a message describing what changed:
- the latest release's manifest fails the vendored upstream validator (--require-publishable);
- the archive's size disagrees with the manifest;
- upstream has not published for STALE_AFTER_DAYS;
- upstream changed qlib/validate_archive.py, so the vendored copy needs re-syncing.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

from qlib_sync._upstream import SOURCE_BLOB_SHA, SOURCE_PATH, SOURCE_REPOSITORY
from qlib_sync.cn.update import DEFAULT_SOURCE_URL, USER_AGENT, ManifestError, fetch_release

STALE_AFTER_DAYS = 10


def _request(url: str, method: str = "GET") -> urllib.request.Request:
    headers = {"User-Agent": USER_AGENT}
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, method=method, headers=headers)


def upstream_validator_blob() -> str:
    url = f"https://api.github.com/repos/{SOURCE_REPOSITORY}/contents/{SOURCE_PATH}"
    with urllib.request.urlopen(_request(url), timeout=30) as response:
        return json.load(response)["sha"]


def archive_size(url: str) -> int | None:
    with urllib.request.urlopen(_request(url, "HEAD"), timeout=30) as response:
        length = response.headers.get("Content-Length")
    return int(length) if length and length.isdigit() else None


def main() -> int:
    problems: list[str] = []

    with tempfile.TemporaryDirectory() as scratch:
        try:
            release = fetch_release(DEFAULT_SOURCE_URL, 30, None, Path(scratch))
        except ManifestError as exc:
            problems.append(f"latest release cannot be verified: {exc}")
            release = None
    if release:
        print(f"release {release.tag}: trade_date={release.trade_date} size={release.size} sha256={release.sha256}")
        print("OK manifest passes the vendored upstream validator (--require-publishable)")
        age = (dt.date.today() - dt.date.fromisoformat(release.trade_date)).days
        if age > STALE_AFTER_DAYS:
            problems.append(f"latest release is {age} days old; upstream may have stopped publishing")
        try:
            size = archive_size(release.archive_url)
            if size is not None and size != release.size:
                problems.append(f"archive is {size} bytes but the manifest says {release.size}")
        except Exception as exc:  # Any failure here is exactly what this check exists to report.
            problems.append(f"archive could not be reached: {exc}")

    try:
        blob = upstream_validator_blob()
        if blob != SOURCE_BLOB_SHA:
            problems.append(
                f"upstream changed {SOURCE_PATH} (blob {blob}, vendored {SOURCE_BLOB_SHA}); "
                "re-sync src/qlib_sync/_upstream/validate_archive.py and its recorded commit"
            )
        else:
            print("OK vendored validate_archive.py matches upstream")
    except Exception as exc:
        problems.append(f"could not compare the vendored validator with upstream: {exc}")

    for problem in problems:
        print(f"FAIL {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
