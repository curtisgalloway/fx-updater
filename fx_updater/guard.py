# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
"""Guardrails that decide whether an unattended run may touch the tree.

Every decision here errs toward doing nothing. An unparseable `jiri status`
means "skip", a project that cannot be proven clean means "leave it alone",
and the disk floor is checked before anything is written.

- Work in progress wins. Before updating, `jiri status -check-head=false` is
  consulted; ANY output (tracked/untracked changes, unmerged local commits)
  skips the whole run and reports, rather than trusting jiri to preserve the
  work. Being off JIRI_HEAD is not WIP - that is just "needs the update".
- The one exception is mode-only dirt: files whose content matches the index
  exactly and whose mode alone differs. Those are restored, not skipped (see
  `restore_modes` for why restoring is required and why it is safe).
- Disk floor. The run aborts before touching anything if the tree's
  filesystem has less free space than the floor.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

# jiri's own log lines, e.g. "[05:30:36.168] WARN: Found 1 deleted project(s),
# run with -d flag to list them." They are diagnostics about the manifest (a
# project upstream dropped, still on disk; `jiri update` leaves it alone), not
# status rows about the user's work, and must not trip the WIP guard: one such
# line skipped a whole unattended update on 2026-08-29.
_JIRI_LOG_RE = re.compile(r"^\[\d\d:\d\d:\d\d(?:\.\d+)?\] (?:WARN|INFO|ERROR):")


def wip_summary(status_output: str, limit: int = 20) -> str:
    """Compress `jiri status` output to a report; "" means the tree is clean."""
    lines = [
        ln.rstrip()
        for ln in status_output.splitlines()
        if ln.strip() and not _JIRI_LOG_RE.match(ln)
    ]
    if not lines:
        return ""
    if len(lines) > limit:
        omitted = len(lines) - limit
        lines = lines[:limit] + [f"... ({omitted} more lines)"]
    return "\n".join(lines)


# A jiri status project header: the project's path at column 0, ending in a
# colon ("." for the root project). `Branch: DETACHED-HEAD(...)` also starts
# at column 0 and contains a colon, but does not *end* with one, which is what
# separates the two.
_JIRI_PROJECT_RE = re.compile(r"^(\S.*?):\s*$")


def jiri_status_projects(status_output: str) -> list[str]:
    """Project paths, relative to the tree root, that `jiri status` reported."""
    return [
        m.group(1)
        for m in (
            _JIRI_PROJECT_RE.match(ln)
            for ln in status_output.splitlines()
            if not _JIRI_LOG_RE.match(ln)
        )
        if m
    ]


def _git_out(project_dir: pathlib.Path, args: list[str]) -> str | None:
    """`git -C project_dir ...` stdout, or None if git failed."""
    done = subprocess.run(
        ["git", "-C", str(project_dir), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout if done.returncode == 0 else None


def mode_only_paths(project_dir: pathlib.Path) -> list[str] | None:
    """Paths in `project_dir` whose ONLY change is the file mode.

    Returns None - meaning "leave this tree alone" - the moment anything
    else is dirty.

    A fresh checkout reports a handful of files as modified whose content is
    byte-identical to the index: only the mode differs, 100755 where git
    recorded 100644. What sets the execute bit is not established. It is
    demonstrably not anyone's work, which is all this function needs to
    decide.

    Deliberately strict, because the cost of a false "clean" is a `jiri
    update` run over someone's real work:

    - Only unstaged modifications (` M`) can qualify. Anything staged,
      untracked, added, deleted, renamed, copied or unmerged means a person
      touched this tree, and the answer is None immediately.
    - "Same content" is decided by hashing the working file and comparing to
      the blob the index records - not by a diff line count, which cannot
      tell a mode-only change from a content change in a binary file (both
      render as `-\t-` in `--numstat`, and `atx_metadata.bin` is exactly
      that case).
    - Any git invocation that fails at all yields None.
    """
    out = _git_out(project_dir, ["status", "--porcelain", "-z"])
    if out is None:
        return None
    changed: list[str] = []
    for record in out.split("\0"):
        if not record:
            continue
        status, path = record[:2], record[3:]
        # Rejecting R/C here also keeps this loop safe: a rename record is
        # followed by a second NUL-separated field (the source path), which
        # we never reach.
        if status != " M":
            return None
        listing = _git_out(project_dir, ["ls-files", "-s", "--", path])
        worktree = _git_out(project_dir, ["hash-object", "--", path])
        if not listing or not worktree:
            return None
        fields = listing.split()
        if len(fields) < 2 or fields[1] != worktree.strip():
            return None
        changed.append(path)
    return changed


def project_dirt_is_mode_only(project_dir: pathlib.Path) -> bool:
    """True when nothing in `project_dir` is dirty beyond file modes."""
    return mode_only_paths(project_dir) is not None


def restore_modes(fuchsia_dir: pathlib.Path, status_output: str) -> int | None:
    """Put mode-only-modified files back as the index records them.

    Returns how many files were restored, or None if any project holds real
    work (in which case nothing is touched).

    Restoring rather than ignoring is the point: `jiri update` refuses to run
    against a project with *any* uncommitted change, mode flips included -
    "Project fuchsia(.) contains uncommitted changes. Commit or discard the
    changes and try again." So merely waving the dirt past this guard just
    moves the failure one step later, from a silent skip to a failed update.

    This discards nothing. Every path was proven byte-identical to its index
    blob by `mode_only_paths`, so `git checkout` rewrites a permission bit
    and no content. Anything it cannot prove is left strictly alone.
    """
    projects = jiri_status_projects(status_output)
    if not projects:
        return None
    work: list[tuple[pathlib.Path, list[str]]] = []
    for rel in projects:
        project_dir = fuchsia_dir / rel
        changed = mode_only_paths(project_dir)
        if changed is None:
            return None
        work.append((project_dir, changed))
    restored = 0
    for project_dir, changed in work:
        if not changed:
            continue
        if _git_out(project_dir, ["checkout", "--", *changed]) is None:
            return None
        restored += len(changed)
    return restored


def disk_free_gb(path: pathlib.Path) -> float:
    """Free space on `path`'s filesystem, in GB (10^9 bytes)."""
    return shutil.disk_usage(path).free / 1e9
