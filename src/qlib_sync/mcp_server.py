"""MCP server: let AI assistants check, update, and read the local A-share dataset.

Run with `qlib-sync mcp` (stdio). Requires the optional extra: pip install "qlib-sync[mcp]".
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import re
import threading
from pathlib import Path
from typing import Any, Literal

try:
    import anyio
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise SystemExit('The MCP server needs the optional dependency: pip install "qlib-sync[mcp]"') from exc

from .cn import market
from .cn.update import DEFAULT_SOURCE_URL, UpdateError, UpdateOptions, run_update

DATA_DIR_ENV = "QLIB_SYNC_DATA_DIR"
PRICE_FIELDS = ("open", "high", "low", "close", "vwap")
ALL_FIELDS = (*PRICE_FIELDS, "volume", "amount", "change", "factor")
DEFAULT_FIELDS = ("open", "high", "low", "close", "volume", "change")
MAX_SYMBOLS = 20
MAX_ROWS_LIMIT = 5000

INSTRUCTIONS = """\
Local China A-share daily data (Qlib format), installed and kept current by qlib-sync.
Call cn_status first to see how recent the data is. If the user needs recent prices and
an update is available, call cn_update (it takes about a minute and is safe: on any
failure the previous data is restored). Symbols accept 600519, SH600519 or 600519.SH.
cn_get_prices returns real traded prices by default; pass price_basis="adjusted" for the
split- and dividend-adjusted series used in backtests. Data covers trading days only;
the latest date is the last close, never intraday.
"""

_SYMBOL = re.compile(r"^(?:(SH|SZ|BJ)(\d{6})|(\d{6})(?:\.(SH|SZ|BJ))?)$")


def normalize_symbol(value: str) -> str:
    """Map 600519, sh600519, or 600519.SH to Qlib's SH600519."""
    match = _SYMBOL.match(value.strip().upper())
    if not match:
        raise ToolError(f"unrecognized symbol {value!r}; use a 6-digit code such as 600519 or SH600519")
    prefix, code = (match.group(1), match.group(2)) if match.group(1) else (match.group(4), match.group(3))
    if prefix:
        return f"{prefix}{code}"
    if code.startswith("92") or code[0] in "48":
        return f"BJ{code}"
    if code[0] in "69":
        return f"SH{code}"
    return f"SZ{code}"


