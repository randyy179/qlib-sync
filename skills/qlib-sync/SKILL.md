---
name: qlib-sync
description: Install, update, check, and read the local Qlib China A-share daily dataset with the qlib-sync tool. Use when the user asks to update or download A-share (中国A股) data for Qlib, asks how fresh their local stock data is, hits errors with ~/.qlib/qlib_data/cn_data, wants to schedule daily updates, or asks about A-share prices or index constituents that local data can answer.
---

# qlib-sync

qlib-sync keeps `~/.qlib/qlib_data/cn_data` (Qlib's default A-share location) in step with the
daily community release from chenditc/investment_data. Every update is transactional: the
release is pinned by tag, checked with the dataset maintainers' own `validate_archive.py
--require-publishable` (an unmodified copy ships with qlib-sync), staged, swapped in, and checked
with a real Qlib read. A release that cannot be verified is refused, and on any failure the
previous data is restored, so running an update is always safe.

## Pick the interface

- **MCP tools available** (`cn_status`, `cn_update`, `cn_get_prices`, ...): use them directly.
- **Otherwise** use the CLI. If `qlib-sync` is not installed, `uvx --python 3.12 qlib-sync ...` runs it
  without installing; `pip install qlib-sync` installs it. Confirm before installing anything.

## Update the data

1. Check first: `qlib-sync cn status`. It prints the local latest date and whether the release
   is newer. This is one small request and never downloads the package.
2. Update: `qlib-sync cn update`. When already current it exits in about a second. Otherwise it
   downloads about 550 MB and finishes in roughly a minute on a good connection.
3. Report from the summary it prints: the new latest date, how many trading days were added,
   and new or delisted symbols. Add `--json` when you need to parse the result.

The release appears once a day at an irregular time, observed between about 16:00 and 01:00
China time, occasionally on weekends. "No update available" during the day is normal; data is
end-of-day only.

## When something fails

Read the `[ERROR]` line. The existing data is unchanged in every failure case.

| Message contains | Meaning | What to do |
|---|---|---|
| `could not download` | Network problem reaching GitHub | Retry with a mirror: `qlib-sync cn update --mirror https://ghfast.top` (or set `QLIB_SYNC_MIRROR`). Common in mainland China. |
| `refusing to install unverified data` | The release manifest could not be read or was rejected | Usually a network problem: retry, or add `--mirror`. If the message names `invalid-manifest`, upstream published something its own validator rejects; wait for the next release. |
| `upstream validator (archive-identity-mismatch)` | Corrupt or tampered download; it was discarded | Run the update again. If it repeats, the release itself may be broken; wait for the next one. |
| `upstream validator (` any other reason | The package fails the maintainers' own checks | Upstream problem. Wait and retry later; do not use `--force` or `--allow-unverified`. |
| `refusing downgrade` | The release is older than the local data | Nothing to do; local data is newer. |
| `already running` | Another update holds the lock | Wait for it. If none is running (a crash left the lock), remove `~/.qlib/qlib_data/.cn_data_update.lock` after confirming with the user. |
| `not a Qlib daily dataset` | The target folder has other content | Ask the user; never delete it yourself. Use `--data-dir` to install elsewhere. |

After any repair, run `qlib-sync cn health-check`; it ends with `passed` or lists errors.

## Things to tell the user

- The update replaces the whole `cn_data` folder. Files the user added there are lost unless
  passed with `--protect FILE` (custom stock pools in `instruments/`). Mention this before the
  first update if they have customized the folder.
- Only suggest `--allow-unverified` if the user explicitly accepts installing data nobody has
  checked; it never bypasses a checksum mismatch, and the result is marked unverified.
- `--keep-backup` keeps the previous dataset for one rollback; `qlib-sync cn restore --list`
  shows backups and `restore --yes` switches back. Restoring replaces the active data, so
  confirm with the user first.

## Reading prices

Qlib stores adjusted prices: real price = `$close / $factor`. The MCP tool `cn_get_prices`
returns real prices by default and adjusted prices with `price_basis="adjusted"`. Use real
prices for questions like "what did Moutai close at"; use adjusted prices for returns over
periods that span splits or dividends. `change` is the daily return as a fraction.

Symbols: `600519`, `SH600519`, and `600519.SH` all work. Shanghai codes start with 6,
Shenzhen with 0 or 3, Beijing with 4, 8, or 92.

## Daily automatic updates

An update that finds nothing new costs one small request, so run it daily rather than trying
to hit the release time. 08:00 China time catches the previous evening's release reliably;
a second run in the evening picks it up sooner. cron (machine clock in China time):

```cron
0 8,22 * * * /path/to/venv/bin/qlib-sync cn update >> ~/.qlib/qlib-sync.log 2>&1
```

On macOS, launchd is more reliable for laptops; see `docs/scheduling.md` in the qlib-sync
repository for a ready-made plist. Confirm with the user before installing any schedule.
