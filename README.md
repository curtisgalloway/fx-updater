<!-- SPDX-FileCopyrightText: 2026 Curtis Galloway -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# fx-updater

Keep a Fuchsia checkout updated and built on a schedule, so the first build
of your day is warm, without ever running an update over your uncommitted
work.

**Status:** early. `run`, `install`, `uninstall` and `status` work on Linux
with systemd. The newcomer quickstart is still to be written.

**Terms**

- **jiri** — the tool that fetches and updates the many git repositories in a
  Fuchsia checkout (`jiri update`).
- **fx** — Fuchsia's developer command (`fx build`, `fx gen`).
- **build dir** — an output directory under `out/`, such as `out/core.x64`,
  configured by `fx set`. `.fx-build-dir` names the default one.

## Run it once

```bash
uv tool install git+<this repo's URL>
cd /path/to/fuchsia
fx-updater run
```

That runs `jiri update` and then `fx build` for the default build dir. Use
`--build-dir NAME` (repeatable) to pick others, and `--no-update` to build
only.

## Schedule it

```bash
fx-updater install --fuchsia-dir /path/to/fuchsia --build-dir core.x64
fx-updater status
```

`install` writes `~/.config/fx-updater/config.toml` and a systemd user timer
(`fx-updater.timer`, daily at 05:30; change it with `--schedule`, in systemd
`OnCalendar` syntax). Add `--dry-run` to see what it would do first. For the
timer to fire while you are logged out, enable linger once:
`loginctl enable-linger $USER`. `fx-updater uninstall` removes the timer and
keeps the config.

## What it will not do

- **Update over work in progress.** If `jiri status` shows anything
  uncommitted, the run is skipped (exit 0) and the status document lists what
  it saw. `--force-update` overrides.
- **Fill the disk.** Below `--min-free-gb` (default 100) it touches nothing.
- **Retry a real failure.** Network-shaped `jiri update` failures are retried;
  conflicts are not. A build that fails because upstream deleted a file the
  build graph recorded gets one `fx gen` and one more try; any other build
  failure stands.

One thing it does fix: files whose content is unchanged but whose mode bit
flipped (seen on fresh checkouts) are restored, since `jiri update` refuses
any project with uncommitted changes. Only the permission bit is rewritten.

## Where results go

`$XDG_STATE_HOME/fx-updater/` (default `~/.local/state/fx-updater/`):
`status.json` holds the last outcome, `logs/` one log per run, and `lock`
exists while a run is in progress.

`fx-updater status` prints the last outcome in one line (`--json` for the whole
document). `--prom-dir DIR` (or `prom_dir` in the config, which `install
--prom-dir` sets) also writes `fx_updater.prom` there for a Prometheus textfile
collector; with neither, nothing is written.

`fx-updater --skill` prints the agent-facing usage document, including the
exit codes. `fx-updater --help` lists them too.

## License

Apache License 2.0; see [LICENSE](LICENSE).
