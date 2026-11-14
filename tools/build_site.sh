#!/usr/bin/env bash
# Assemble the landing page and its images into an output folder (default: _site).
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-$root/_site}"
rm -rf "$out" && mkdir -p "$out/assets"
cp "$root/site/index.html" "$out/"
cp "$root"/docs/assets/*.mp4 "$root"/docs/assets/*-poster.jpg "$root"/docs/assets/comic.jpg "$root"/docs/assets/comic-en.jpg "$root"/docs/assets/*.svg "$out/assets/"
echo "$out"
