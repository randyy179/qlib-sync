"""Install or update Qlib CN daily data from the community release."""

from __future__ import annotations

import datetime as dt
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

from .._upstream import validate_archive as upstream
from ..txn import LockedError, exclusive_lock, replace_directory

DEFAULT_SOURCE_URL = "https://github.com/chenditc/investment_data/releases/latest/download/qlib_bin.tar.gz"
MANIFEST_NAME = "qlib_bin.manifest.json"
BACKUP_PREFIX = "cn_data_backup_before_latest_"
DOWNLOAD_NAME = "qlib_bin.tar.gz.part"
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CHUNK_SIZE = 1 << 20
USER_AGENT = "qlib-sync"
MIRROR_ENV = "QLIB_SYNC_MIRROR"
INSTALL_RECORD = ".qlib-sync.json"
LATEST_SEGMENT = "/releases/latest/download/"
TAG_IN_URL = re.compile(r"/releases/download/([^/]+)/")
UNVERIFIED_HINT = "pass --allow-unverified to install without verification"


class UpdateError(RuntimeError):
    """The update could not be completed; the active dataset is unchanged."""


class ManifestError(UpdateError):
    """The release manifest could not be read, or the upstream validator rejected it."""


@dataclass
class Release:
    """One community release, pinned by tag, with its manifest already validated.

    `manifest` is what chenditc/investment_data's validate_archive.py returned for the raw
    manifest bytes, so every field has passed the maintainers' own format checks.
    """

    tag: str
    tag_source: str  # "release URL", or "manifest" when the tag could not be resolved independently
    archive_url: str
    manifest: dict
    manifest_path: Path

    @property
    def trade_date(self) -> str:
        return self.manifest["target_trade_date"]

    @property
    def sha256(self) -> str:
        return self.manifest["archive_sha256"].removeprefix("sha256:")

    @property
    def size(self) -> int:
        return self.manifest["archive_size_bytes"]

    def describe(self) -> dict:
        return {
            "tag": self.tag,
            "tag_source": self.tag_source,
            "trade_date": self.trade_date,
            "size": self.size,
            "sha256": self.sha256,
        }


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
    allow_unverified: bool = False
    retries: int = 3
    retry_delay: float = 3.0
    timeout: int = 60


@dataclass
class UpdateResult:
    """What an update did, for people (`summary_lines`) and for programs (`asdict`)."""

    status: str  # "installed", "updated", "republished", or "up_to_date"
    latest_date: str
    previous_date: str | None = None
    new_trading_days: list[str] = field(default_factory=list)
    added_symbols: list[str] = field(default_factory=list)
    delisted_symbols: list[str] = field(default_factory=list)
    backup: str | None = None
    verified: bool = False
    verification: str = ""

    def summary_lines(self) -> list[str]:
        if self.status == "up_to_date":
            return [f"Already up to date: data ends at {self.latest_date}."]
        if self.status == "installed":
            return [f"Installed CN data through {self.latest_date}.", self._verification_line()]
        if self.status == "republished":
            return [
                f"Replaced CN data through {self.latest_date} with a corrected upstream release.",
                self._verification_line(),
            ]
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
        lines.append(self._verification_line())
        return lines

    def _verification_line(self) -> str:
        if self.verified:
            return f"Verified: {self.verification}."
        return f"WARNING: installed WITHOUT verification ({self.verification})."


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


def tilde(path: Path | str) -> str:
    """Show paths under the home folder as ~/..., shorter and without the account name."""
    text, home = str(path), str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def read_last_line(path: Path) -> str:
    lines = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def lock_dir_for(data_dir: Path) -> Path:
    return data_dir.parent / ".cn_data_update.lock"


