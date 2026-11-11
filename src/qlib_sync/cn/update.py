"""Install or update Qlib CN daily data from the community release."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
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
MIRROR_ENV = "QLIB_SYNC_MIRROR"


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
    mirror: str | None = None
    protected: list[str] = field(default_factory=list)
    refresh_protected: bool = False
    keep_backup: bool = False
    force: bool = False
    download_cache_dir: Path | None = None
    retries: int = 3
    retry_delay: float = 3.0
    timeout: int = 60


@dataclass
class UpdateResult:
    """What an update did, for people (`summary_lines`) and for programs (`asdict`)."""

    status: str  # "installed", "updated", or "up_to_date"
    latest_date: str
    previous_date: str | None = None
    new_trading_days: list[str] = field(default_factory=list)
    added_symbols: list[str] = field(default_factory=list)
    delisted_symbols: list[str] = field(default_factory=list)
    backup: str | None = None

    def summary_lines(self) -> list[str]:
        if self.status == "up_to_date":
            return [f"Already up to date: data ends at {self.latest_date}."]
        if self.status == "installed":
            return [f"Installed CN data through {self.latest_date}."]
        days = self.new_trading_days
        span = f"{days[0]} to {days[-1]}" if len(days) > 1 else (days[0] if days else "none")
        lines = [
            f"Updated CN data from {self.previous_date} to {self.latest_date}.",
            f"New trading days: {len(days)} ({span})",
            f"New symbols: {len(self.added_symbols)}{_preview(self.added_symbols)}",
            f"Delisted symbols: {len(self.delisted_symbols)}{_preview(self.delisted_symbols)}",
        ]
        if self.backup:
            lines.append(f"Rollback backup kept at {self.backup}")
        return lines


def _preview(symbols: list[str], limit: int = 8) -> str:
    if not symbols:
        return ""
    more = f", +{len(symbols) - limit} more" if len(symbols) > limit else ""
    return f" ({', '.join(symbols[:limit])}{more})"


def _stamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# Progress and diagnostics go to stderr so stdout stays clean for JSON output and
# for the MCP stdio transport.
def log(message: str) -> None:
    print(f"[INFO] {_stamp()} {message}", file=sys.stderr, flush=True)


def warn(message: str) -> None:
    print(f"[WARN] {_stamp()} {message}", file=sys.stderr, flush=True)


def read_last_line(path: Path) -> str:
    lines = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def lock_dir_for(data_dir: Path) -> Path:
    return data_dir.parent / ".cn_data_update.lock"


def resolve_mirror(mirror: str | None) -> str | None:
    value = mirror if mirror is not None else os.environ.get(MIRROR_ENV, "")
    return value.strip().rstrip("/") or None


def mirrored(url: str, mirror: str | None) -> str:
    """Route a URL through a prefix-style GitHub proxy, e.g. https://ghfast.top/<url>."""
    return f"{mirror}/{url}" if mirror else url


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


def _read_manifest(url: str, timeout: int) -> Manifest:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return parse_manifest(json.load(response))


def fetch_manifest(source_url: str, timeout: int = 15, mirror: str | None = None) -> Manifest | None:
    """Return the release manifest, or None (with a warning) when it cannot be used.

    The manifest is read from the source directly when possible, so its checksum stays
    independent of any mirror that serves the package. It is an optimization and an
    integrity check, not a requirement: when it is missing or its format changes, the
    update falls back to validating the package itself after download.
    """
    url = manifest_url(source_url)
    errors = (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError)
    try:
        return _read_manifest(url, min(timeout, 10) if mirror else timeout)
    except errors as exc:
        if not mirror:
            warn(f"release manifest unavailable ({exc}); the package will be validated after download")
            return None
        direct_error = exc
    try:
        manifest = _read_manifest(mirrored(url, mirror), timeout)
    except errors as exc:
        warn(f"release manifest unavailable directly ({direct_error}) or via the mirror ({exc}); "
             "the package will be validated after download")
        return None
    warn("release manifest was read through the mirror; its checksum is only as trustworthy as the mirror")
    return manifest


def qlib_calendar_smoke_test(data_dir: Path) -> None:
    # Keep Qlib imports lazy so the module stays importable without initializing Qlib.
    # pylint: disable=import-outside-toplevel
    import qlib
    from qlib.config import REG_CN
    from qlib.data import D

    qlib.init(provider_uri=str(data_dir), region=REG_CN, logging_level="ERROR")
    if len(D.calendar(start_time="2020-01-01", end_time=None, freq="day")) == 0:
        raise RuntimeError("Qlib calendar is empty")


