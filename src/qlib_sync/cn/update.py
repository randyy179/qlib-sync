"""Safely replace Qlib CN daily data from the community release."""

from __future__ import annotations

import datetime as dt
import re
import shutil
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

DEFAULT_SOURCE_URL = "https://github.com/chenditc/investment_data/releases/latest/download/qlib_bin.tar.gz"
BACKUP_PREFIX = "cn_data_backup_before_latest_"
DOWNLOAD_NAME = "qlib_bin.tar.gz.part"
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CHUNK_SIZE = 1 << 20


class UpdateError(RuntimeError):
    """The update could not be completed; the active dataset is unchanged."""


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


def read_last_line(path: Path) -> str:
    lines = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    return lines[-1] if lines else ""


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
    request = urllib.request.Request(url, headers={"User-Agent": "qlib-sync"})
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


def download_package(options: UpdateOptions, target: Path) -> None:
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
            handle.extractall(extract_dir, filter="data")
    except (tarfile.TarError, EOFError) as exc:
        # A corrupt or mismatched download must not be resumed on the next run.
        archive.unlink(missing_ok=True)
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
    """Replace the active dataset with the latest package and return the resulting latest date.

    The package is validated in a staging directory first. If anything fails after the
    directories are switched, the previous dataset is moved back into place.
    """
    data_dir = options.data_dir
    base_dir = data_dir.parent
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    work_dir = base_dir / f".cn_data_update_{stamp}"
    backup_dir = base_dir / f"{BACKUP_PREFIX}{stamp}"
    lock_dir = base_dir / ".cn_data_update.lock"
    download_file = (options.download_cache_dir or base_dir / "downloads") / DOWNLOAD_NAME

    try:
        lock_dir.mkdir()
    except FileExistsError as exc:
        raise UpdateError(f"another CN data update is already running: {lock_dir}") from exc

    swapped = False
    try:
        if not (data_dir / "calendars" / "day.txt").is_file():
            raise UpdateError(f"existing CN data was not found at: {data_dir}")

        log(f"starting CN data update; active_dir={data_dir}")
        log("downloading latest community package")
        extract_dir = work_dir / "extract"
        extract_dir.mkdir(parents=True)
        download_file.parent.mkdir(parents=True, exist_ok=True)
        download_package(options, download_file)

        log("extracting and validating package")
        stage_dir = extract_package(download_file, extract_dir)
        old_latest = read_last_line(data_dir / "calendars" / "day.txt")
        new_latest = read_last_line(stage_dir / "calendars" / "day.txt")
        if not DATE_PATTERN.match(new_latest):
            raise UpdateError(f"downloaded calendar has an invalid latest date: {new_latest}")
        if new_latest < old_latest:
            raise UpdateError(f"refusing downgrade: package ends at {new_latest}, active data ends at {old_latest}")
        if new_latest == old_latest and not options.force:
            log(f"package ends at {new_latest}, same as active data; no replacement needed")
            download_file.unlink(missing_ok=True)
            return old_latest
        log(f"validated package; active_latest={old_latest} package_latest={new_latest}")

        apply_protected_instruments(options, stage_dir)

        log("switching data directories")
        data_dir.rename(backup_dir)
        swapped = True
        stage_dir.rename(data_dir)

        log("running Qlib read smoke test")
        smoke_test(data_dir)

        if options.keep_backup:
            remove_backups(base_dir, keep=backup_dir, label="superseded")
            log(f"update complete; latest_date={new_latest} rollback_backup={backup_dir}")
        else:
            log(f"removing validated previous data directory: {backup_dir}")
            shutil.rmtree(backup_dir)
            remove_backups(base_dir, keep=None, label="legacy")
            log(f"update complete; latest_date={new_latest} rollback_backup=none")
        swapped = False
        download_file.unlink(missing_ok=True)
        return new_latest
    finally:
        if swapped and backup_dir.is_dir():
            log("update did not complete; restoring the previous data directory")
            if data_dir.exists():
                shutil.rmtree(data_dir)
            backup_dir.rename(data_dir)
        shutil.rmtree(work_dir, ignore_errors=True)
        lock_dir.rmdir()
