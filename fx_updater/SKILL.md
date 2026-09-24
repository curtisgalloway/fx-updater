---
name: fx-updater
description: Keep a Fuchsia checkout updated (jiri update) and built (fx build) unattended, with guardrails that never touch work in progress. Use to schedule, run, or diagnose a Fuchsia tree update, or to check whether the last one worked.
---
<!-- SPDX-FileCopyrightText: 2026 Curtis Galloway -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# fx-updater {{VERSION}}

Runs `jiri update` and then `fx build` for one or more build dirs in a Fuchsia
checkout, so the first interactive build of the day is warm. It is built to
run with nobody watching: every decision errs toward leaving the tree alone.

## Invocation

```bash
fx-updater install --fuchsia-dir DIR [--build-dir NAME ...] [--schedule CAL] [--prom-dir DIR] [--dry-run]
fx-updater status [--json]
fx-updater run [--fuchsia-dir DIR] [--build-dir NAME ...] [--no-update] [--force-update]
fx-updater uninstall [--dry-run]
fx-updater --skill       # print this document
```

- `install` writes `$XDG_CONFIG_HOME/fx-updater/config.toml` (flags merged
  over any existing file) and a systemd user `fx-updater.service` + `.timer`,
  then enables the timer. `--schedule` is systemd `OnCalendar` syntax
  (default `*-*-* 05:30:00`). Re-running converges; lines say `write` or
  `unchanged`. `--unit-name` picks another unit name.
- `run` takes any setting not given as a flag from the config file.
- `uninstall` disables the timer and removes the two units. It keeps the
  config and lets a run in progress finish.
- Both mutating commands refuse to overwrite or delete a unit file of the same
  name that fx-updater did not write (exit 3).

## Exit status

| Code | Meaning |
|---|---|
| 0 | `ok`, or `skipped_wip` (uncommitted work; nothing touched) |
| 1 | `status`: no run has happened yet (`never_run`) |
| 2 | usage error, including an invalid flag value (a build dir that is a path, a negative floor, a schedule systemd rejects) |
| 3 | missing precondition: jiri/fx, config, build dir, systemctl or systemd-analyze; below the disk floor (`skipped_disk`); unreadable status document; a foreign unit in the way |
| 20 | nothing done, retry is free: lock held; `status_failed` (`jiri status` itself failed); a `systemctl` call failed before install/uninstall changed any file |
| 21 | partially done: `update_failed` (the tree may be part-updated), or install/uninstall wrote or removed a file (unit or config) and then a `systemctl` call failed |
| 100 | `build_failed`: the tree is updated and a build broke |

jiri's and fx's own codes are in the status document (`update_rc`,
`builds[].build_exit`), never the exit status.

## Status

`fx-updater status` prints one line, `<outcome> <timestamp>: <reason>`, plus a
`lock:` line while a lock exists. `--json` prints one document:

```json
{"outcome": "ok", "empty": false, "status_file": "...", "last_run": {...}, "lock": null}
```

Outcomes: `ok`, `skipped_wip`, `skipped_disk`, `status_failed`,
`update_failed`, `build_failed`, and `never_run` (no run yet; `empty: true`).
If the status file exists but cannot be read, the document is
`{"outcome": null, "empty": false, "error": "..."}` with exit 3.

Branch on `outcome` and `empty`, not on the exit status. `last_run` is the
full status document from `$XDG_STATE_HOME/fx-updater/status.json`, with the
log path in `last_run.log`. `lock.pid_alive` is `false` when a killed run left
the lock behind.

## Metrics (opt-in)

With `--prom-dir DIR` (or `prom_dir` in the config), every run that reaches
an outcome rewrites `DIR/fx_updater.prom` for a Prometheus textfile
collector. A run refused earlier (lock held, jiri/fx or config missing)
leaves the previous file, so alert on the age of the timestamp too:
`fx_updater_last_run_timestamp_seconds`, `_last_run_ok`, `_last_run_skipped`,
`_last_run_outcome{outcome}`, `_commits_pulled`,
`_build_seconds/_build_exit/_build_regen{build_dir}`, `_disk_free_bytes`.
Without it, no `.prom` file is written anywhere.

## Notes for an agent

- A `skipped_wip` exit 0 is by design. Read `last_run.wip` to see what is
  uncommitted; do not pass `--force-update` over someone's work.
- Files whose only change is the mode bit (identical content) are restored
  automatically. That rewrites a permission bit and never content.
- A stale lock (`status` says `NOT running`) blocks every later run with exit
  20 until it is removed. Confirm no `fx-updater` process is running first.
- If `install` warns that linger is off, the timer fires only while the user
  is logged in.
- The unit runs the Python interpreter that ran `install`. After
  reinstalling or moving the tool, run `install` again. `install` rewrites
  the whole config file from its values: comments added by hand are lost.
- `run --fuchsia-dir` for a tree other than the config's ignores the default
  config entirely (build dirs, floor, prom dir).
