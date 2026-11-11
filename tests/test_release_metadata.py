"""Release metadata lives in several files; a mismatch breaks installs or registry publishing."""

import json
import re
from pathlib import Path

import pytest

import qlib_sync

tomllib = pytest.importorskip("tomllib")  # Python 3.11+

ROOT = Path(__file__).resolve().parents[1]
SERVER_NAME = "io.github.randyy179/qlib-sync"


def _pyproject(path: Path) -> dict:
    return tomllib.loads(path.read_text())["project"]


def test_versions_agree_across_package_bundle_and_registry():
    version = _pyproject(ROOT / "pyproject.toml")["version"]
    manifest = json.loads((ROOT / "mcpb" / "manifest.json").read_text())
    server = json.loads((ROOT / "server.json").read_text())

    assert qlib_sync.__version__ == version
    assert manifest["version"] == version
    assert f"qlib-sync=={version}" in _pyproject(ROOT / "mcpb" / "pyproject.toml")["dependencies"]
    assert server["version"] == version and server["packages"][0]["version"] == version


def test_python_bounds_agree():
    bound = _pyproject(ROOT / "pyproject.toml")["requires-python"]
    manifest = json.loads((ROOT / "mcpb" / "manifest.json").read_text())

    assert _pyproject(ROOT / "mcpb" / "pyproject.toml")["requires-python"] == bound
    assert manifest["compatibility"]["runtimes"]["python"] == bound


def test_readme_carries_the_registry_ownership_marker():
    # The MCP registry verifies PyPI ownership by finding this marker in the package README.
    server = json.loads((ROOT / "server.json").read_text())
    assert server["name"] == SERVER_NAME
    assert re.search(rf"mcp-name: {re.escape(SERVER_NAME)}\s", (ROOT / "README.md").read_text())
