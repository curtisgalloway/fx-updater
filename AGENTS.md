<!-- SPDX-FileCopyrightText: 2026 Curtis Galloway -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# fx-updater — agent notes

Keeps a Fuchsia checkout updated (`jiri update`) and built (`fx build`)
unattended. Extracted from a private CI project's daily update script; the
guardrails and their docstrings carry the reasoning, and that reasoning is
the value, so keep it with the code when refactoring.

## Layout

- `fx_updater/guard.py` — WIP guard, mode-only-dirt restore, disk floor.
- `fx_updater/jiri.py` — update-failure classification, revisions.
- `fx_updater/build.py` — `fx build` with the stale-`build.ninja` regen.
- `fx_updater/status.py` — state dir, lock, status document, `status` lines.
- `fx_updater/config.py` — the TOML config file `install` writes and `run` reads.
- `fx_updater/systemd.py` — unit rendering, install/uninstall; every
  systemd call goes through a `Runner` seam that tests fake.
- `fx_updater/promfile.py` — the opt-in `fx_updater.prom` gauges.
- `fx_updater/cli.py` — `run`, `status`, `install`, `uninstall`; `--skill`
  prints `fx_updater/SKILL.md`.

## Checks

```bash
uv run pytest -q
uvx pylint fx_updater tests
uvx pyink --check fx_updater tests
```

## Rules

- **This repo is written to go public.** No lab hostnames, IP addresses,
  home-directory paths or usernames anywhere, including test fixtures and
  comments. Grep before committing.
- **Exit codes** follow the portable contract (0/1, 2-9 deterministic, 20s
  transient, 100+ tool-specific); the table is `_EXIT_EPILOG` in `cli.py`
  and is repeated in `SKILL.md`. Keep the two in step. Tool-specific codes:
  `100` = `build_failed`. Never propagate jiri's or fx's return code; record
  it in the status document.
- **Tests never read the host's real config or state.** Every test that runs
  the CLI sets `XDG_CONFIG_HOME` and `XDG_STATE_HOME` to a temp dir.
- Commit identity: `Curtis Galloway <4055365+curtisgalloway@users.noreply.github.com>`.
