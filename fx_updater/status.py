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

The status document is overwritten atomically after every outcome, so the
last result is always one `cat` away: ok | skipped_wip | skipped_disk |
update_failed | build_failed, with the reason and the log path.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile

OUTCOMES = ("ok", "skipped_wip", "skipped_disk", "update_failed", "build_failed")


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
