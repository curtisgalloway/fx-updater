# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures."""

import contextlib
import subprocess
import sys

import pytest

_HOLDER = """
import sys, pathlib
from fx_updater import status
with status.Lock(pathlib.Path(sys.argv[1])):
    print("held", flush=True)
    sys.stdin.readline()
"""


@contextlib.contextmanager
def _held(path):
    """Hold the fx-updater lock at `path` from another process until exit."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline() == "held\n"
        yield proc
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


@pytest.fixture(name="hold_lock")
def fixture_hold_lock():
    """`with hold_lock(path) as proc:` - the lock is held by `proc.pid`."""
    return _held