def read_install_record(data_dir: Path) -> dict:
    """The release this dataset was installed from, written by qlib-sync; {} if unknown."""
    try:
        return json.loads((data_dir / INSTALL_RECORD).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def write_install_record(data_dir: Path, trade_date: str, release: Release | None) -> None:
    record = {
        "trade_date": trade_date,
        "release_tag": release.tag if release else None,
        "archive_sha256": release.sha256 if release else None,
        "verified": release is not None,
        "installed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    (data_dir / INSTALL_RECORD).write_text(json.dumps(record, indent=2) + "\n")


def is_republished(release: Release, old_latest: str, record: dict) -> bool:
    """Whether the verified release should replace local data for the same trade date.

    True when upstream republished that date with a new checksum (it does so after fixing data),
    or when the local copy was installed with --allow-unverified. Records written before 0.2.0
    carry no `verified` flag and are compared by checksum only.
    """
    if release.trade_date != old_latest or record.get("trade_date") != old_latest:
        return False
    if record.get("verified") is False:
        return True
    installed = record.get("archive_sha256")
    return bool(installed) and installed != release.sha256


def resolve_mirror(mirror: str | None) -> str | None:
    value = mirror if mirror is not None else os.environ.get(MIRROR_ENV, "")
    return value.strip().rstrip("/") or None


def mirrored(url: str, mirror: str | None) -> str:
    """Route a URL through a prefix-style GitHub proxy, e.g. https://ghfast.top/<url>."""
    return f"{mirror}/{url}" if mirror else url


def manifest_url(source_url: str) -> str:
    return f"{source_url.rsplit('/', 1)[0]}/{MANIFEST_NAME}"


def tagged_url(source_url: str, tag: str) -> str:
    """Pin a releases/latest URL to one tag, so the manifest and archive come from the same release."""
    return source_url.replace(LATEST_SEGMENT, f"/releases/download/{tag}/") if LATEST_SEGMENT in source_url else source_url


class _StopAtRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D102 - returning None surfaces the 3xx
        return None


def resolve_release_tag(source_url: str, timeout: int) -> str | None:
    """The tag a URL names, or the tag GitHub's releases/latest redirect points to right now.

    Returns None for sources that name no tag (a local file, a custom server).
    """
    named = TAG_IN_URL.search(source_url)
    if named:
        return named.group(1)
    if LATEST_SEGMENT not in source_url:
        return None
    request = urllib.request.Request(manifest_url(source_url), method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        urllib.request.build_opener(_StopAtRedirect).open(request, timeout=timeout).close()
    except urllib.error.HTTPError as exc:
        location = TAG_IN_URL.search(exc.headers.get("Location", "") or "")
        if 300 <= exc.code < 400 and location:
            return location.group(1)
        raise
    raise OSError("releases/latest did not redirect to a tagged release")


def _read_bytes(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _read_manifest_bytes(url: str, timeout: int, mirror: str | None, try_direct: bool = True) -> bytes:
    """Read the manifest directly when possible, so its checksum does not depend on the mirror."""
    direct_error: Exception | str = "skipped: GitHub was unreachable"
    try:
        if not try_direct:
            raise ConnectionError(direct_error)
        return _read_bytes(url, min(timeout, 10) if mirror else timeout)
    except (OSError, urllib.error.URLError) as exc:
        if not mirror:
            raise ManifestError(f"could not read the release manifest: {exc}") from exc
        direct_error = exc
    try:
        raw = _read_bytes(mirrored(url, mirror), timeout)
    except (OSError, urllib.error.URLError) as exc:
        raise ManifestError(
            f"could not read the release manifest directly ({direct_error}) or via the mirror ({exc})"
        ) from exc
    warn("release manifest was read through the mirror; its checksum is only as trustworthy as the mirror")
    return raw


def fetch_release(source_url: str, timeout: int, mirror: str | None, scratch_dir: Path) -> Release:
    """Pin the release by tag, read its manifest, and validate it with the upstream validator.

    Raises ManifestError when the manifest is unavailable or the validator rejects it.
    """
    tag, tag_source, github_reachable = None, "release URL", True
    try:
        tag = resolve_release_tag(source_url, min(timeout, 15))
    except (OSError, urllib.error.URLError) as exc:
        if not mirror:
            raise ManifestError(f"could not resolve the current release tag: {exc}") from exc
        warn(f"could not reach GitHub to resolve the release tag ({exc}); reading it through the mirror")
        github_reachable = False
    pinned = tagged_url(source_url, tag) if tag else source_url
    raw = _read_manifest_bytes(manifest_url(pinned), timeout, mirror, try_direct=github_reachable)
    if tag is None:
        # Without an independent tag, the manifest's own tag is all there is to check against.
        try:
            tag = json.loads(raw.decode("utf-8")).get("release_tag")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
            raise ManifestError(f"the release manifest is not valid JSON: {exc}") from exc
        if not isinstance(tag, str) or not DATE_PATTERN.match(tag):
            raise ManifestError(f"the release manifest has no usable release_tag: {tag!r}")
        tag_source = "manifest"
        pinned = tagged_url(source_url, tag)
        warn("the release tag was taken from the manifest itself, so the tag check cannot catch a mismatched manifest")
    scratch_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = scratch_dir / MANIFEST_NAME
    manifest_path.write_bytes(raw)
    try:
        manifest, _digest = upstream._load_manifest(manifest_path, tag, True)  # pylint: disable=protected-access
    except upstream.ValidationFailure as exc:
        raise ManifestError(f"the release manifest failed the upstream validator ({exc.token})") from exc
    return Release(tag=tag, tag_source=tag_source, archive_url=pinned, manifest=manifest, manifest_path=manifest_path)


def verify_with_upstream(archive: Path, release: Release) -> str:
    """Run chenditc/investment_data's validate_archive.py exactly as its README documents."""
    try:
        upstream.validate(archive, release.manifest_path, release.tag, require_publishable=True)
    except upstream.ValidationFailure as exc:
        # A rejected package must not linger as a 550 MB cache file or be reused next run.
        clear_download_cache(archive)
        raise UpdateError(f"the package failed the upstream validator ({exc.token}); nothing was changed") from exc
    return f"investment_data validate_archive.py --require-publishable passed for release {release.tag}"


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


def prepare_download_cache(target: Path, release: Release | None) -> None:
    """Discard a cached partial download that belongs to a different package."""
    marker = target.with_name(f"{target.name}.sha256")
    expected = release.sha256 if release else None
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


def download_package(options: UpdateOptions, target: Path, release: Release | None) -> None:
    if release and target.exists() and target.stat().st_size == release.size:
        log("cached package is complete; skipping download")
        return
    if target.exists():
        log(f"resuming incomplete package download: {tilde(target)}")
    source = release.archive_url if release else options.source_url
    url = mirrored(source, resolve_mirror(options.mirror))
    if url != source:
        log(f"downloading through mirror: {url}")
    last_error: Exception | None = None
    for attempt in range(1, options.retries + 1):
        try:
            _download_once(url, target, options.timeout, release.size if release else None)
            return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            warn(f"download attempt {attempt}/{options.retries} failed: {exc}")
            if attempt < options.retries:
                time.sleep(options.retry_delay)
    hint = "" if resolve_mirror(options.mirror) else f"; behind a slow connection to GitHub, try --mirror or {MIRROR_ENV}"
    raise UpdateError(f"could not download the community package: {last_error}{hint}")


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
            log(f"saved protected instrument copy: {tilde(protected_file)}")
        if protected_file.is_file():
            shutil.copy2(protected_file, stage_dir / "instruments" / instrument)
            log(f"restoring protected instrument: {instrument}")
        else:
            log(f"protected instrument not found; skipping restoration: {instrument}")


def remove_backups(base_dir: Path, keep: Path | None, label: str) -> None:
    for backup in sorted(base_dir.glob(f"{BACKUP_PREFIX}*")):
        if backup.is_dir() and backup != keep:
            log(f"removing {label} rollback backup: {tilde(backup)}")
            shutil.rmtree(backup)


def run_update(
    options: UpdateOptions, smoke_test: Callable[[Path], None] = qlib_calendar_smoke_test
) -> UpdateResult:
    """Install or update the dataset and describe what changed.

    The release is pinned by tag and its manifest is validated first, so an up-to-date dataset
    costs two small requests. The package is checked with the dataset maintainers' own
    validator (`--require-publishable`), extracted to a staging directory, and swapped in with
    `replace_directory`, which restores the previous dataset if the smoke test fails. Without
    a valid manifest the update is refused unless `allow_unverified` is set.
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
        work_dir.mkdir(parents=True)
        calendar = data_dir / "calendars" / "day.txt"
        if calendar.is_file():
            old_latest = read_last_line(calendar)
            log(f"starting CN data update; active_dir={tilde(data_dir)} active_latest={old_latest}")
        elif data_dir.exists() and any(data_dir.iterdir()):
            raise UpdateError(f"{data_dir} exists but is not a Qlib daily dataset; refusing to replace it")
        else:
            old_latest = None
            log(f"no existing CN data at {tilde(data_dir)}; installing from the community release")

        release: Release | None
        unverified_reason = ""
        try:
            release = fetch_release(options.source_url, options.timeout, mirror, work_dir)
        except ManifestError as exc:
            if not options.allow_unverified:
                raise UpdateError(f"{exc}; refusing to install unverified data ({UNVERIFIED_HINT})") from exc
            warn(f"{exc}; continuing WITHOUT verification because --allow-unverified was given")
            release, unverified_reason = None, str(exc)

        republished = False
        if release:
            log(f"release {release.tag}: trade_date={release.trade_date}; manifest passed the upstream validator")
            republished = bool(old_latest) and is_republished(release, old_latest, read_install_record(data_dir))
            if republished:
                log(f"the {old_latest} release was republished upstream (new checksum); replacing local data")
            elif old_latest and release.trade_date <= old_latest and not options.force:
                log(f"already up to date; local data ends at {old_latest}, release ends at {release.trade_date}")
                return UpdateResult(
                    status="up_to_date", latest_date=old_latest, previous_date=old_latest, verified=True,
                    verification=f"manifest for release {release.tag} passed the upstream validator",
                )

        log("downloading community package")
        download_file.parent.mkdir(parents=True, exist_ok=True)
        prepare_download_cache(download_file, release)
        download_package(options, download_file, release)
        if release:
            log("running investment_data's validate_archive.py --require-publishable")
            verification = verify_with_upstream(download_file, release)
            log(verification)
        else:
            verification = f"--allow-unverified: {unverified_reason}"

        log("extracting package")
        extract_dir = work_dir / "extract"
        extract_dir.mkdir(parents=True)
        stage_dir = extract_package(download_file, extract_dir)
        new_latest = read_last_line(stage_dir / "calendars" / "day.txt")
        if not DATE_PATTERN.match(new_latest):
            raise UpdateError(f"downloaded calendar has an invalid latest date: {new_latest}")
        if old_latest and new_latest < old_latest:
            raise UpdateError(f"refusing downgrade: package ends at {new_latest}, active data ends at {old_latest}")
        if old_latest and new_latest == old_latest and not (options.force or republished):
            log(f"package ends at {new_latest}, same as active data; no replacement needed")
            clear_download_cache(download_file)
            return UpdateResult(status="up_to_date", latest_date=old_latest, previous_date=old_latest)
        log(f"package ready; active_latest={old_latest or 'none'} package_latest={new_latest}")

        if old_latest:
            result = describe_change(data_dir, old_latest, stage_dir, new_latest)
            if republished:
                result.status = "republished"
        else:
            result = UpdateResult(status="installed", latest_date=new_latest)
        result.verified, result.verification = release is not None, verification
        apply_protected_instruments(options, stage_dir)
        write_install_record(stage_dir, new_latest, release)

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
            log(f"update complete; latest_date={new_latest} rollback_backup={tilde(previous)}")
        else:
            log(f"removing validated previous data directory: {tilde(previous)}")
            shutil.rmtree(previous)
            remove_backups(base_dir, keep=None, label="legacy")
            log(f"update complete; latest_date={new_latest} rollback_backup=none")
        clear_download_cache(download_file)
        return result
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
