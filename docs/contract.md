<!-- SPDX-FileCopyrightText: 2026 Curtis Galloway -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# The consumer contract

**Terms.** A **consumer** is any program that uses the tree fx-updater
maintains: a boot test that builds and flashes an image, a nightly scheduler,
a metrics script. A **run** is one `fx-updater run`. The **lock** tells a
consumer that a run is using the tree. The **status document** records what
the last run did. The **hook** is a command a run starts after it finishes.
`jiri` is Fuchsia's multi-repo checkout tool; `fx` is its build front end.

This page lists what fx-updater promises to consumers. The code behind it is
`fx_updater/contract.py`. A consumer should import that module and nothing
else from fx-updater. If a change would break a consumer written against
schema version N, it raises `SCHEMA_VERSION` to N+1 and changes this page in
the same commit.

Current schema version: **1**.

## The lock

| | |
|---|---|
| Path | `$XDG_STATE_HOME/fx-updater/lock` (default `~/.local/state/fx-updater/lock`) |
| Held means | a process has an exclusive `flock(2)` on the file |
| Held while | the run is doing anything at all: disk check, WIP check, `jiri update`, every build, **and the hook** |
| Ask | `contract.tree_busy()`; block with `contract.wait_until_idle(timeout_secs)` |

**Existence means nothing.** The file is created once and never removed. A
consumer that checks `path.exists()` will think the tree is busy forever. Use
`tree_busy()`, which takes a shared lock for a moment without blocking.

**A run that dies holds nothing.** The kernel drops a flock when the process
holding it exits, whether it exits cleanly, is killed with SIGKILL, or loses
power. So a killed run cannot leave behind a lock that blocks the next run
or keeps a consumer waiting. This is the stale-lock policy: no lock can go
stale, so there is nothing to detect and nothing to break by hand. It
replaces M2's lock, which was a file created with `O_EXCL` whose existence
was the signal, and which a killed run left in place.

**Children do not hold it.** `jiri`, `fx`, and the hook do not inherit the
lock's file descriptor. A daemon that a build leaves running (a build server,
say) cannot keep the tree looking busy after the run ends.

**One lock per account.** The lock is not per tree. Two trees under one
account share it, which means their runs take turns.

**Local filesystems only.** flock on NFS varies by client. Keep
`$XDG_STATE_HOME` on a local disk.

A consumer's probe holds a shared lock for a moment. If a run starts at that
exact moment, it retries for up to a second before it reports the lock as
held (exit 20).

The file holds the pid of its last holder, for `fx-updater status`. A clean
release empties it; a killed run leaves its dead pid behind. Neither means
anything: only the flock says whether the lock is held. The file is created
0600 (another account able to open it could hold a shared lock forever and
refuse every run) and is never opened through a symlink. Consumers should
not read the pid.

## The status document

Written to `$XDG_STATE_HOME/fx-updater/status.json` (or to `run --status-file`)
after every outcome. It is replaced atomically, so a reader never sees half a
document. Read it with `contract.read_status()`, and check it with
`contract.validate_status(doc)`, which returns a list of problems (`[]` means
it conforms).

A run writes the document **twice** when a hook is set: first when the
outcome is known, before the hook starts (so the hook can read it), and again
with `hook` added once the hook has exited. Both writes happen while the run
still holds the lock. So a consumer that waits for the lock to be free
always reads the final version.

### Fields

Every document has these:

| Key | Type | Meaning |
|---|---|---|
| `schema_version` | int | `1` |
| `timestamp` | string | when the run started, local ISO 8601 to the second |
| `outcome` | string | `ok`, `skipped_wip`, `skipped_disk`, `status_failed`, `update_failed`, or `build_failed` |
| `reason` | string | one line for a person |
| `log` | string | path of this run's log. It also contains the hook's output |
| `fuchsia_dir` | string | absolute path of the tree |

These appear depending on how far the run got:

