<!-- SPDX-FileCopyrightText: 2026 Curtis Galloway -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# fx-updater

Keep a Fuchsia checkout updated and built on a schedule, so the first build
of your day is warm, without ever running an update over your uncommitted
work.

**Status:** early. `fx-updater run` works; scheduling (`install`) and
`status` are not written yet.

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

`fx-updater --skill` prints the agent-facing usage document.

## License

Apache License 2.0; see [LICENSE](LICENSE).
