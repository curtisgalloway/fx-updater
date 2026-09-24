---
name: fx-updater
description: Keep a Fuchsia checkout updated (jiri update) and built (fx build) unattended, with guardrails that never touch work in progress. Use to run or diagnose a scheduled Fuchsia tree update.
---
<!-- SPDX-FileCopyrightText: 2026 Curtis Galloway -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# fx-updater {{VERSION}}

Runs `jiri update` and then `fx build` for one or more build dirs in a Fuchsia
checkout, so the first interactive build of the day is warm. It is built to
run with nobody watching: every decision errs toward leaving the tree alone.

## Invocation

```bash
fx-updater run [--fuchsia-dir DIR] [--build-dir NAME ...] [--no-update] [--force-update]
fx-updater --skill       # print this document
```

## What stops a run

| Outcome | Meaning | Exit |
|---|---|---|
| `ok` | updated and built | 0 |
| `skipped_wip` | uncommitted work in the checkout; nothing touched | 0 |
| `skipped_wip` | `jiri status` itself failed; nothing touched | 4 |
| `skipped_disk` | below `--min-free-gb`; nothing touched | 3 |
| `update_failed` | `jiri update` failed (network failures are retried first) | jiri's code |
| `build_failed` | `fx build` failed | fx's code |
| — | another run holds the lock | 2 |
| — | `jiri`/`fx` missing, or no build dir | 1 |

The last outcome is in `$XDG_STATE_HOME/fx-updater/status.json`, and the full
log path is in it.

## Notes for an agent

- A `skipped_wip` exit 0 is by design. Read the `wip` field of the status
  document to see what is uncommitted; do not pass `--force-update` over
  someone's work.
- Files whose only change is the mode bit (identical content) are restored
  automatically. That rewrites a permission bit and never content.
- A leftover `lock` file after a killed run blocks every later run until it
  is removed. Check no `fx-updater` process is running before removing it.