class _Progress:
    """Report download progress: a live line on a terminal, quarter milestones otherwise."""

    def __init__(self, total: int | None, done: int):
        self.total = total
        self.done = done
        self.start_done = done
        self.started = time.monotonic()
        self.last_draw = 0.0
        self.next_milestone = 25
        self.live = sys.stderr.isatty()

    def advance(self, size: int) -> None:
        self.done += size
        now = time.monotonic()
        if self.live and now - self.last_draw >= 0.25:
            self.last_draw = now
            sys.stderr.write(f"\r  {self._line(now)}   ")
            sys.stderr.flush()
        elif not self.live and self.total:
            percent = self.done * 100 // self.total
            if percent >= self.next_milestone and self.next_milestone < 100:
                log(f"downloaded {self._line(now)}")
                self.next_milestone = (percent // 25 + 1) * 25

    def finish(self) -> None:
        now = time.monotonic()
        if self.live:
            sys.stderr.write(f"\r  {self._line(now)}   \n")
            sys.stderr.flush()
        log(f"download finished: {_mib(self.done)} in {now - self.started:.0f}s")

    def _line(self, now: float) -> str:
        elapsed = max(now - self.started, 1e-6)
        speed = (self.done - self.start_done) / elapsed
        if not self.total:
            return f"{_mib(self.done)} at {_mib(speed)}/s"
        percent = self.done * 100 / self.total
        eta = (self.total - self.done) / speed if speed > 0 else 0
        return f"{_mib(self.done)} / {_mib(self.total)} ({percent:.0f}%) at {_mib(speed)}/s, {eta:.0f}s left"


def _mib(size: float) -> str:
    return f"{size / (1 << 20):.1f} MiB"


def _download_once(url: str, target: Path, timeout: int, expected_size: int | None) -> None:
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
            return _download_once(url, target, timeout, expected_size)
        raise
    with response:
        resumed = bool(offset) and getattr(response, "status", None) == 206
        length = response.headers.get("Content-Length")
        start = offset if resumed else 0
        total = expected_size or (start + int(length) if length and length.isdigit() else None)
        progress = _Progress(total, start)
        with target.open("ab" if resumed else "wb") as handle:
            for chunk in iter(lambda: response.read(CHUNK_SIZE), b""):
                handle.write(chunk)
                progress.advance(len(chunk))
        progress.finish()


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
    url = mirrored(options.source_url, resolve_mirror(options.mirror))
    if url != options.source_url:
        log(f"downloading through mirror: {url}")
    last_error: Exception | None = None
    for attempt in range(1, options.retries + 1):
        try:
            _download_once(url, target, options.timeout, manifest.size if manifest else None)
            return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            warn(f"download attempt {attempt}/{options.retries} failed: {exc}")
            if attempt < options.retries:
                time.sleep(options.retry_delay)
    hint = "" if resolve_mirror(options.mirror) else f"; behind a slow connection to GitHub, try --mirror or {MIRROR_ENV}"
    raise UpdateError(f"could not download the community package: {last_error}{hint}")


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


def read_listing_ends(data_dir: Path) -> dict[str, str]:
    """Map each symbol in `instruments/all.txt` to the last date it is listed."""
    path = data_dir / "instruments" / "all.txt"
    if not path.is_file():
        return {}
    ends: dict[str, str] = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 3:
            ends[parts[0]] = max(ends.get(parts[0], ""), parts[2])
    return ends


def describe_change(old_dir: Path, old_latest: str, stage_dir: Path, new_latest: str) -> UpdateResult:
    """Compare the active dataset with the staged one before they are swapped."""
    calendar = [line.strip() for line in (stage_dir / "calendars" / "day.txt").read_text().splitlines()]
    new_days = [day for day in calendar if day and day > old_latest]
    old_ends = read_listing_ends(old_dir)
    new_ends = read_listing_ends(stage_dir)
    added = sorted(set(new_ends) - set(old_ends))
    # Listed through the old latest date, but no longer listed through the new one.
    delisted = sorted(
        symbol for symbol, end in new_ends.items() if old_ends.get(symbol, "") >= old_latest and end < new_latest
    )
    return UpdateResult(
        status="updated",
        latest_date=new_latest,
        previous_date=old_latest,
        new_trading_days=new_days,
        added_symbols=added,
        delisted_symbols=delisted,
    )


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


def run_update(
    options: UpdateOptions, smoke_test: Callable[[Path], None] = qlib_calendar_smoke_test
) -> UpdateResult:
    """Install or update the dataset and describe what changed.

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


def _run_update_locked(options: UpdateOptions, smoke_test: Callable[[Path], None]) -> UpdateResult:
    data_dir = options.data_dir
    base_dir = data_dir.parent
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    work_dir = base_dir / f".cn_data_update_{stamp}"
    backup_dir = base_dir / f"{BACKUP_PREFIX}{stamp}"
    download_file = (options.download_cache_dir or base_dir / "downloads") / DOWNLOAD_NAME
    mirror = resolve_mirror(options.mirror)

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

        manifest = fetch_manifest(options.source_url, options.timeout, mirror)
        if manifest:
            log(f"release manifest: trade_date={manifest.trade_date}")
            if old_latest and manifest.trade_date <= old_latest and not options.force:
                log(f"already up to date; local data ends at {old_latest}, release ends at {manifest.trade_date}")
                return UpdateResult(status="up_to_date", latest_date=old_latest, previous_date=old_latest)

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
            return UpdateResult(status="up_to_date", latest_date=old_latest, previous_date=old_latest)
        log(f"validated package; active_latest={old_latest or 'none'} package_latest={new_latest}")

        if old_latest:
            result = describe_change(data_dir, old_latest, stage_dir, new_latest)
        else:
            result = UpdateResult(status="installed", latest_date=new_latest)
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
            result.backup = str(previous)
            log(f"update complete; latest_date={new_latest} rollback_backup={previous}")
        else:
            log(f"removing validated previous data directory: {previous}")
            shutil.rmtree(previous)
            remove_backups(base_dir, keep=None, label="legacy")
            log(f"update complete; latest_date={new_latest} rollback_backup=none")
        clear_download_cache(download_file)
        return result
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
