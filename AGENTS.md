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
- `fx_updater/status.py` — state dir, lock, status document.
- `fx_updater/cli.py` — `run`; `--skill` prints `fx_updater/SKILL.md`.

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
- **Exit codes** currently match the original script (see `cli.py`). They
  move to the `cli-conventions` contract when `status`/`install` land; record
  any tool-specific code (100-124) here when it is introduced.
- Commit identity: `Curtis Galloway <4055365+curtisgalloway@users.noreply.github.com>`.
