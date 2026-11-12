"""One command-line entry point for the local Qlib datasets."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from .cn.update import tilde

DEFAULT_QLIB_DATA_DIR = Path("~/.qlib/qlib_data").expanduser()

USAGE = """usage: qlib-sync <command> [options]

commands:
  status              Latest date and size of every local dataset
  cn <subcommand>     China A-share daily data: status, update, health-check, restore
  mcp                 Serve the A-share data to AI assistants over MCP (stdio)

Run `qlib-sync cn --help` for details.
"""


def read_last_line(path: Path) -> str | None:
    if not path.is_file():
        return None
    lines = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    return lines[-1] if lines else None


def dataset_overview(base_dir: Path = DEFAULT_QLIB_DATA_DIR) -> list[dict[str, str]]:
    rows = []
    for dataset in sorted(path for path in base_dir.iterdir() if path.is_dir()) if base_dir.is_dir() else []:
        # Qlib keeps planned trading days in `*_future.txt`; they are not data.
        calendars = sorted(p for p in (dataset / "calendars").glob("*.txt") if not p.stem.endswith("_future"))
        if not calendars:
            continue
        latest = max(filter(None, (read_last_line(path) for path in calendars)), default="missing")
        freqs = ",".join(path.stem for path in calendars)
        rows.append({"name": dataset.name, "freq": freqs, "latest": latest})
    return rows


def print_overview(base_dir: Path = DEFAULT_QLIB_DATA_DIR) -> None:
    rows = dataset_overview(base_dir)
    if not rows:
        print(f"no Qlib datasets found under {tilde(base_dir)}")
        return
    width = max(len(row["name"]) for row in rows)
    print(f"Qlib datasets under {tilde(base_dir)}")
    for row in rows:
        print(f"  {row['name']:<{width}}  latest={row['latest']}  freq={row['freq']}")


def run_module_main(module_name: str, prog: str, argv: list[str]) -> None:
    # The dataset modules own their argparse parsers; hand them the remaining arguments.
    sys.argv = [prog, *argv]
    importlib.import_module(module_name).main()


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return
    command, rest = argv[0], argv[1:]
    if command == "status":
        print_overview()
    elif command == "cn":
        run_module_main("qlib_sync.cn.market", "qlib-sync cn", rest)
    elif command == "mcp":
        from .mcp_server import main as mcp_main  # pylint: disable=import-outside-toplevel

        mcp_main(rest)
    else:
        print(USAGE, file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
