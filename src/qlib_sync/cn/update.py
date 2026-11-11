"""Install or update Qlib CN daily data from the community release."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from ..txn import LockedError, exclusive_lock, replace_directory

DEFAULT_SOURCE_URL = "https://github.com/chenditc/investment_data/releases/latest/download/qlib_bin.tar.gz"
MANIFEST_NAME = "qlib_bin.manifest.json"
BACKUP_PREFIX = "cn_data_backup_before_latest_"
DOWNLOAD_NAME = "qlib_bin.tar.gz.part"
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CHUNK_SIZE = 1 << 20
USER_AGENT = "qlib-sync"


class UpdateError(RuntimeError):
    """The update could not be completed; the active dataset is unchanged."""


@dataclass
class Manifest:
    """What the community release says about its package, read before downloading it."""

    trade_date: str
    sha256: str | None = None
    size: int | None = None


@dataclass
class UpdateOptions:
    data_dir: Path
    protected_dir: Path
    source_url: str = DEFAULT_SOURCE_URL
    protected: list[str] = field(default_factory=list)
    refresh_protected: bool = False
    keep_backup: bool = False
    force: bool = False
    download_cache_dir: Path | None = None
    retries: int = 3
    retry_delay: float = 3.0
    timeout: int = 60


def log(message: str) -> None:
    print(f"[INFO] {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def warn(message: str) -> None:
    print(f"[WARN] {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def read_last_line(path: Path) -> str:
    lines = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def lock_dir_for(data_dir: Path) -> Path:
    return data_dir.parent / ".cn_data_update.lock"


def manifest_url(source_url: str) -> str:
    return f"{source_url.rsplit('/', 1)[0]}/{MANIFEST_NAME}"


def parse_manifest(payload: dict) -> Manifest:
    trade_date = str(payload.get("target_trade_date") or "")
    if not DATE_PATTERN.match(trade_date):
        raise ValueError(f"target_trade_date is missing or invalid: {trade_date!r}")
    digest = payload.get("archive_sha256")
    if digest is not None:
        digest = str(digest).removeprefix("sha256:").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"archive_sha256 is not a sha256 digest: {payload.get('archive_sha256')!r}")
    size = payload.get("archive_size_bytes")
    return Manifest(trade_date=trade_date, sha256=digest, size=int(size) if size is not None else None)


def fetch_manifest(source_url: str, timeout: int = 15) -> Manifest | None:
    """Return the release manifest, or None (with a warning) when it cannot be used.

    The manifest is an optimization and an integrity check, not a requirement: when it
    is missing or its format changes, the update falls back to validating the package
    itself after download.
    """
    url = manifest_url(source_url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return parse_manifest(json.load(response))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
        warn(f"release manifest unavailable ({exc}); the package will be validated after download")
        return None


def qlib_calendar_smoke_test(data_dir: Path) -> None:
    # Keep Qlib imports lazy so the module stays importable without initializing Qlib.
    # pylint: disable=import-outside-toplevel
    import qlib
    from qlib.config import REG_CN
    from qlib.data import D

    qlib.init(provider_uri=str(data_dir), region=REG_CN, logging_level="ERROR")
    if len(D.calendar(start_time="2020-01-01", end_time=None, freq="day")) == 0:
        raise RuntimeError("Qlib calendar is empty")


def _download_once(url: str, target: Path, timeout: int) -> None:
    offset = target.stat().st_size if target.exists() else 0
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if offset and url.startswith(("http://", "https://")):
        request.add_header("Range", f"bytes={offset}-")
    else:
        offset = 0
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and offset:
            # The cached part no longer matches the remote package; start over.
            target.unlink()
            return _download_once(url, target, timeout)
        raise
    with response:
        resumed = offset and getattr(response, "status", None) == 206
        with target.open("ab" if resumed else "wb") as handle:
            shutil.copyfileobj(response, handle, CHUNK_SIZE)


def prepare_download_cache(target: Path, manifest: Manifest | None) -> None:
    """Discard a cached partial download that belongs to a different package."""
    marker = target.with_name(f"{target.name}.sha256")
    expected = manifest.sha256 if manifest else None
    if target.exists() and expected and (not marker.is_file() or marker.read_text().strip() != expected):
        log("discarding a cached partial download from a different package")
        target.unlink()
    if expected:
        marker.write_text(expected)
    else:
        marker.unlink(missing_ok=True)


def clear_download_cache(target: Path) -> None:
    target.unlink(missing_ok=True)
    target.with_name(f"{target.name}.sha256").unlink(missing_ok=True)


def download_package(options: UpdateOptions, target: Path, manifest: Manifest | None) -> None:
    if manifest and manifest.size and target.exists() and target.stat().st_size == manifest.size:
        log("cached package is complete; skipping download")
        return
    if target.exists():
        log(f"resuming incomplete package download: {target}")
    last_error: Exception | None = None
    for attempt in range(1, options.retries + 1):
        try:
            _download_once(options.source_url, target, options.timeout)
            return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            log(f"download attempt {attempt}/{options.retries} failed: {exc}")
            if attempt < options.retries:
                time.sleep(options.retry_delay)
    raise UpdateError(f"could not download the community package: {last_error}")


def verify_package(target: Path, manifest: Manifest | None) -> None:
    if manifest is None:
        return
    if manifest.size is not None and target.stat().st_size != manifest.size:
        actual = target.stat().st_size
        clear_download_cache(target)
        raise UpdateError(f"package size {actual} does not match the manifest ({manifest.size})")
    if manifest.sha256 is not None:
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
                digest.update(chunk)
        if digest.hexdigest() != manifest.sha256:
            clear_download_cache(target)
            raise UpdateError("package sha256 does not match the release manifest; the download was discarded")
        log("package sha256 matches the release manifest")


def extract_package(archive: Path, extract_dir: Path) -> Path:
    """Extract the archive after rejecting unsafe members; return the staged Qlib data directory."""
    try:
        with tarfile.open(archive, "r:gz") as handle:
            for member in handle.getmembers():
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise UpdateError(f"unsafe archive member path: {member.name}")
                if member.issym() or member.islnk():
                    raise UpdateError(f"archive links are not permitted: {member.name}")
            # The member checks above already enforce this; the filter adds a second
            # layer on Python versions that provide it.
            extra = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            handle.extractall(extract_dir, **extra)
    except (tarfile.TarError, EOFError) as exc:
        # A corrupt or mismatched download must not be resumed on the next run.
        clear_download_cache(archive)
        raise UpdateError(f"downloaded archive is not a valid package: {exc}") from exc
    calendar_file = next((path for path in sorted(extract_dir.rglob("calendars/day.txt")) if path.is_file()), None)
    if calendar_file is None:
        raise UpdateError("downloaded archive does not contain Qlib daily calendar data")
    stage_dir = calendar_file.parent.parent
    if not (stage_dir / "features").is_dir() or not (stage_dir / "instruments").is_dir():
        raise UpdateError("downloaded archive does not contain a complete Qlib data directory")
    return stage_dir


def apply_protected_instruments(options: UpdateOptions, stage_dir: Path) -> None:
    for instrument in options.protected:
        if "/" in instrument or instrument.startswith("."):
            raise UpdateError(f"protected instrument must be a plain filename: {instrument}")
        protected_file = options.protected_dir / instrument
        active_file = options.data_dir / "instruments" / instrument
        if active_file.is_file() and (not protected_file.is_file() or options.refresh_protected):
            options.protected_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(active_file, protected_file)
            log(f"saved protected instrument copy: {protected_file}")
        if protected_file.is_file():
            shutil.copy2(protected_file, stage_dir / "instruments" / instrument)
            log(f"restoring protected instrument: {instrument}")
        else:
            log(f"protected instrument not found; skipping restoration: {instrument}")


def remove_backups(base_dir: Path, keep: Path | None, label: str) -> None:
    for backup in sorted(base_dir.glob(f"{BACKUP_PREFIX}*")):
        if backup.is_dir() and backup != keep:
            log(f"removing {label} rollback backup: {backup}")
            shutil.rmtree(backup)


def run_update(options: UpdateOptions, smoke_test: Callable[[Path], None] = qlib_calendar_smoke_test) -> str:
    """Install or update the dataset and return the resulting latest trading date.

    The release manifest is read first so an up-to-date dataset costs one small request.
    The package is verified and validated in a staging directory, then swapped in with
    `replace_directory`, which restores the previous dataset if the smoke test fails.
    """
    options.data_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        with exclusive_lock(lock_dir_for(options.data_dir)):
            return _run_update_locked(options, smoke_test)
    except LockedError as exc:
        raise UpdateError(str(exc)) from exc


def _run_update_locked(options: UpdateOptions, smoke_test: Callable[[Path], None]) -> str:
    data_dir = options.data_dir
    base_dir = data_dir.parent
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    work_dir = base_dir / f".cn_data_update_{stamp}"
    backup_dir = base_dir / f"{BACKUP_PREFIX}{stamp}"
    download_file = (options.download_cache_dir or base_dir / "downloads") / DOWNLOAD_NAME

    try:
        calendar = data_dir / "calendars" / "day.txt"
        if calendar.is_file():
            old_latest = read_last_line(calendar)
            log(f"starting CN data update; active_dir={data_dir} active_latest={old_latest}")
        elif data_dir.exists() and any(data_dir.iterdir()):
            raise UpdateError(f"{data_dir} exists but is not a Qlib daily dataset; refusing to replace it")
        else:
            old_latest = None
            log(f"no existing CN data at {data_dir}; installing from the community release")

        manifest = fetch_manifest(options.source_url, options.timeout)
        if manifest:
            log(f"release manifest: trade_date={manifest.trade_date}")
            if old_latest and manifest.trade_date <= old_latest and not options.force:
                log(f"already up to date; local data ends at {old_latest}, release ends at {manifest.trade_date}")
                return old_latest

        log("downloading community package")
        download_file.parent.mkdir(parents=True, exist_ok=True)
        prepare_download_cache(download_file, manifest)
        download_package(options, download_file, manifest)
        verify_package(download_file, manifest)

        log("extracting and validating package")
        extract_dir = work_dir / "extract"
        extract_dir.mkdir(parents=True)
        stage_dir = extract_package(download_file, extract_dir)
        new_latest = read_last_line(stage_dir / "calendars" / "day.txt")
        if not DATE_PATTERN.match(new_latest):
            raise UpdateError(f"downloaded calendar has an invalid latest date: {new_latest}")
        if manifest and new_latest != manifest.trade_date:
            raise UpdateError(f"package calendar ends at {new_latest}, but the manifest says {manifest.trade_date}")
        if old_latest and new_latest < old_latest:
            raise UpdateError(f"refusing downgrade: package ends at {new_latest}, active data ends at {old_latest}")
        if old_latest and new_latest == old_latest and not options.force:
            log(f"package ends at {new_latest}, same as active data; no replacement needed")
            clear_download_cache(download_file)
            return old_latest
        log(f"validated package; active_latest={old_latest or 'none'} package_latest={new_latest}")

        apply_protected_instruments(options, stage_dir)

        log("switching data directories and running a Qlib read smoke test")
        try:
            previous = replace_directory(data_dir, stage_dir, backup_dir, verify=smoke_test)
        except BaseException:
            log("update did not complete; the previous data directory was restored")
            raise

        if previous is None:
            log(f"install complete; latest_date={new_latest}")
        elif options.keep_backup:
            remove_backups(base_dir, keep=previous, label="superseded")
            log(f"update complete; latest_date={new_latest} rollback_backup={previous}")
        else:
            log(f"removing validated previous data directory: {previous}")
            shutil.rmtree(previous)
            remove_backups(base_dir, keep=None, label="legacy")
            log(f"update complete; latest_date={new_latest} rollback_backup=none")
        clear_download_cache(download_file)
        return new_latest
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
