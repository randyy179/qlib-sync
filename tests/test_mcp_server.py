import asyncio
import tarfile
from pathlib import Path

import numpy as np
import pytest

from mcp import Client

from qlib_sync import mcp_server
from qlib_sync.mcp_server import create_server, normalize_symbol

DAYS = ["2026-07-01", "2026-07-02", "2026-07-03"]
BARS = {
    # symbol: (adjusted close per day, factor)
    "SH600519": ([350.0, 352.0, 351.0], 0.25),
    "SH600000": ([10.0, 10.1, 10.2], 0.5),
}


def _write_qlib_dataset(root: Path) -> Path:
    (root / "calendars").mkdir(parents=True)
    (root / "calendars" / "day.txt").write_text("\n".join(DAYS) + "\n")
    (root / "instruments").mkdir()
    rows = "".join(f"{symbol}\t{DAYS[0]}\t{DAYS[-1]}\n" for symbol in BARS)
    (root / "instruments" / "all.txt").write_text(rows)
    (root / "instruments" / "csi300.txt").write_text(rows)
    for symbol, (closes, factor) in BARS.items():
        features = root / "features" / symbol.lower()
        features.mkdir(parents=True)
        series = {
            "close": closes,
            "open": closes,
            "high": closes,
            "low": closes,
            "volume": [1000.0] * 3,
            "change": [0.0, 0.01, -0.005],
            "factor": [factor] * 3,
        }
        for name, values in series.items():
            # Qlib bin layout: the first float is the calendar index of the first value.
            np.array([0, *values], dtype="<f").tofile(features / f"{name}.day.bin")
    return root


def _call(server, tool, arguments=None):
    async def run():
        async with Client(server) as client:
            return await client.call_tool(tool, arguments or {})

    return asyncio.run(run())


@pytest.fixture
def server(tmp_path):
    return create_server(_write_qlib_dataset(tmp_path / "cn_data"))


def test_tools_are_listed_with_read_only_hints(server):
    async def run():
        async with Client(server) as client:
            return (await client.list_tools()).tools

    tools = {tool.name: tool for tool in asyncio.run(run())}
    assert set(tools) == {"cn_status", "cn_update", "cn_health_check", "cn_get_prices", "cn_list_pool"}
    assert tools["cn_get_prices"].annotations.read_only_hint is True
    assert tools["cn_update"].annotations.read_only_hint is False


def test_get_prices_returns_raw_prices_by_default(server):
    result = _call(server, "cn_get_prices", {"symbols": ["600519"], "start": "2026-07-01", "fields": ["close", "volume"]})

    data = result.structured_content
    assert not result.is_error and data["price_basis"] == "raw"
    first = data["rows"][0]
    assert first["symbol"] == "SH600519" and first["date"] == "2026-07-01"
    assert first["close"] == pytest.approx(1400.0)  # 350 / 0.25
    assert first["volume"] == pytest.approx(250.0)  # 1000 * 0.25
    assert data["total_rows"] == 3 and data["truncated"] is False


def test_get_prices_adjusted_basis_and_row_cap(server):
    result = _call(
        server,
        "cn_get_prices",
        {"symbols": ["SH600519", "600000.SH"], "start": "2026-07-01", "price_basis": "adjusted", "max_rows": 2},
    )

    data = result.structured_content
    assert data["rows"][0]["symbol"] == "SH600000" and data["rows"][0]["close"] == pytest.approx(10.0)
    assert data["total_rows"] == 6 and len(data["rows"]) == 2 and data["truncated"] is True


def test_get_prices_reports_bad_input_as_tool_errors(server):
    bad_field = _call(server, "cn_get_prices", {"symbols": ["600519"], "start": "2026-07-01", "fields": ["pe"]})
    bad_symbol = _call(server, "cn_get_prices", {"symbols": ["AAPL"], "start": "2026-07-01"})

    assert bad_field.is_error and "unknown fields" in bad_field.content[0].text
    assert bad_symbol.is_error and "unrecognized symbol" in bad_symbol.content[0].text


def test_list_pool_status_and_health(server):
    pool = _call(server, "cn_list_pool", {"pool": "csi300"}).structured_content
    status = _call(server, "cn_status", {"offline": True}).structured_content
    health = _call(server, "cn_health_check").structured_content

    assert pool == {"pool": "csi300", "date": "2026-07-03", "count": 2, "symbols": ["SH600000", "SH600519"]}
    assert status["local_latest_date"] == "2026-07-03" and "csi300" in status["pools"]
    assert health["ok"] is True, health


def test_update_installs_then_prices_are_readable(tmp_path):
    staged = _write_qlib_dataset(tmp_path / "release" / "cn_data")
    archive = tmp_path / "qlib_bin.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(staged, arcname="release/cn_data")
    server = create_server(tmp_path / "qlib_data" / "cn_data", source_url=archive.as_uri())

    before = _call(server, "cn_get_prices", {"symbols": ["600519"], "start": "2026-07-01"})
    update = _call(server, "cn_update").structured_content
    after = _call(server, "cn_get_prices", {"symbols": ["600519"], "start": "2026-07-01"}).structured_content

    assert before.is_error and "call cn_update" in before.content[0].text
    assert update["status"] == "installed" and update["latest_date"] == "2026-07-03"
    assert after["total_rows"] == 3


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("600519", "SH600519"), ("sh600519", "SH600519"), ("000001.SZ", "SZ000001"), ("300750", "SZ300750"),
     ("830799", "BJ830799"), ("920001", "BJ920001")],
)
def test_normalize_symbol(raw, expected):
    assert normalize_symbol(raw) == expected


def test_empty_data_dir_variable_means_the_default(monkeypatch):
    monkeypatch.setenv(mcp_server.DATA_DIR_ENV, "")
    seen = {}
    monkeypatch.setattr(mcp_server, "QlibReader", lambda path: seen.setdefault("dir", path))

    create_server()

    assert seen["dir"] == mcp_server.market.DEFAULT_DATA_DIR.resolve()