| Key | Type | Present when |
|---|---|---|
| `disk_free_gb` | number | `skipped_disk`, `ok`, `build_failed` |
| `wip` | string | `skipped_wip`: what `jiri status` reported |
| `detail` | string | `status_failed`: the end of `jiri status`'s output |
| `update_rc`, `update_secs` | int, number | `update_failed` (`update_secs` also on `ok`/`build_failed`) |
| `integration_pre`, `integration_post` | string | `integration.git` HEAD before and after the update: both on `ok`/`build_failed`, `_pre` only on `update_failed` |
| `tree_pre`, `tree_post` | string | HEAD of the root `fuchsia.git`, before and after: likewise |
| `commits_pulled` | int | `ok`, `build_failed`: commits from pre to post in `integration.git` (`-1` if git could not count them) |
| `builds` | list | `ok`, `build_failed`: one object per build dir, in order |
| `hook` | object | a hook was set and has finished |

A revision is `"unknown"` when git could not read it.

`builds[]` objects: `build_dir` (string, a name under `out/`), `build_start`
(number, epoch seconds), `build_secs` (number), `build_exit` (int, fx's exit
code), `regen` (string or null: the stale `build.ninja` input that caused
one `fx gen` and a retry).

`hook`: `argv` (list of strings), `exit` (int, or null when the hook never
started or was killed), `secs` (number), and `error` (string, only when
`exit` is null).

**Readers must ignore keys they do not know.** Version 1 may gain optional
keys. If a key is removed, renamed, becomes required, or changes meaning,
the version goes up.

## The hook

Set it with `run --hook CMD` or `post_build_hook` in the config, or through
`install --hook CMD`, which saves it to the config. The command is split the
way a POSIX shell splits words (`shlex.split`). It is **not** run through a
shell, so pipes and `&&` do nothing unless you write `sh -c '...'` yourself.

- **When:** once per run, after any outcome the run reports, including
  skips and failures. A run that stops before it has an outcome does not
  start the hook: lock held, jiri/fx missing, config broken, or no build
  dir. The hook reads `FX_UPDATER_OUTCOME` to decide what to do.
- **Under the lock**, with the tree's root as its working directory and
  stdin closed. Its stdout and stderr go to the run's log.
- **Time limit:** 900 s. After that it is killed and `hook.error` says so. A
  hook that never finishes would otherwise hold the lock, and every consumer
  would wait on it.
- **It cannot change the run's result.** The hook's exit status goes into
  `hook.exit` and, with `--prom-dir`, into the `fx_updater_hook_ok` gauge.
  The run's outcome and fx-updater's own exit status stay the same whatever
  the hook does.

Environment, in addition to the run's own:

| Variable | Value |
|---|---|
| `FX_UPDATER_SCHEMA_VERSION` | `1` |
| `FX_UPDATER_STATUS_FILE` | path of the status document; the variables below repeat its fields, except the two marked environment-only |
| `FX_UPDATER_OUTCOME` | the outcome |
| `FX_UPDATER_FUCHSIA_DIR` | the tree |
| `FX_UPDATER_BUILD_DIRS` | environment-only: the build dirs the run was set to build, space-separated names under `out/`, set for every outcome (a name cannot contain whitespace or `/`; a `.fx-build-dir` that names one is refused) |
| `FX_UPDATER_RUN_START` | environment-only: epoch seconds when the run started (`timestamp` is the same instant, to the second). Each build's own start is `builds[].build_start` |
| `FX_UPDATER_LOG` | the run's log |
| `FX_UPDATER_INTEGRATION_PRE`, `_POST` | as in the document, `""` where the document has no such key |
| `FX_UPDATER_TREE_PRE`, `_POST` | as in the document, `""` likewise |

## Example: waiting before a build

```python
from fx_updater import contract

if not contract.wait_until_idle(timeout_secs=2 * 3600):
    raise SystemExit("fx-updater is still running; try later")
doc = contract.read_status()          # None if fx-updater has never run
if doc and doc["outcome"] == "ok":
    built_at = doc["integration_post"]
```
