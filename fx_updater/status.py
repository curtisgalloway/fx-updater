# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
"""Where a run keeps its state: the lock, the status document, the logs.

State lives under `$XDG_STATE_HOME/fx-updater/` (falling back to
`~/.local/state/fx-updater/`), so any account can run the tool without a
path being spelled out.

The lock is a file that is never removed; holding it means holding an
exclusive flock(2) on it. The kernel drops that the moment the holder exits,
however it exits, so a run killed outright (SIGKILL, power loss) cannot leave
a lock behind that blocks the next one. A consumer that must not build the
same tree at the same time asks `is_held` (fx_updater.contract wraps it);
the file merely existing means nothing. The holder's pid is written into it
for `fx-updater status` and cleared on a clean release; a killed run leaves
its dead pid there, which is harmless because only the flock is consulted.
The file is 0600 and never followed through a symlink.

The status document is overwritten atomically after every outcome, so the
last result is always one `cat` away: ok | skipped_wip | skipped_disk |
status_failed | update_failed | build_failed, with the reason and the log
path. `status_failed` means `jiri status` itself failed, so the WIP guard
could not run and nothing was touched.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import pathlib
import tempfile
import time

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
    """Another run holds the lock."""


# flock(2) is only "held" against other open file descriptions, so a
# consumer's `is_held` probe takes a shared lock for an instant. A run that
# starts in that instant would see the lock as busy; it retries this long
# before giving up. Probes poll on the order of a minute, so this is ample.
_ACQUIRE_TRIES = 10
_ACQUIRE_WAIT_SECS = 0.1


class Lock:
    """An exclusive flock on `path`, recording the owner's pid; a context manager."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> "Lock":
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # os.open is non-inheritable by default (PEP 446), so jiri, fx and the
        # hook never hold the lock: a daemon an fx build leaves running must
        # not keep the tree looking busy after this run has ended.
        # 0600: any account that can open the file can flock it, and a
        # shared lock held forever would refuse every run. O_NOFOLLOW: the
        # file is truncated below, so a planted symlink must not be followed.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        for attempt in range(_ACQUIRE_TRIES):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    os.close(fd)
                    raise
                if attempt == _ACQUIRE_TRIES - 1:
                    os.close(fd)
                    raise LockHeld(str(self.path)) from e
                time.sleep(_ACQUIRE_WAIT_SECS)
        self._fd = fd
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
        except OSError:
            # __exit__ never runs when __enter__ raises: release here.
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc) -> None:
        if self._fd is None:
            return
        try:
            # A released lock names nobody. Best effort: closing is what
            # releases the flock, and it happens regardless.
            os.ftruncate(self._fd, 0)
        except OSError:
            pass
        os.close(self._fd)
        self._fd = None


def is_held(path: pathlib.Path) -> bool:
    """True while some process holds the lock at `path`.

    Never creates the file. A lock file that exists but is not flocked (a
    run that ended, however it ended) is not held.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return True
        raise
    finally:
        # Closing releases the probe's shared lock along with the fd.
        os.close(fd)
    return False


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

    `pid` is the holder's, except in the instant between a run taking the
    lock and writing its pid, when it is None or a previous holder's.
    `pid_alive` is always True for a held lock - the kernel releases
    a dead holder's lock - and is kept so readers of the M2 document shape
    keep working.
    """
    if not is_held(path):
        return None
    try:
        text = path.read_text().strip()
    except FileNotFoundError:
        return None
    pid = int(text) if text.isdigit() else None
    return {"path": str(path), "pid": pid, "pid_alive": True}


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
