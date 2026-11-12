# Demo recordings

The GIFs in `docs/assets/` are recorded with [vhs](https://github.com/charmbracelet/vhs)
against real releases, in a throwaway HOME so your own data is untouched.

```bash
demo=$(mktemp -d)
# Install an older release so the recording shows a real update to the latest one.
HOME=$demo .venv/bin/qlib-sync cn update \
  --source-url https://github.com/chenditc/investment_data/releases/download/<older-tag>/qlib_bin.tar.gz
tools/demo/record.sh update "$demo"
```

`python tools/make_flow_diagram.py` regenerates the flow diagrams.

The Claude demo runs Claude Code against `qlib-sync mcp` with a one-off `--mcp-config`, so
your own Claude configuration is not changed. Open the demo folder with `claude` once to
trust it, then:

```bash
tools/demo/record-claude.sh <demo-dir>
```

The script crops Claude Code's start banner (version, plan, folder path) out of the GIF.
Check every frame before publishing a new recording.
