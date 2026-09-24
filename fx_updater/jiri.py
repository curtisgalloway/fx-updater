# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
"""`jiri update` failure classification and the revisions around an update.

Two repositories matter and they are easy to confuse:

- **integration.git** (`<tree>/integration`) is the manifest repo. Its HEAD
  is what jiri pins, so it is what "commits pulled" counts.
- **fuchsia.git** (the tree root) holds `src/`, `zircon/` and the rest. A
  consumer that wants to know which source paths moved diffs this one.
"""

from __future__ import annotations

import pathlib
import subprocess

# Substrings (lowercased) that mark a jiri update failure as network-shaped
# and therefore worth retrying. Everything else - rebase conflicts, corrupt
# checkouts, disk errors - is a hard failure that a retry would only repeat.
_TRANSIENT_PATTERNS = (
    "could not resolve host",
    "connection timed out",
    "connection reset",
    "connection refused",
    "network is unreachable",
    "temporary failure",
    "temporarily unavailable",
    "early eof",
    "rpc failed",
    "unable to access",
    "tls handshake",
    "remote end hung up",
    "http 5",
    # git's form for an HTTP error status: "The requested URL returned error:
    # 503". A bare "503" also matched jiri's millisecond timestamps
    # ("[05:30:36.503]") and commit hashes, retrying hard failures.
    "returned error: 5",
    "503 service unavailable",
)


def classify_update_failure(log_tail: str) -> str:
    """Return "transient" for network-shaped failures, else "hard"."""
    low = log_tail.lower()
    if any(p in low for p in _TRANSIENT_PATTERNS):
        return "transient"
    return "hard"


def _rev_parse_head(repo: pathlib.Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip() if out.returncode == 0 else "unknown"


def integration_rev(fuchsia_dir: pathlib.Path) -> str:
    """HEAD of integration.git, or "unknown"."""
    return _rev_parse_head(fuchsia_dir / "integration")


def tree_rev(fuchsia_dir: pathlib.Path) -> str:
    """HEAD of the root fuchsia.git, or "unknown"."""
    return _rev_parse_head(fuchsia_dir)


def commits_between(fuchsia_dir: pathlib.Path, pre: str, post: str) -> int:
    """integration.git commits in pre..post; 0 if unknown or equal, -1 on error."""
    if "unknown" in (pre, post) or pre == post:
        return 0
    out = subprocess.run(
        [
            "git",
            "-C",
            str(fuchsia_dir / "integration"),
            "rev-list",
            "--count",
            f"{pre}..{post}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return int(out.stdout.strip()) if out.returncode == 0 else -1
