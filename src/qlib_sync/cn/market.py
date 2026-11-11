"""Operate the local Qlib China market dataset through one safe command-line entry point."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import sys
from pathlib import Path

from ..txn import exclusive_lock, replace_directory
from .update import (
    BACKUP_PREFIX,
    DEFAULT_SOURCE_URL,
    MIRROR_ENV,
    UpdateError,
    UpdateOptions,
    fetch_manifest,
    lock_dir_for,
    resolve_mirror,
    run_update as run_package_update,
)

DEFAULT_DATA_DIR = Path("~/.qlib/qlib_data/cn_data").expanduser()
DEFAULT_PROTECTED_DIR = Path("~/.qlib/qlib_data/custom_instruments/cn_data").expanduser()
DEFAULT_PROTECTED_FILES: tuple[str, ...] = ()


def log(message: str) -> None:
    print(f"[INFO] {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}", file=sys.stderr, flush=True)


def read_calendar(calendar_path: Path) -> list[str]:
    if not calendar_path.is_file():
        return []
    return [line.strip() for line in calendar_path.read_text().splitlines() if line.strip()]


def read_latest_date(data_dir: Path) -> str | None:
    calendar = read_calendar(data_dir / "calendars" / "day.txt")
    return calendar[-1] if calendar else None


def instrument_stats(path: Path) -> dict[str, int | str]:
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    symbols = {line.split()[0] for line in lines}
    return {"name": path.stem, "records": len(lines), "unique_symbols": len(symbols)}


def list_instrument_stats(data_dir: Path) -> list[dict[str, int | str]]:
    instruments_dir = data_dir / "instruments"
    if not instruments_dir.is_dir():
        return []
    return [instrument_stats(path) for path in sorted(instruments_dir.glob("*.txt"))]


def list_backups(data_dir: Path) -> list[Path]:
    return sorted(data_dir.parent.glob(f"{BACKUP_PREFIX}*"), reverse=True)


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def related_dataset_status(data_dir: Path) -> list[dict[str, str | None]]:
    output = []
    for name in ("cn_etf_data", "cn_etf_data_hfq"):
        path = data_dir.parent / name
        if path.exists():
            output.append({"name": name, "path": str(path), "latest_date": read_latest_date(path)})
    return output


def collect_status(
    data_dir: Path,
    protected_dir: Path,
    offline: bool,
    timeout: int,
    source_url: str = DEFAULT_SOURCE_URL,
    mirror: str | None = None,
) -> dict:
    backups = list_backups(data_dir)
    protected_files = sorted(path.name for path in protected_dir.glob("*.txt")) if protected_dir.is_dir() else []
    result = {
        "data_dir": str(data_dir),
        "local_latest_date": read_latest_date(data_dir),
        "size_bytes": directory_size(data_dir),
        "instrument_pools": list_instrument_stats(data_dir),
        "protected_dir": str(protected_dir),
        "protected_files": protected_files,
        "backups": [str(path) for path in backups],
        "related_datasets": related_dataset_status(data_dir),
        "remote_release": None,
        "update_available": None,
    }
    if not offline:
        manifest = fetch_manifest(source_url, timeout, resolve_mirror(mirror))
        if manifest:
            result["remote_release"] = dataclasses.asdict(manifest)
            local = result["local_latest_date"]
            result["update_available"] = local is None or manifest.trade_date > local
    return result


def print_status(result: dict) -> None:
    print(f"CN data: {result['data_dir']}")
    print(f"Local latest date: {result['local_latest_date'] or 'missing'}")
    print(f"Data size: {human_size(result['size_bytes'])}")
    remote = result.get("remote_release")
    if remote:
        size = f", {human_size(remote['size'])}" if remote.get("size") else ""
        state = "update available" if result["update_available"] else "up to date"
        print(f"Community release: trade date {remote['trade_date']}{size} ({state})")
    print("Instrument pools:")
    for pool in result["instrument_pools"]:
        print(f"  {pool['name']}: records={pool['records']} unique_symbols={pool['unique_symbols']}")
    protected = ", ".join(result["protected_files"]) or "none"
    print(f"Protected local pools: {protected} ({result['protected_dir']})")
    print(f"Rollback backups: {len(result['backups'])}")
    for dataset in result["related_datasets"]:
        print(f"Related dataset {dataset['name']}: latest={dataset['latest_date']} path={dataset['path']}")


def run_update(args: argparse.Namespace) -> None:
    protected = args.protect or list(DEFAULT_PROTECTED_FILES)
    options = UpdateOptions(
        data_dir=args.data_dir,
        protected_dir=args.protected_dir,
        protected=protected,
        refresh_protected=args.refresh_protected,
        keep_backup=args.keep_backup,
        force=args.force,
        source_url=args.source_url,
        mirror=args.mirror,
    )
    log(f"start update: data_dir={args.data_dir} protected={','.join(protected) or 'none'}")
    try:
        result = run_package_update(options)
    except UpdateError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if args.json:
        print(json.dumps(dataclasses.asdict(result), indent=2))
    else:
        print("\n".join(result.summary_lines()))


def check_structure(data_dir: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    notes: list[str] = []
    required = (data_dir / "calendars" / "day.txt", data_dir / "features", data_dir / "instruments" / "all.txt")
    for path in required:
        if not path.exists():
            errors.append(f"missing required path: {path}")
    calendar = read_calendar(data_dir / "calendars" / "day.txt")
    if calendar:
        if calendar != sorted(set(calendar)):
            errors.append("daily calendar is not strictly increasing and unique")
        try:
            [dt.date.fromisoformat(value) for value in calendar]
        except ValueError as exc:
            errors.append(f"daily calendar contains an invalid date: {exc}")
        notes.append(f"calendar_entries={len(calendar)} latest={calendar[-1]}")
    elif not errors:
        errors.append("daily calendar is empty")
    pools = list_instrument_stats(data_dir)
    if not pools:
        errors.append("no instrument pools found")
    else:
        notes.append(f"instrument_pools={len(pools)}")
    feature_files = list((data_dir / "features").glob("*/*.day.bin")) if (data_dir / "features").is_dir() else []
    if not feature_files:
        errors.append("no daily feature files found")
    else:
        notes.append(f"daily_feature_files={len(feature_files)}")
    return errors, notes


def check_protected(data_dir: Path, protected_dir: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    notes: list[str] = []
    if not protected_dir.is_dir():
        return errors, notes
    files = sorted(protected_dir.glob("*.txt"))
    if not files:
        return errors, notes
    for protected_file in files:
        active_file = data_dir / "instruments" / protected_file.name
        if not active_file.is_file():
            errors.append(f"protected pool is missing from active data: {protected_file.name}")
        elif protected_file.read_bytes() != active_file.read_bytes():
            errors.append(f"protected pool differs from active data: {protected_file.name}")
        else:
            notes.append(f"protected_pool_ok={protected_file.name}")
    return errors, notes


def qlib_smoke_test(data_dir: Path) -> tuple[list[str], list[str]]:
    # Keep Qlib imports lazy so status and backup inspection remain lightweight.
    # pylint: disable=import-outside-toplevel
    import qlib
    from qlib.config import REG_CN
    from qlib.data import D

    errors: list[str] = []
    notes: list[str] = []
    try:
        qlib.init(provider_uri=str(data_dir), region=REG_CN, logging_level="ERROR")
        calendar = D.calendar(start_time="2020-01-01", end_time=None, freq="day")
        if len(calendar) == 0:
            errors.append("Qlib returned an empty daily calendar")
            return errors, notes
        notes.append(f"qlib_latest={calendar[-1].date().isoformat()}")
        frame = D.features(
            ["SH600000"],
            ["$close", "$change"],
            start_time=str(calendar[max(0, len(calendar) - 20)].date()),
            end_time=str(calendar[-1].date()),
        )
        if frame.dropna(how="all").empty:
            errors.append("Qlib feature read returned no close/change data for SH600000")
        else:
            notes.append(f"qlib_feature_rows={len(frame.dropna(how='all'))}")
    except Exception as exc:  # The command must report provider failures instead of hiding them.
        errors.append(f"Qlib read failed: {exc}")
    return errors, notes


def health_check(data_dir: Path, protected_dir: Path, skip_qlib: bool = False) -> dict:
    errors, notes = check_structure(data_dir)
    protected_errors, protected_notes = check_protected(data_dir, protected_dir)
    errors.extend(protected_errors)
    notes.extend(protected_notes)
    if not skip_qlib and not errors:
        qlib_errors, qlib_notes = qlib_smoke_test(data_dir)
        errors.extend(qlib_errors)
        notes.extend(qlib_notes)
    return {"ok": not errors, "errors": errors, "checks": notes}


def run_health_check(args: argparse.Namespace) -> int:
    log(f"start health-check: data_dir={args.data_dir}")
    report = health_check(args.data_dir, args.protected_dir, args.skip_qlib)
    errors, notes = report["errors"], report["checks"]
    for note in notes:
        print(f"[OK] {note}")
    for error in errors:
        print(f"[ERROR] {error}", file=sys.stderr)
    if errors:
        log(f"end health-check: failed errors={len(errors)}")
        return 1
    log("end health-check: passed")
    return 0


def resolve_backup(data_dir: Path, value: Path | None) -> Path:
    if value is not None:
        backup = value.expanduser().resolve()
        if backup.parent != data_dir.parent.resolve() or not backup.name.startswith(BACKUP_PREFIX):
            raise ValueError(f"backup must be a managed CN data backup under {data_dir.parent}")
        if not backup.is_dir():
            raise FileNotFoundError(f"backup not found: {backup}")
        return backup
    backups = list_backups(data_dir)
    if not backups:
        raise FileNotFoundError(f"no rollback backups found under {data_dir.parent}")
    return backups[0]


def run_restore(args: argparse.Namespace) -> None:
    backups = list_backups(args.data_dir)
    if args.list:
        for backup in backups:
            print(f"{backup}\tlatest={read_latest_date(backup)}\tsize={human_size(directory_size(backup))}")
        return
    if not args.yes:
        raise RuntimeError("restore changes the active dataset; rerun with --yes after reviewing `restore --list`")
    backup = resolve_backup(args.data_dir, args.backup)
    backup_errors, _ = check_structure(backup)
    if backup_errors:
        raise RuntimeError(f"backup failed structural validation: {'; '.join(backup_errors)}")
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    displaced = args.data_dir.parent / f"cn_data_before_restore_{stamp}"
    log(f"start restore: backup={backup} active={args.data_dir}")
    with exclusive_lock(lock_dir_for(args.data_dir)):
        previous = replace_directory(args.data_dir, backup, displaced)
    log(f"end restore: latest_date={read_latest_date(args.data_dir)} displaced_data={previous or 'none'}")


def add_data_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--protected-dir", type=Path, default=DEFAULT_PROTECTED_DIR)


def add_mirror_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mirror",
        help=f"Prefix-style GitHub proxy, e.g. https://ghfast.top (default: ${MIRROR_ENV})",
    )


def add_subcommands(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="Show local data, pools, backups, and remote release metadata")
    add_data_args(status_parser)
    status_parser.add_argument("--offline", action="store_true", help="Do not read the remote release manifest")
    status_parser.add_argument("--timeout", type=int, default=15)
    status_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    add_mirror_arg(status_parser)

    update_parser = subparsers.add_parser(
        "update", help="Install, or safely replace CN data with, the latest community package"
    )
    add_data_args(update_parser)
    update_parser.add_argument(
        "--protect", action="append", help="Protected instrument filename; repeat for multiple files"
    )
    update_parser.add_argument("--refresh-protected", action="store_true")
    update_parser.add_argument("--keep-backup", action="store_true", help="Keep one rollback backup after a successful update")
    update_parser.add_argument("--force", action="store_true", help="Replace even when package and local dates match")
    update_parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL, help="Community package URL")
    update_parser.add_argument("--json", action="store_true", help="Print the update result as JSON")
    add_mirror_arg(update_parser)

    health_parser = subparsers.add_parser("health-check", help="Validate structure, protected pools, and Qlib reads")
    add_data_args(health_parser)
    health_parser.add_argument("--skip-qlib", action="store_true", help="Run filesystem checks only")

    restore_parser = subparsers.add_parser("restore", help="List or restore managed CN data backups")
    add_data_args(restore_parser)
    restore_parser.add_argument("--list", action="store_true", help="List rollback backups without changing data")
    restore_parser.add_argument("--backup", type=Path, help="Specific managed backup; defaults to the newest")
    restore_parser.add_argument("--yes", action="store_true", help="Confirm replacement of the active data directory")



def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_subcommands(parser)
    return parser


def run(args: argparse.Namespace) -> None:
    args.data_dir = args.data_dir.expanduser().resolve()
    args.protected_dir = args.protected_dir.expanduser().resolve()
    if args.command == "status":
        result = collect_status(args.data_dir, args.protected_dir, args.offline, args.timeout, mirror=args.mirror)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=True))
        else:
            print_status(result)
    elif args.command == "update":
        run_update(args)
    elif args.command == "health-check":
        raise SystemExit(run_health_check(args))
    elif args.command == "restore":
        run_restore(args)
    else:
        raise AssertionError(f"unknown command: {args.command}")


def main() -> None:
    run(get_parser().parse_args())


if __name__ == "__main__":
    main()
