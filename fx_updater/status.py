# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
"""Where a run keeps its state: the lock, the status document, the logs.

State lives under `$XDG_STATE_HOME/fx-updater/` (falling back to
`~/.local/state/fx-updater/`), so any account can run the tool without a
path being spelled out.

The lock is a file created with O_EXCL and removed when the run ends. Its
*existence* is the signal: a consumer that must not build the same tree at
the same time waits for it to disappear. A run that is killed outright
leaves it behind, and the next run then refuses to start until someone
removes it - deliberately loud rather than guessing that the holder is gone.
`fx-updater status` reports such a lock as stale (its pid is not running).

The status document is overwritten atomically after every outcome, so the
last result is always one `cat` away: ok | skipped_wip | skipped_disk |
status_failed | update_failed | build_failed, with the reason and the log
path. `status_failed` means `jiri status` itself failed, so the WIP guard
could not run and nothing was touched.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile

OUTCOMES = (
    "ok",
    "skipped_wip",
    "skipped_disk",
    "status_failed",
    "update_failed",
    "build_failed",
)
# Reported by `status` only, when no run has written a document yet.
NEVER_RUN = "never_run"


def state_dir() -> pathlib.Path:
    """`$XDG_STATE_HOME/fx-updater`, else `~/.local/state/fx-updater`."""
    xdg = os.environ.get("XDG_STATE_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".local" / "state"
    return base / "fx-updater"


def lock_path() -> pathlib.Path:
    return state_dir() / "lock"


def default_status_path() -> pathlib.Path:
    return state_dir() / "status.json"


def default_log_dir() -> pathlib.Path:
    return state_dir() / "logs"


class LockHeld(Exception):
    """Another run holds the lock (or a killed run left it behind)."""


class Lock:
    """An O_EXCL lock file holding the owner's pid; a context manager."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> "Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as e:
            raise LockHeld(str(self.path)) from e
        try:
            os.write(self._fd, str(os.getpid()).encode())
        except OSError:
            # __exit__ never runs when __enter__ raises: release here, or a
            # full disk leaves a lock that blocks every later run.
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.path.unlink(missing_ok=True)


class StatusUnreadable(Exception):
    """The status document exists but is not a document `run` wrote."""


def read_status(path: pathlib.Path) -> dict | None:
    """The last run's document, or None if no run has written one."""
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise StatusUnreadable(f"{path}: {e}") from e
    if not isinstance(doc, dict) or doc.get("outcome") not in OUTCOMES:
        raise StatusUnreadable(f"{path}: not an fx-updater status document")
    return doc


def summary_line(doc: dict | None, path: pathlib.Path) -> str:
    """One line for a person: outcome, when, and why."""
    if doc is None:
        return f"{NEVER_RUN}: no status document at {path}"
    return f"{doc['outcome']} {doc.get('timestamp', '?')}: {doc.get('reason', '')}"


def lock_holder(path: pathlib.Path) -> dict | None:
    """Who holds the lock at `path`, or None if nobody does.

    `pid_alive` is False when the recorded pid is not running, i.e. a killed
    run left the lock behind. It is None when that cannot be told: an empty
    file (a run between creating the lock and writing its pid) or garbage.
    A recycled pid reads as alive, so False is certain and True is not.
    """
    try:
        text = path.read_text().strip()
    except FileNotFoundError:
        return None
    pid = int(text) if text.isdigit() else None
    alive = None
    if pid is not None:
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
    return {"path": str(path), "pid": pid, "pid_alive": alive}


def write_status(path: pathlib.Path, payload: dict) -> None:
    """Write the status document atomically (temp file, then rename)."""
    if payload.get("outcome") not in OUTCOMES:
        raise ValueError(f"unknown outcome {payload.get('outcome')!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp: an unpredictable name opened O_EXCL, so a symlink planted
    # beside a status file in a shared directory is never followed.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(payload, indent=1) + "\n")
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise
