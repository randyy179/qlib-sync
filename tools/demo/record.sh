#!/usr/bin/env bash
# Record a demo GIF from a tape in this folder, in an isolated HOME so real data is untouched.
#   tools/demo/record.sh update <demo-home>
# The demo home must already hold an older release; see tools/demo/README.md.
set -euo pipefail
tape="$1"; demo_home="$(cd "$2" && pwd)"
root="$(cd "$(dirname "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
sed -e "s#@DEMO_HOME@#${demo_home}#" -e "s#@DEMO_PATH@#${root}/.venv/bin:/usr/bin:/bin#" \
  "$root/tools/demo/$tape.tape" > "$work/$tape.tape"
cd "$root" && vhs "$work/$tape.tape"
