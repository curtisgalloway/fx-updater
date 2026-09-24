# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
"""Opt-in Prometheus textfile-collector output.

node_exporter (or Grafana Alloy's unix exporter) can read `*.prom` files
from a directory and publish them as metrics. Writing one after every run
turns "the job quietly stopped" into something an alert rule can see: a run
that fails turns `last_run_ok` to 0, and a job that stops running lets
`last_run_timestamp_seconds` go stale.

Only a run that reaches an outcome rewrites the file. A run refused before
that (lock held, jiri/fx or config missing) leaves the previous gauges in
place, which is why alerting keys on the age of the timestamp as well as on
`last_run_ok`.

Nothing is written unless a directory is configured (`--prom-dir` or the
config's `prom_dir`). The file is `fx_updater.prom`, rewritten whole and
renamed into place so a scrape never sees half of it. A failure to write it
never fails the run: `write` returns False and the caller warns.
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import pathlib
import tempfile
from typing import Iterable

FILENAME = "fx_updater.prom"
PREFIX = "fx_updater_"


@dataclasses.dataclass(frozen=True)
class Sample:
    """One gauge sample. `help` is emitted once per metric name, on first use."""

    name: str
    value: float | int | bool
    labels: dict[str, str] = dataclasses.field(default_factory=dict)
    help: str = ""


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_value(value: float | int | bool) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return repr(value) if isinstance(value, float) else str(value)


def render(samples: Iterable[Sample]) -> str:
    """The exposition text: HELP/TYPE once per metric, samples in given order."""
    lines: list[str] = []
    described: set[str] = set()
    for s in samples:
        if s.name not in described:
            if s.help:
                lines.append(f"# HELP {s.name} {s.help}")
            lines.append(f"# TYPE {s.name} gauge")
            described.add(s.name)
        labels = ",".join(
            f'{k}="{_escape_label(str(v))}"' for k, v in sorted(s.labels.items())
        )
        lines.append(f"{s.name}{{{labels}}} {_format_value(s.value)}")
    return "\n".join(lines) + "\n"


def samples_for(doc: dict) -> list[Sample]:
    """Gauges for one status document (see status.py for its fields)."""
    outcome = doc["outcome"]
    started = datetime.datetime.fromisoformat(doc["timestamp"]).timestamp()
    out = [
        Sample(
            PREFIX + "last_run_timestamp_seconds",
            started,
            help="start of the most recent run",
        ),
        Sample(PREFIX + "last_run_ok", outcome == "ok", help="1 iff outcome ok"),
        Sample(
            PREFIX + "last_run_skipped",
            outcome.startswith("skipped_"),
            help="1 iff the run was skipped (work in progress, disk floor)",
        ),
        Sample(
            PREFIX + "last_run_outcome",
            1,
            {"outcome": outcome},
            help="1, with the outcome as a label",
        ),
    ]
    if "commits_pulled" in doc:
        out.append(
            Sample(
                PREFIX + "commits_pulled",
                doc["commits_pulled"],
                help="integration commits the update pulled",
            )
        )
    for b in doc.get("builds", []):
        labels = {"build_dir": b["build_dir"]}
        out += [
            Sample(PREFIX + "build_seconds", b["build_secs"], labels),
            Sample(PREFIX + "build_exit", b["build_exit"], labels),
            Sample(PREFIX + "build_regen", b["regen"] is not None, labels),
        ]
    if "hook" in doc:
        out.append(
            Sample(
                PREFIX + "hook_ok",
                doc["hook"].get("exit") == 0,
                help="1 iff the post-build hook exited 0 (present only with a hook)",
            )
        )
    if "disk_free_gb" in doc:
        out.append(
            Sample(
                PREFIX + "disk_free_bytes",
                round(doc["disk_free_gb"] * 1e9),
                help="free space on the tree's filesystem",
            )
        )
    return out


def write(directory: pathlib.Path, samples: Iterable[Sample]) -> bool:
    """Atomically replace `directory/FILENAME`; False on any error.

    World-readable, since the collector usually runs as its own user.
    """
    text = render(samples)
    path = directory / FILENAME
    try:
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            os.unlink(tmp)
            raise
    except OSError:
        return False
    return True
