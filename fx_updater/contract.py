# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
"""The consumer contract: what another program may rely on.

A consumer (a boot test, a nightly scheduler, anything that builds or reads
the same tree) imports this module and nothing else from fx-updater. The
prose version, with the reasoning, is docs/contract.md; the two change
together, and a change that would break a reader of version N bumps
`SCHEMA_VERSION`.

- **The lock.** `tree_busy()` is True while a run is updating or building,
  including while its post-build hook runs. Wait for it to be False before
  building the same tree. A run that died released it already.
- **The status document.** `read_status()` returns the last run's document;
  `validate_status()` lists how a document breaks the schema. Readers ignore
  keys they do not know.
- **The hook.** A command run after every outcome, still under the lock,
  with `HOOK_ENV` in its environment. Its exit status is recorded and never
  changes the run's outcome.
"""

from __future__ import annotations

import pathlib
import time
from typing import Callable

from fx_updater import status

SCHEMA_VERSION = 1

OUTCOMES = status.OUTCOMES
StatusUnreadable = status.StatusUnreadable

# The environment a hook receives, beyond the run's own. Revisions are ""
# for an outcome that stopped before reading them.
ENV_SCHEMA_VERSION = "FX_UPDATER_SCHEMA_VERSION"
ENV_STATUS_FILE = "FX_UPDATER_STATUS_FILE"
ENV_OUTCOME = "FX_UPDATER_OUTCOME"
ENV_FUCHSIA_DIR = "FX_UPDATER_FUCHSIA_DIR"
ENV_BUILD_DIRS = "FX_UPDATER_BUILD_DIRS"  # space-separated names under out/
ENV_RUN_START = "FX_UPDATER_RUN_START"  # epoch seconds, start of the run
ENV_LOG = "FX_UPDATER_LOG"
ENV_INTEGRATION_PRE = "FX_UPDATER_INTEGRATION_PRE"
ENV_INTEGRATION_POST = "FX_UPDATER_INTEGRATION_POST"
ENV_TREE_PRE = "FX_UPDATER_TREE_PRE"
ENV_TREE_POST = "FX_UPDATER_TREE_POST"

HOOK_ENV = (
    ENV_SCHEMA_VERSION,
    ENV_STATUS_FILE,
    ENV_OUTCOME,
    ENV_FUCHSIA_DIR,
    ENV_BUILD_DIRS,
    ENV_RUN_START,
    ENV_LOG,
    ENV_INTEGRATION_PRE,
    ENV_INTEGRATION_POST,
    ENV_TREE_PRE,
    ENV_TREE_POST,
)


def lock_path() -> pathlib.Path:
    """`$XDG_STATE_HOME/fx-updater/lock`: one per account, not per tree."""
    return status.lock_path()


def status_path() -> pathlib.Path:
    """`$XDG_STATE_HOME/fx-updater/status.json`, where `run` writes by default."""
    return status.default_status_path()


def tree_busy(path: pathlib.Path | None = None) -> bool:
    """True while an fx-updater run holds the lock."""
    return status.is_held(path or lock_path())


def wait_until_idle(
    timeout_secs: float,
    poll_secs: float = 60.0,
    on_wait: Callable[[], None] | None = None,
    path: pathlib.Path | None = None,
) -> bool:
    """Block until no run holds the lock; False if `timeout_secs` passes first.

    `on_wait` is called before each sleep, for a consumer's log line.
    """
    t0 = time.monotonic()
    while tree_busy(path):
        if time.monotonic() - t0 > timeout_secs:
            return False
        if on_wait:
            on_wait()
        time.sleep(poll_secs)
    return True


def read_status(path: pathlib.Path | None = None) -> dict | None:
    """The last run's document, or None if no run has written one.

    Raises StatusUnreadable for a file that is not a status document. A
    document from before `schema_version` existed still reads; check
    `validate_status` when the version matters.
    """
    return status.read_status(path or status_path())


_STR, _NUM, _INT = (str,), (int, float), (int,)

# key -> accepted types. Required in every document: the first six.
_REQUIRED = {
    "schema_version": _INT,
    "timestamp": _STR,
    "outcome": _STR,
    "reason": _STR,
    "log": _STR,
    "fuchsia_dir": _STR,
}
_OPTIONAL = {
    "disk_free_gb": _NUM,
    "wip": _STR,
    "detail": _STR,
    "update_rc": _INT,
    "update_secs": _NUM,
    "integration_pre": _STR,
    "integration_post": _STR,
    "tree_pre": _STR,
    "tree_post": _STR,
    "commits_pulled": _INT,
    "builds": (list,),
    "hook": (dict,),
}
_BUILD = {
    "build_dir": _STR,
    "build_start": _NUM,
    "build_secs": _NUM,
    "build_exit": _INT,
}


def _type_ok(value, types) -> bool:
    # bool is an int subclass; a boolean is never a valid number here.
    return isinstance(value, types) and not isinstance(value, bool)


def validate_status(doc) -> list[str]:
    """Every way `doc` breaks schema version 1; [] means it conforms."""
    if not isinstance(doc, dict):
        return ["not a JSON object"]
    errors = []
    for key, types in _REQUIRED.items():
        if key not in doc:
            errors.append(f"missing {key}")
        elif not _type_ok(doc[key], types):
            errors.append(f"{key} has the wrong type")
    if doc.get("schema_version") not in (None, SCHEMA_VERSION):
        errors.append(
            f"schema_version {doc['schema_version']!r} is not {SCHEMA_VERSION}"
        )
    if "outcome" in doc and doc["outcome"] not in OUTCOMES:
        errors.append(f"unknown outcome {doc['outcome']!r}")
    for key, types in _OPTIONAL.items():
        if key in doc and not _type_ok(doc[key], types):
            errors.append(f"{key} has the wrong type")
    for i, b in enumerate(doc.get("builds") or []):
        if not isinstance(b, dict):
            errors.append(f"builds[{i}] is not an object")
            continue
        for key, types in _BUILD.items():
            if key not in b:
                errors.append(f"builds[{i}] missing {key}")
            elif not _type_ok(b[key], types):
                errors.append(f"builds[{i}].{key} has the wrong type")
        if "regen" in b and b["regen"] is not None and not isinstance(b["regen"], str):
            errors.append(f"builds[{i}].regen has the wrong type")
    hook = doc.get("hook")
    if isinstance(hook, dict):
        if not isinstance(hook.get("argv"), list):
            errors.append("hook.argv has the wrong type")
        if hook.get("exit") is not None and not _type_ok(hook.get("exit"), _INT):
            errors.append("hook.exit has the wrong type")
    return errors


def hook_env(
    doc: dict, status_file: pathlib.Path, build_dirs: list[str], run_start: float
) -> dict[str, str]:
    """The variables a hook gets for the run that wrote `doc`."""
    return {
        ENV_SCHEMA_VERSION: str(SCHEMA_VERSION),
        ENV_STATUS_FILE: str(status_file),
        ENV_OUTCOME: doc["outcome"],
        ENV_FUCHSIA_DIR: doc.get("fuchsia_dir", ""),
        ENV_BUILD_DIRS: " ".join(build_dirs),
        ENV_RUN_START: f"{run_start:.3f}",
        ENV_LOG: doc.get("log", ""),
        ENV_INTEGRATION_PRE: doc.get("integration_pre", ""),
        ENV_INTEGRATION_POST: doc.get("integration_post", ""),
        ENV_TREE_PRE: doc.get("tree_pre", ""),
        ENV_TREE_POST: doc.get("tree_post", ""),
    }
