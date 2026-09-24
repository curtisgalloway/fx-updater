# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
"""`fx build` with the one recovery an unattended job can safely make itself.

When upstream deletes a file that GN had recorded as an input of
`build.ninja`, ninja refuses to regenerate and the build dies before a single
action runs:

    ninja: error: rebuilding 'build.ninja': '../../build/beads/.agent/skills/
    migrating_host_tool_to_bazel/examples/go/after/BUILD.gn', needed by
    'build.ninja.stamp', missing and no known rule to make it

That is what killed an unattended morning update and the boot test that
followed it on 2026-09-01 (upstream 0ff75a4021b removed the file), and they
would have kept dying every morning: `fx build` re-runs `gn gen` only for its
own landmine files (the gn binary, args.gn, the fx scripts), never for this.
The fix a person types is `fx gen`, so this module types it - build, and if
the failure is exactly that message, `fx gen` once and build again. Any other
failure is returned untouched: a regen repairs a stale graph and nothing
else, and retrying a real compile error unattended would only double the log.

Pure orchestration: the caller supplies the command runner and the log tail,
so the decision is unit-testable without a Fuchsia tree.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import time
from typing import Callable

_STALE_REGEN_RE = re.compile(
    r"ninja: error: rebuilding 'build\.ninja': '(?P<path>[^']+)'.*?"
    r"missing and no known rule to make it"
)

# Runs one command in the tree, logging as the caller sees fit; returns rc.
Runner = Callable[[list[str]], int]


def stale_regen_input(log_tail: str) -> str | None:
    """The build.ninja input ninja reports missing, or None for any other log."""
    m = _STALE_REGEN_RE.search(log_tail)
    return m.group("path") if m else None


@dataclasses.dataclass
class BuildResult:
    """Outcome of build_with_regen. `secs` spans every attempt, gen included."""

    returncode: int
    secs: float
    regen: str | None = None  # the missing input that forced `fx gen`, if any
    regen_returncode: int | None = None


def build_with_regen(
    fx: pathlib.Path,
    build_dir: str,
    run: Runner,
    log_tail: Callable[[], str],
) -> BuildResult:
    """`fx --dir out/<build_dir> build`, regenerating once if the graph is stale.

    `log_tail` is consulted only after a failure; it must return the end of
    the output of the command `run` last ran, and nothing older (the ninja
    error is the last thing printed). A tail of a log shared with earlier
    commands can carry another build dir's stale-graph message into this
    decision.
    """
    fx_dir = [str(fx), "--dir", f"out/{build_dir}"]
    t0 = time.monotonic()
    rc = run(fx_dir + ["build"])
    if rc == 0:
        return BuildResult(rc, time.monotonic() - t0)
    missing = stale_regen_input(log_tail())
    if missing is None:
        return BuildResult(rc, time.monotonic() - t0)
    gen_rc = run(fx_dir + ["gen"])
    if gen_rc == 0:
        rc = run(fx_dir + ["build"])
    return BuildResult(
        rc, time.monotonic() - t0, regen=missing, regen_returncode=gen_rc
    )
