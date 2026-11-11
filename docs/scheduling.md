# Daily updates

The community release appears once a day at an irregular time, observed between about 16:00
and 01:00 China time (UTC+8), occasionally on weekends. An update that finds nothing new
costs one small request, so run it on a fixed schedule instead of chasing the release time.
08:00 China time reliably picks up the previous evening's release; a second evening run
picks it up sooner.

Find the full path of the command first, since schedulers do not load your shell profile:

```bash
which qlib-sync
```

Add `--mirror https://ghfast.top` (or another mirror) to the commands below if GitHub is
slow from your network.

## cron (Linux, or macOS on a machine that stays awake)

`crontab -e`, then add a line. cron uses the machine's local time zone; the times below
assume it is China time.

```cron
0 8,22 * * * /path/to/qlib-sync cn update >> "$HOME/.qlib/qlib-sync.log" 2>&1
```

If your machine runs in another time zone, convert: 08:00 China time is 00:00 UTC.

## launchd (macOS)

launchd runs a missed job when the Mac wakes up, which suits laptops. Save as
`~/Library/LaunchAgents/io.github.qlib-sync.plist`, replacing the program path and `USER`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>io.github.qlib-sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/qlib-sync</string>
    <string>cn</string>
    <string>update</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>22</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>StandardOutPath</key>
  <string>/Users/USER/.qlib/qlib-sync.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/USER/.qlib/qlib-sync.log</string>
</dict>
</plist>
```

Load it, and run it once to check:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.github.qlib-sync.plist
launchctl kickstart gui/$(id -u)/io.github.qlib-sync
tail -n 20 ~/.qlib/qlib-sync.log
```

`StartCalendarInterval` uses the Mac's local time zone.

To remove it:

```bash
launchctl bootout gui/$(id -u)/io.github.qlib-sync
rm ~/Library/LaunchAgents/io.github.qlib-sync.plist
```

## Reading the log

Each run ends with a summary: `Already up to date`, `Updated CN data from ... to ...` with new
trading days and symbol changes, or an `[ERROR]` line. A failed run leaves the existing data
unchanged; the next scheduled run tries again.
