"""Python interface for notebooks and scripts.

    >>> from qlib_sync import update_cn, status_cn, health_check_cn
    >>> result = update_cn()
    >>> print("\\n".join(result.summary_lines()))
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .cn import market
from .cn.update import DEFAULT_SOURCE_URL, UpdateOptions, UpdateResult, run_update


def _resolve(path: str | Path | None, default: Path) -> Path:
    return Path(path).expanduser().resolve() if path is not None else default


def update_cn(
    data_dir: str | Path | None = None,
    *,
    mirror: str | None = None,
    protect: Iterable[str] = (),
    keep_backup: bool = False,
    force: bool = False,
    protected_dir: str | Path | None = None,
    source_url: str = DEFAULT_SOURCE_URL,
) -> UpdateResult:
    """Install or update the A-share dataset (default `~/.qlib/qlib_data/cn_data`).

    Raises `qlib_sync.UpdateError` when the update cannot complete; the existing
    dataset is left unchanged in that case.
    """
    options = UpdateOptions(
        data_dir=_resolve(data_dir, market.DEFAULT_DATA_DIR),
        protected_dir=_resolve(protected_dir, market.DEFAULT_PROTECTED_DIR),
        source_url=source_url,
        mirror=mirror,
        protected=list(protect),
        keep_backup=keep_backup,
        force=force,
    )
    return run_update(options)


def status_cn(
    data_dir: str | Path | None = None,
    *,
    offline: bool = False,
    mirror: str | None = None,
    timeout: int = 15,
) -> dict:
    """Local latest date, size, pools, backups, and (unless offline) the remote release."""
    return market.collect_status(
        _resolve(data_dir, market.DEFAULT_DATA_DIR),
        market.DEFAULT_PROTECTED_DIR,
        offline,
        timeout,
        mirror=mirror,
    )


def health_check_cn(data_dir: str | Path | None = None, *, skip_qlib: bool = False) -> dict:
    """Structure checks plus a real Qlib read; returns {"ok", "errors", "checks"}."""
    return market.health_check(_resolve(data_dir, market.DEFAULT_DATA_DIR), market.DEFAULT_PROTECTED_DIR, skip_qlib)
