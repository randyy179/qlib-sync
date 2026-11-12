"""Check that the upstream community release still looks the way qlib-sync expects.

Run weekly in CI so a format change or a stalled upstream is noticed before users hit it:
    python tools/check_upstream.py
Exits non-zero with a message describing what changed.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.request

from qlib_sync.cn.update import DEFAULT_SOURCE_URL, USER_AGENT, manifest_url, parse_manifest

STALE_AFTER_DAYS = 10


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def archive_size(url: str) -> int | None:
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        length = response.headers.get("Content-Length")
    return int(length) if length and length.isdigit() else None


def main() -> int:
    problems: list[str] = []
    try:
        payload = fetch_json(manifest_url(DEFAULT_SOURCE_URL))
    except Exception as exc:  # Any failure here is exactly what this check exists to report.
        print(f"FAIL manifest could not be read: {exc}")
        return 1
    try:
        manifest = parse_manifest(payload)
    except ValueError as exc:
        print(f"FAIL manifest format changed: {exc}\n{json.dumps(payload, indent=2)}")
        return 1
    print(f"manifest: trade_date={manifest.trade_date} size={manifest.size} sha256={manifest.sha256}")

    if manifest.sha256 is None:
        problems.append("manifest no longer carries archive_sha256; downloads would go unverified")
    if manifest.size is None:
        problems.append("manifest no longer carries archive_size_bytes")
    age = (dt.date.today() - dt.date.fromisoformat(manifest.trade_date)).days
    if age > STALE_AFTER_DAYS:
        problems.append(f"latest release is {age} days old; upstream may have stopped publishing")
    try:
        size = archive_size(DEFAULT_SOURCE_URL)
        if manifest.size is not None and size is not None and size != manifest.size:
            problems.append(f"archive is {size} bytes but the manifest says {manifest.size}")
    except Exception as exc:
        problems.append(f"archive could not be reached: {exc}")

    for problem in problems:
        print(f"FAIL {problem}")
    if not problems:
        print("OK upstream release matches what qlib-sync expects")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