class QlibReader:
    """Initialize Qlib once per process and re-initialize after the data is replaced."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self._ready = False
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        with self._lock:
            self._ready = False

    def _ensure(self) -> None:
        # pylint: disable=import-outside-toplevel
        import qlib
        from qlib.config import REG_CN

        if not (self.data_dir / "calendars" / "day.txt").is_file():
            raise ToolError(f"no A-share data at {self.data_dir}; call cn_update to install it")
        if not self._ready:
            # kernels=1 keeps reads in-process; spawning workers per request is far slower.
            qlib.init(provider_uri=str(self.data_dir), region=REG_CN, logging_level="ERROR", kernels=1)
            self._ready = True

    def prices(self, symbols: list[str], start: str, end: str, fields: list[str], adjusted: bool):
        from qlib.data import D  # pylint: disable=import-outside-toplevel

        with self._lock:
            self._ensure()
            columns = sorted(set(fields) | ({"factor"} if not adjusted else set()))
            frame = D.features(symbols, [f"${name}" for name in columns], start_time=start, end_time=end)
        frame.columns = [column.lstrip("$") for column in frame.columns]
        if not adjusted:
            # Qlib stores adjusted prices: raw = adjusted / factor, raw volume = adjusted * factor.
            for name in PRICE_FIELDS:
                if name in frame:
                    frame[name] = frame[name] / frame["factor"]
            if "volume" in frame:
                frame["volume"] = frame["volume"] * frame["factor"]
        frame = frame[[name for name in fields if name in frame]].dropna(how="all").astype("float64")
        # Qlib stores float32; round away the noise so 1452.9998779 reads as 1453.0.
        return frame.round({**{name: 3 for name in PRICE_FIELDS}, "volume": 0, "amount": 2, "change": 6, "factor": 6})

    def pool(self, name: str, date: str | None) -> list[str]:
        from qlib.data import D  # pylint: disable=import-outside-toplevel

        with self._lock:
            self._ensure()
            day = date or market.read_latest_date(self.data_dir)
            return sorted(D.list_instruments(D.instruments(name), start_time=day, end_time=day, as_list=True))


def create_server(data_dir: Path | None = None, source_url: str = DEFAULT_SOURCE_URL) -> MCPServer:
    data_dir = (data_dir or Path(os.environ.get(DATA_DIR_ENV, market.DEFAULT_DATA_DIR))).expanduser().resolve()
    reader = QlibReader(data_dir)
    server = MCPServer("qlib-sync", instructions=INSTRUCTIONS)

    @server.tool(
        name="cn_status",
        annotations=ToolAnnotations(title="A-share data status", read_only_hint=True, open_world_hint=True),
    )
    def cn_status(offline: bool = False) -> dict[str, Any]:
        """Report the local A-share data's latest trading date, size, and stock pools, and whether
        the community release has newer data (set offline=True to skip the network check)."""
        result = market.collect_status(data_dir, market.DEFAULT_PROTECTED_DIR, offline, 15)
        result.pop("related_datasets", None)
        result["pools"] = [pool["name"] for pool in result.pop("instrument_pools")]
        result["today"] = dt.date.today().isoformat()
        return result

    @server.tool(
        name="cn_update",
        annotations=ToolAnnotations(
            title="Update A-share data",
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
    )
    async def cn_update(force: bool = False) -> dict[str, Any]:
        """Install or update the local A-share data from the community release.

        Returns quickly when already current. Otherwise downloads about 550 MB, verifies its
        checksum, and swaps it in; this takes roughly a minute. On any failure the previous
        data is restored. The result lists new trading days and listed/delisted symbols.
        """
        options = UpdateOptions(
            data_dir=data_dir, protected_dir=market.DEFAULT_PROTECTED_DIR, source_url=source_url, force=force
        )
        try:
            result = await anyio.to_thread.run_sync(run_update, options)
        except UpdateError as exc:
            raise ToolError(f"update failed and the existing data is unchanged: {exc}") from exc
        if result.status != "up_to_date":
            reader.invalidate()
        payload = dataclasses.asdict(result)
        payload["summary"] = result.summary_lines()
        return payload

    @server.tool(
        name="cn_health_check",
        annotations=ToolAnnotations(title="Check A-share data", read_only_hint=True, open_world_hint=False),
    )
    async def cn_health_check() -> dict[str, Any]:
        """Validate the local data's structure and perform a real Qlib read; returns ok, errors, checks."""
        return await anyio.to_thread.run_sync(market.health_check, data_dir, market.DEFAULT_PROTECTED_DIR)

    @server.tool(
        name="cn_get_prices",
        annotations=ToolAnnotations(title="Get A-share daily prices", read_only_hint=True, open_world_hint=False),
    )
    async def cn_get_prices(
        symbols: list[str],
        start: str,
        end: str | None = None,
        fields: list[str] | None = None,
        price_basis: Literal["raw", "adjusted"] = "raw",
        max_rows: int = 500,
    ) -> dict[str, Any]:
        """Daily bars for up to 20 A-share symbols between start and end (YYYY-MM-DD, inclusive;
        end defaults to the latest date).

        fields: any of open, high, low, close, vwap, volume, amount, change, factor
        (default open, high, low, close, volume, change). change is the daily return as a
        fraction (0.01 = +1%). price_basis="raw" gives real traded prices and share volume;
        "adjusted" gives Qlib's split- and dividend-adjusted series. Rows are capped by max_rows.
        """
        if not symbols:
            raise ToolError("pass at least one symbol, for example ['600519']")
        if len(symbols) > MAX_SYMBOLS:
            raise ToolError(f"at most {MAX_SYMBOLS} symbols per call; split the request")
        wanted = list(fields or DEFAULT_FIELDS)
        unknown = sorted(set(wanted) - set(ALL_FIELDS))
        if unknown:
            raise ToolError(f"unknown fields {unknown}; choose from {list(ALL_FIELDS)}")
        codes = [normalize_symbol(symbol) for symbol in symbols]
        for value in (start, end):
            if value is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ToolError(f"dates must be YYYY-MM-DD, got {value!r}")
        limit = max(1, min(max_rows, MAX_ROWS_LIMIT))
        latest = market.read_latest_date(data_dir)
        frame = await anyio.to_thread.run_sync(
            reader.prices, codes, start, end or latest, wanted, price_basis == "adjusted"
        )
        rows = [
            {"symbol": symbol, "date": day.date().isoformat(), **{k: _clean(v) for k, v in values.items()}}
            for (symbol, day), values in frame.to_dict(orient="index").items()
        ]
        missing = sorted(set(codes) - {row["symbol"] for row in rows})
        return {
            "price_basis": price_basis,
            "data_latest_date": latest,
            "rows": rows[:limit],
            "total_rows": len(rows),
            "truncated": len(rows) > limit,
            "symbols_without_data": missing,
        }

    @server.tool(
        name="cn_list_pool",
        annotations=ToolAnnotations(title="List stock pool members", read_only_hint=True, open_world_hint=False),
    )
    async def cn_list_pool(pool: str = "csi300", date: str | None = None) -> dict[str, Any]:
        """Members of a stock pool on a date (default: the latest date). Pools: all, csi100, csi300,
        csi500, csi800, csi1000, csiall, plus any local pools; cn_status lists what exists."""
        available = sorted(path.stem for path in (data_dir / "instruments").glob("*.txt"))
        if pool not in available:
            raise ToolError(f"unknown pool {pool!r}; available: {available}")
        members = await anyio.to_thread.run_sync(reader.pool, pool, date)
        return {"pool": pool, "date": date or market.read_latest_date(data_dir), "count": len(members), "symbols": members}

    return server


def _clean(value: float) -> float | None:
    return None if value != value else value  # NaN -> null in JSON


def main(argv: list[str] | None = None) -> None:
    import argparse  # pylint: disable=import-outside-toplevel

    parser = argparse.ArgumentParser(prog="qlib-sync mcp", description="Serve the A-share dataset over MCP (stdio).")
    parser.add_argument("--data-dir", type=Path, help=f"Dataset directory (default: ${DATA_DIR_ENV} or ~/.qlib/qlib_data/cn_data)")
    args = parser.parse_args(argv)
    create_server(args.data_dir).run("stdio")


if __name__ == "__main__":
    main()
