<!-- SPDX-FileCopyrightText: 2026 Curtis Galloway -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Upstream bug reports, drafted for a person to file

Every defect fx-updater runs into in code it does not own (jiri, `fx`, the
Fuchsia tree) gets one file here.

**Agents never file.** They draft, keep the evidence current, and record what
filing would still take. A person decides when and whether to file, files, and
updates the Status line and this table with the upstream URL.

## Layout

- `bugs/<project>/NNN-<slug>.md`: one report, numbered per project.
- `bugs/<project>/NNN-<slug>.patch`: the fix, if one is written, beside the
  report, against the revision its header names.

Each report opens with an HTML comment header in this order: **Where to file**,
**Status**, **Tracking**, **Fix**, **Evidence**, **To file** (what is missing
before it is ready). Status is one of `TRIAGE` (may not be a bug), `DRAFT — not
ready`, `DRAFT — ready to file`, `FILED <url>`, or `WITHDRAWN <reason>`.
This repo is public, so the header follows the same rule as the body: no host
names, addresses, usernames, or local paths.

## Index

| # | Bug | Where | Status |
|---|---|---|---|
| fuchsia/001 | `jiri update` fails on internal CIPD packages when any worktree is registered: other workspaces' packages skip the ACL filter, and the package cache is now always on | issues.fuchsia.dev (Tools > Jiri) | DRAFT — ready to file (cause confirmed 2026-10-14: removing the worktree fixes the update) |
