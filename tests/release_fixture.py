"""Build community releases for tests, in the exact shape chenditc/investment_data publishes.

The layout and manifest follow the fixtures in upstream's tests/test_release_safety.py:
every member under `qlib_bin/`, and a manifest whose ten fields appear in the validator's
order. Tests mutate a valid release to check that qlib-sync refuses what upstream refuses.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

from qlib_sync._upstream import validate_archive as upstream

POOLS = ("all", "csi300", "csi500", "csi800", "csi1000", "csiall")
FUTURE_END = "2026-12-31"
DELETE = object()  # manifest override that removes the field


def calendar_files(days: list[str], future: list[str] | None = None) -> dict[str, str]:
    future = future if future is not None else [FUTURE_END]
    return {
        "calendars/day.txt": "".join(f"{day}\n" for day in days),
        "calendars/day_future.txt": "".join(f"{day}\n" for day in days + future),
    }


def pool_files(rows: list[tuple[str, str, str]]) -> dict[str, str]:
    body = "".join(f"{symbol}\t{start}\t{end}\n" for symbol, start, end in sorted(rows))
    return {f"instruments/{pool}.txt": body for pool in POOLS}


def release_files(days: list[str], rows: list[tuple[str, str, str]] | None = None) -> dict[str, str | bytes]:
    """A minimal valid dataset: calendars, every required pool, and one feature file."""
    rows = rows if rows is not None else [("SH600000", days[0], days[-1])]
    return {
        **calendar_files(days),
        **pool_files(rows),
        "features/sh600000/close.day.bin": b"test",
    }


def build_archive(path: Path, files: dict[str, str | bytes], prefix: str = "qlib_bin") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as tar:
        directories = sorted({str(Path(name).parent) for name in files} - {"."})
        added = set()
        for directory in directories:
            parts = Path(directory).parts
            for depth in range(1, len(parts) + 1):
                sub = "/".join(parts[:depth])
                if sub not in added:
                    info = tarfile.TarInfo(f"{prefix}/{sub}")
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    tar.addfile(info)
                    added.add(sub)
        for name, content in sorted(files.items()):
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))


def manifest_payload(archive: Path, tag: str, days: list[str], **overrides) -> dict:
    data = archive.read_bytes()
    payload = {
        "release_tag": tag,
        "target_trade_date": days[-1],
        "future_start_date": FUTURE_END,
        "future_end_date": FUTURE_END,
        "dolt_commit": "9vtplc2tar9ver7p6s1bus2oiedjvtqo",
        "investment_data_commit": "0" * 40,
        "qlib_commit": upstream.QLIB_COMMIT,
        "image_digest": "sha256:" + "a" * 64,
        "archive_size_bytes": len(data),
        "archive_sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
    }
    for key, value in overrides.items():
        if value is DELETE:
            payload.pop(key)
        else:
            payload[key] = value
    return payload


def publish(
    root: Path,
    days: list[str],
    *,
    tag: str | None = None,
    files: dict[str, str | bytes] | None = None,
    manifest: bool | str | bytes = True,
    tagged_path: bool = True,
    **manifest_overrides,
) -> str:
    """Write a release under `root` and return the archive URL qlib-sync should be pointed at.

    With `tagged_path` the URL contains /releases/download/<tag>/, as GitHub's does, so the
    tag is resolved from the URL. `manifest` may be False (none), raw text/bytes, or True.
    """
    tag = tag or days[-1]
    folder = root / "releases" / "download" / tag if tagged_path else root / "plain"
    archive = folder / "qlib_bin.tar.gz"
    build_archive(archive, files if files is not None else release_files(days))
    manifest_path = folder / upstream_manifest_name()
    if manifest is True:
        manifest_path.write_text(json.dumps(manifest_payload(archive, tag, days, **manifest_overrides)))
    elif manifest is not False:
        manifest_path.write_bytes(manifest.encode() if isinstance(manifest, str) else manifest)
    return archive.as_uri()


def upstream_manifest_name() -> str:
    return "qlib_bin.manifest.json"


def write_dataset(path: Path, latest: str, rows: list[tuple[str, str, str]] | None = None) -> None:
    """An installed dataset (no qlib_bin prefix), as qlib-sync leaves it on disk."""
    days = [latest]
    for name, content in release_files(days, rows).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)
