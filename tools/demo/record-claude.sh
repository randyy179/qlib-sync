#!/usr/bin/env bash
# Record docs/assets/demo-claude.gif with Claude Code talking to `qlib-sync mcp`.
#   tools/demo/record-claude.sh <demo-dir> [crop-top-px]
# The demo dir must already be trusted by Claude Code (open it once with `claude`).
set -euo pipefail
demo_dir="$(cd "$1" && pwd)"; crop_top="${2:-180}"
root="$(cd "$(dirname "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
printf '{"mcpServers": {"qlib-sync": {"command": "%s", "args": ["mcp"]}}}\n' "$root/.venv/bin/qlib-sync" > "$work/mcp.json"
sed -e "s#@DEMO_DIR@#${demo_dir}#" -e "s#@MCP_CONFIG@#$work/mcp.json#" -e "s#@RAW_GIF@#$work/raw.gif#" \
  "$root/tools/demo/claude.tape" > "$work/claude.tape"
cd "$root" && vhs "$work/claude.tape"
# Drop the banner rows at the top; regenerate the palette so colors stay clean.
ffmpeg -v error -y -i "$work/raw.gif" -t "${DEMO_SECONDS:-27}" -filter_complex \
  "[0:v]crop=in_w:in_h-${crop_top}:0:${crop_top},split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=none" \
  "$root/docs/assets/demo-claude.gif"
cp "$work/raw.gif" "${RAW_COPY:-/dev/null}" 2>/dev/null || true
